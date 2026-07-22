"""kernel/scanner.py — polling-based async file scanner.

Watches one or more directories for files matching configured extensions and
publishes ``document.scanned`` events onto the EventBus. Polling (not watchdog)
is deliberate: it keeps the dependency graph clean (no external file-watch lib)
and is trivially testable and deterministic.

AXIS CONTRACT: depends on kernel.domain (Event) + kernel.bus (EventBus) only.
Scanner is workspace-scoped — every emitted event carries the owning
``workspace_id`` so downstream pipeline stages stay tenant-aware.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path

from kernel.bus import EventBus
from kernel.domain import Event

logger = logging.getLogger("hermes.scanner")

DEFAULT_EXTENSIONS = (".md", ".pdf", ".txt")

# Explicit MIME map so results don't depend on the OS mimetypes registry
# (e.g. ".md" is often unregistered on Windows).
_MIME_MAP = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
}


class FileScanner:
    """Async, polling directory scanner bound to a single workspace."""

    def __init__(
        self,
        workspace_id: str,
        paths: list[str | Path],
        bus: EventBus,
        extensions: tuple[str, ...] | list[str] = DEFAULT_EXTENSIONS,
        recursive: bool = True,
        interval: float = 1.0,
    ) -> None:
        self.workspace_id = workspace_id
        self.paths = [Path(p) for p in paths]
        self._bus = bus
        # normalise: lowercase, ensure leading dot
        self.extensions = {
            (e if e.startswith(".") else f".{e}").lower() for e in extensions
        }
        self.recursive = recursive
        self.interval = interval
        self._running = False
        self._task: asyncio.Task | None = None
        # remember already-seen files so watch mode only emits new ones
        self._seen: set[str] = set()

    # -- discovery -------------------------------------------------------- #
    def _iter_files(self):
        for base in self.paths:
            if not base.exists():
                logger.warning("scan path does not exist: %s", base)
                continue
            it = base.rglob("*") if self.recursive else base.glob("*")
            for p in it:
                if p.is_file() and p.suffix.lower() in self.extensions:
                    yield p

    def _make_event(self, path: Path) -> Event:
        ext = path.suffix.lower()
        mime = _MIME_MAP.get(ext) or mimetypes.guess_type(str(path))[0]
        return Event(
            type="document.scanned",
            source=f"scanner:{self.workspace_id}",
            payload={
                "path": str(path.resolve()),
                "mime_type": mime,
                "workspace_id": self.workspace_id,
            },
        )

    # -- public API ------------------------------------------------------- #
    def scan_once(self) -> list[Event]:
        """One synchronous sweep. Returns the events (also published)."""
        events: list[Event] = []
        for p in self._iter_files():
            key = str(p.resolve())
            evt = self._make_event(p)
            events.append(evt)
            self._seen.add(key)
            self._bus.publish(evt)
        return events

    def _scan_new(self) -> list[Event]:
        """Sweep, but only emit files not seen before (watch loop)."""
        events: list[Event] = []
        for p in self._iter_files():
            key = str(p.resolve())
            if key in self._seen:
                continue
            self._seen.add(key)
            evt = self._make_event(p)
            events.append(evt)
            self._bus.publish(evt)
        return events

    async def _watch_loop(self) -> None:
        while self._running:
            try:
                self._scan_new()
            except Exception:  # noqa: BLE001 — fault containment
                logger.exception("scan sweep failed")
            await asyncio.sleep(self.interval)

    async def start(self) -> None:
        """Begin polling in the background."""
        if self._running:
            return
        self._running = True
        # emit current contents immediately, then poll for new files
        self._scan_new()
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop polling and await loop teardown."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
