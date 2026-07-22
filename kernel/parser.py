"""kernel/parser.py — content extraction stage of the knowledge pipeline.

Subscribes to ``document.scanned`` events, extracts text content according to
the file's MIME type, and republishes ``document.parsed``. This is the second
stage of P2 (scanner → parser → chunker → embedding → graph).

AXIS CONTRACT: depends on kernel.domain (Event) + kernel.bus (EventBus) only.

Dependency policy (mirrors the scanner's "no watchdog" stance): PDF extraction
uses ``pdfminer.six`` *if installed*, imported lazily inside the method. When it
is absent, ``parse`` degrades gracefully to a placeholder instead of crashing —
keeping the core dependency graph clean and CI free of heavy optional libs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kernel.bus import EventBus
from kernel.domain import Event

logger = logging.getLogger("hermes.parser")

_TEXT_MIMES = {"text/markdown", "text/plain", "text/csv", "text/html", "application/json"}


class DocumentParser:
    """Extract text from scanned documents and emit ``document.parsed``."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._sub_id = None

    # -- extraction ------------------------------------------------------- #
    def parse(self, path: str | Path, mime_type: str | None) -> str:
        """Return extracted text content for a file. Never raises on content."""
        p = Path(path)
        mime = (mime_type or "").lower()

        if mime == "application/pdf" or p.suffix.lower() == ".pdf":
            return self._parse_pdf(p)

        # everything text-like (and unknown-but-readable) is read as UTF-8 text
        if mime in _TEXT_MIMES or mime.startswith("text/") or mime == "":
            return self._parse_text(p)

        # binary / unsupported → empty content, logged (not an error)
        logger.info("unsupported mime %r for %s; empty content", mime, p)
        return ""

    @staticmethod
    def _parse_text(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.exception("failed reading text file %s", p)
            return ""

    @staticmethod
    def _parse_pdf(p: Path) -> str:
        try:
            from pdfminer.high_level import extract_text  # type: ignore
        except ImportError:
            logger.warning(
                "pdfminer.six not installed; PDF %s parsed as placeholder", p
            )
            return f"[pdf:{p.name}]"
        try:
            return extract_text(str(p)) or ""
        except Exception:  # noqa: BLE001 — never crash the pipeline on a bad file
            logger.exception("pdfminer failed on %s", p)
            return ""

    # -- event wiring ----------------------------------------------------- #
    async def _on_scanned(self, event: Event) -> None:
        payload = event.payload
        path = payload.get("path")
        mime = payload.get("mime_type")
        workspace_id = payload.get("workspace_id")
        try:
            content = self.parse(path, mime)
        except Exception:  # noqa: BLE001 — fault containment
            logger.exception("parse failed for %s", path)
            return
        self._bus.publish(
            Event(
                type="document.parsed",
                source="parser",
                payload={
                    "path": path,
                    "content": content,
                    "mime_type": mime,
                    "workspace_id": workspace_id,
                },
            )
        )

    async def start(self) -> None:
        """Subscribe to ``document.scanned``."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe("document.scanned", self._on_scanned)

    async def stop(self) -> None:
        """Unsubscribe from the bus."""
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None
