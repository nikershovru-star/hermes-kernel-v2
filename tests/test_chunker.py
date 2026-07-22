"""tests/test_chunker.py — DocumentChunker splitting + bus chaining."""

import asyncio

import pytest

from kernel.bus import EventBus
from kernel.domain import Event
from kernel.chunker import DocumentChunker


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


async def test_chunk_md(bus) -> None:
    content = "x" * 2500  # 2500 chars, size 1000/overlap 100 -> 3 chunks
    chunker = DocumentChunker(bus, chunk_size=1000, overlap=100)
    chunks = chunker.chunk(content, "/ws/note.md", "ws1")
    assert len(chunks) >= 2
    assert all(c.text for c in chunks)
    assert all(c.workspace_id == "ws1" for c in chunks)
    assert all(c.document_id == "/ws/note.md" for c in chunks)


async def test_chunk_overlap(bus) -> None:
    content = "".join(str(i % 10) for i in range(2500))
    chunker = DocumentChunker(bus, chunk_size=1000, overlap=100)
    chunks = chunker.chunk(content, "/ws/doc.md", "ws1")
    assert len(chunks) >= 2
    # tail of chunk[0] must equal head of chunk[1] (overlap window)
    c0, c1 = chunks[0], chunks[1]
    assert c0.text[-100:] == c1.text[:100]
    # offsets confirm the step = size - overlap
    assert c1.start == c0.start + (1000 - 100)


async def test_chunk_size_boundaries(bus) -> None:
    chunker = DocumentChunker(bus, chunk_size=100, overlap=20)
    assert chunker.chunk("", "/p", "ws") == []          # empty -> no chunks
    one = chunker.chunk("short", "/p", "ws")            # smaller than size
    assert len(one) == 1
    assert one[0].start == 0 and one[0].end == 5


async def test_chunker_subscribes_to_parser(bus) -> None:
    chunker = DocumentChunker(bus, chunk_size=1000, overlap=100)
    await chunker.start()

    created = bus.wait_for(["chunk.created"])
    bus.publish(
        Event(
            type="document.parsed",
            source="parser",
            payload={
                "path": "/ws/chain.md",
                "content": "y" * 1500,
                "mime_type": "text/markdown",
                "workspace_id": "ws1",
            },
        )
    )
    evt = await asyncio.wait_for(created, timeout=2.0)

    assert evt.type == "chunk.created"
    assert evt.payload["document_path"] == "/ws/chain.md"
    assert evt.payload["workspace_id"] == "ws1"
    assert evt.payload["embedding"] is None
    assert evt.payload["text"]
    assert evt.payload["chunk_id"]

    await chunker.stop()
    assert bus.subscriber_count("document.parsed") == 0


def test_invalid_config(bus) -> None:
    with pytest.raises(ValueError):
        DocumentChunker(bus, chunk_size=0)
    with pytest.raises(ValueError):
        DocumentChunker(bus, chunk_size=100, overlap=100)
