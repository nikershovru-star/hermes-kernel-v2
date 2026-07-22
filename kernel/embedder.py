"""kernel/embedder.py — embedding stage of the knowledge pipeline.

Subscribes to ``chunk.created`` events, computes a vector embedding for each
chunk's text, and republishes ``chunk.embedded``. Fourth stage of P2
(scanner → parser → chunker → embedding → graph).

AXIS CONTRACT: depends on kernel.domain (Event) + kernel.bus (EventBus) only.

Backends (dependency policy mirrors parser/scanner — heavy libs stay optional):

- ``"hash"`` (default): deterministic, dependency-free embedding derived from a
  SHA-256 digest of the text, expanded to a fixed-dimension unit-ish float
  vector. Same text -> same vector, always. Ideal for CI and tests.
- ``"sentence-transformers"``: real semantic embeddings via the optional
  ``sentence-transformers`` package, imported lazily. Falls back to ``"hash"``
  with a warning if the package is not installed.
"""

from __future__ import annotations

import hashlib
import logging
import struct

from kernel.bus import EventBus
from kernel.domain import Event

logger = logging.getLogger("hermes.embedder")

HASH_DIM = 64  # fixed dimension for the hash backend
_ST_MODEL = "all-MiniLM-L6-v2"


class ChunkEmbedder:
    """Compute embeddings for created chunks and emit ``chunk.embedded``."""

    def __init__(self, bus: EventBus, backend: str = "hash") -> None:
        self._bus = bus
        self.backend = backend
        self._sub_id = None
        self._st_model = None  # lazily loaded sentence-transformers model

    # -- embedding backends ---------------------------------------------- #
    def embed(self, text: str) -> list[float]:
        """Return a deterministic (hash) or semantic (ST) embedding vector."""
        if self.backend == "sentence-transformers":
            vec = self._embed_st(text)
            if vec is not None:
                return vec
            # fall through to hash backend if ST unavailable
        return self._embed_hash(text)

    @staticmethod
    def _embed_hash(text: str) -> list[float]:
        """Deterministic embedding: expand SHA-256 digests into HASH_DIM floats.

        Each 4-byte word of successive digests becomes one float in [0, 1). The
        vector is then L2-normalised so magnitudes are comparable across texts.
        """
        raw = text.encode("utf-8")
        floats: list[float] = []
        counter = 0
        while len(floats) < HASH_DIM:
            digest = hashlib.sha256(raw + counter.to_bytes(4, "big")).digest()
            for i in range(0, len(digest), 4):
                if len(floats) >= HASH_DIM:
                    break
                word = struct.unpack(">I", digest[i : i + 4])[0]
                floats.append(word / 0xFFFFFFFF)
            counter += 1
        # L2 normalise (avoid div-by-zero for the empty-string edge case)
        norm = sum(f * f for f in floats) ** 0.5
        if norm > 0:
            floats = [f / norm for f in floats]
        return floats

    def _embed_st(self, text: str) -> list[float] | None:
        try:
            if self._st_model is None:
                from sentence_transformers import SentenceTransformer  # type: ignore

                self._st_model = SentenceTransformer(_ST_MODEL)
            vec = self._st_model.encode(text)
            return [float(x) for x in vec]
        except ImportError:
            logger.warning(
                "sentence-transformers not installed; falling back to hash backend"
            )
            return None
        except Exception:  # noqa: BLE001 — never crash the pipeline
            logger.exception("sentence-transformers failed; falling back to hash")
            return None

    # -- event wiring ----------------------------------------------------- #
    async def _on_chunk(self, event: Event) -> None:
        payload = event.payload
        chunk_id = payload.get("chunk_id")
        text = payload.get("text") or ""
        workspace_id = payload.get("workspace_id", "default")
        try:
            embedding = self.embed(text)
        except Exception:  # noqa: BLE001 — fault containment
            logger.exception("embedding failed for chunk %s", chunk_id)
            return
        self._bus.publish(
            Event(
                type="chunk.embedded",
                source="embedder",
                payload={
                    "chunk_id": chunk_id,
                    "embedding": embedding,
                    "workspace_id": workspace_id,
                },
            )
        )

    async def start(self) -> None:
        """Subscribe to ``chunk.created``."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe("chunk.created", self._on_chunk)

    async def stop(self) -> None:
        """Unsubscribe from the bus."""
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None
