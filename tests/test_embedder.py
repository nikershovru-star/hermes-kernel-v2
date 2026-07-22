"""tests/test_embedder.py — ChunkEmbedder backends + bus chaining."""

import asyncio

import pytest

from kernel.bus import EventBus
from kernel.domain import Event
from kernel.embedder import ChunkEmbedder, HASH_DIM


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


async def test_embed_hash(bus) -> None:
    emb = ChunkEmbedder(bus, backend="hash")
    v1 = emb.embed("hello world")
    v2 = emb.embed("hello world")
    assert isinstance(v1, list)
    assert all(isinstance(x, float) for x in v1)
    assert v1 == v2  # deterministic
    assert emb.embed("different") != v1  # sensitive to input


async def test_embed_shape(bus) -> None:
    emb = ChunkEmbedder(bus, backend="hash")
    assert len(emb.embed("a")) == HASH_DIM
    assert len(emb.embed("a much longer piece of text " * 20)) == HASH_DIM
    assert len(emb.embed("")) == HASH_DIM  # empty edge case still fixed-dim
    # L2-normalised (non-empty text -> unit norm ~ 1.0)
    v = emb.embed("normalise me")
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-9


async def test_embedder_subscribes_to_chunker(bus) -> None:
    emb = ChunkEmbedder(bus, backend="hash")
    await emb.start()

    embedded = bus.wait_for(["chunk.embedded"])
    bus.publish(
        Event(
            type="chunk.created",
            source="chunker",
            payload={
                "chunk_id": "chunk-1",
                "document_path": "/ws/doc.md",
                "text": "some chunk text",
                "embedding": None,
                "workspace_id": "ws1",
            },
        )
    )
    evt = await asyncio.wait_for(embedded, timeout=2.0)

    assert evt.type == "chunk.embedded"
    assert evt.payload["chunk_id"] == "chunk-1"
    assert evt.payload["workspace_id"] == "ws1"
    assert len(evt.payload["embedding"]) == HASH_DIM

    await emb.stop()
    assert bus.subscriber_count("chunk.created") == 0


async def test_st_backend_falls_back_to_hash(bus) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("sentence_transformers"):
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    emb = ChunkEmbedder(bus, backend="sentence-transformers")
    from unittest.mock import patch

    with patch.object(builtins, "__import__", blocked):
        vec = emb.embed("fallback please")
    assert len(vec) == HASH_DIM  # fell back to hash backend
