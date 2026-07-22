"""tests/test_graph.py — KnowledgeGraph node/edge building + bus chaining."""

import asyncio

import pytest

from kernel.bus import EventBus
from kernel.domain import Chunk, Event
from kernel.embedder import ChunkEmbedder
from kernel.graph import KnowledgeGraph, cosine_similarity


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def _emb(text: str) -> list[float]:
    return ChunkEmbedder(EventBus(), backend="hash").embed(text)


async def test_add_node(bus) -> None:
    g = KnowledgeGraph(bus, similarity_threshold=0.8)
    chunk = Chunk(
        document_id="/ws/doc.md",
        text="hello world",
        embedding=_emb("hello world"),
        workspace_id="ws1",
    )
    node = g.add_node(chunk)
    assert node.type == "chunk"
    assert node.domain == "ws1"
    assert node.label == "hello world"
    assert node.properties["text"] == "hello world"
    assert g.node_count == 1


async def test_similarity_links(bus) -> None:
    # identical embeddings -> cosine 1.0 -> guaranteed edge above threshold
    g = KnowledgeGraph(bus, similarity_threshold=0.8)
    e = _emb("same content")
    c1 = Chunk(document_id="/d1", text="same content", embedding=e, workspace_id="ws1")
    c2 = Chunk(document_id="/d2", text="same content", embedding=list(e), workspace_id="ws1")

    n1 = g.add_node(c1)
    n2 = g.add_node(c2)

    # n2 should link to n1 (and symmetric back-edge exists)
    targets_n2 = [edge.target_id for edge in g.edges_of(n2.id)]
    assert n1.id in targets_n2
    targets_n1 = [edge.target_id for edge in g.edges_of(n1.id)]
    assert n2.id in targets_n1
    # score recorded
    assert g.edges_of(n2.id)[0].properties["score"] > 0.99


async def test_no_link_when_dissimilar(bus) -> None:
    g = KnowledgeGraph(bus, similarity_threshold=0.999)
    c1 = Chunk(document_id="/d1", text="alpha", embedding=_emb("alpha"), workspace_id="ws1")
    c2 = Chunk(document_id="/d2", text="totally different topic", embedding=_emb("totally different topic"), workspace_id="ws1")
    n1 = g.add_node(c1)
    n2 = g.add_node(c2)
    assert g.edges_of(n2.id) == []
    assert g.node_count == 2


async def test_workspace_isolation(bus) -> None:
    g = KnowledgeGraph(bus, similarity_threshold=0.8)
    e = _emb("shared")
    c1 = Chunk(document_id="/d1", text="shared", embedding=e, workspace_id="wsA")
    c2 = Chunk(document_id="/d2", text="shared", embedding=list(e), workspace_id="wsB")
    n1 = g.add_node(c1)
    n2 = g.add_node(c2)
    # different workspaces -> no cross-links despite identical embeddings
    assert g.edges_of(n2.id) == []


async def test_graph_subscribes_to_embedder(bus) -> None:
    g = KnowledgeGraph(bus, similarity_threshold=0.8)
    await g.start()

    updated = bus.wait_for(["graph.updated"])
    bus.publish(
        Event(
            type="chunk.embedded",
            source="embedder",
            payload={
                "chunk_id": "chunk-1",
                "document_path": "/ws/doc.md",
                "text": "graph chain text",
                "embedding": _emb("graph chain text"),
                "workspace_id": "ws1",
            },
        )
    )
    evt = await asyncio.wait_for(updated, timeout=2.0)

    assert evt.type == "graph.updated"
    assert evt.payload["workspace_id"] == "ws1"
    assert "node_id" in evt.payload
    assert isinstance(evt.payload["edges"], list)
    assert g.node_count == 1

    await g.stop()
    assert bus.subscriber_count("chunk.embedded") == 0


def test_cosine_edge_cases() -> None:
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0  # length mismatch
