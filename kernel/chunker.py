"""kernel/chunker.py — chunking stage of the knowledge pipeline.

Subscribes to ``document.parsed`` events, splits the extracted content into
overlapping ``domain.Chunk`` entities, and republishes ``chunk.created`` (one
event per chunk). Third stage of P2 (scanner → parser → chunker → embedding →
graph).

AXIS CONTRACT: depends on kernel.domain (Chunk, Event) + kernel.bus (EventBus).

Chunking is a fixed-size sliding window over characters with a configurable
overlap, so consecutive chunks share `overlap` trailing/leading characters —
this preserves context across boundaries for the downstream embedder.
"""

from __future__ import annotations

import logging

from kernel.bus import EventBus
from kernel.domain import Chunk, Event

logger = logging.getLogger("hermes.chunker")


class DocumentChunker:
    """Split parsed document content into overlapping chunks."""

    def __init__(
        self, bus: EventBus, chunk_size: int = 1000, overlap: int = 100
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be > 0")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")
        self._bus = bus
        self.chunk_size = chunk_size
        self.overlap = overlap
        self._sub_id = None

    # -- core ------------------------------------------------------------- #
    def chunk(self, content: str, source_path: str, workspace_id: str) -> list[Chunk]:
        """Slice `content` into overlapping Chunk entities."""
        chunks: list[Chunk] = []
        if not content:
            return chunks

        step = self.chunk_size - self.overlap
        start = 0
        n = len(content)
        while start < n:
            end = min(start + self.chunk_size, n)
            text = content[start:end]
            chunks.append(
                Chunk(
                    document_id=source_path,
                    text=text,
                    start=start,
                    end=end,
                    workspace_id=workspace_id,
                    metadata={"document_path": source_path},
                )
            )
            if end >= n:
                break
            start += step
        return chunks

    # -- event wiring ----------------------------------------------------- #
    async def _on_parsed(self, event: Event) -> None:
        payload = event.payload
        content = payload.get("content") or ""
        path = payload.get("path")
        workspace_id = payload.get("workspace_id", "default")
        try:
            chunks = self.chunk(content, path, workspace_id)
        except Exception:  # noqa: BLE001 — fault containment
            logger.exception("chunking failed for %s", path)
            return
        for c in chunks:
            self._bus.publish(
                Event(
                    type="chunk.created",
                    source="chunker",
                    payload={
                        "chunk_id": c.id,
                        "document_path": path,
                        "text": c.text,
                        "embedding": None,
                        "workspace_id": workspace_id,
                    },
                )
            )

    async def start(self) -> None:
        """Subscribe to ``document.parsed``."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe("document.parsed", self._on_parsed)

    async def stop(self) -> None:
        """Unsubscribe from the bus."""
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None
