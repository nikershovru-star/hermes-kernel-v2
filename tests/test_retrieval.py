"""tests/test_retrieval.py — KnowledgeRetrievalService vector search (variant B)."""

import pytest

from kernel.bus import EventBus
from kernel.domain import KnowledgeNode
from kernel.persistence import PersistenceRegistry
from kernel.retrieval import KnowledgeRetrievalService


def _node(node_id: str, ws: str, embedding: list[float], text: str = "x") -> KnowledgeNode:
    return KnowledgeNode(
        id=node_id,
        label=text[:80],
        type="chunk",
        domain=ws,
        workspace_id=ws,
        properties={"embedding": embedding, "text": text},
    )


@pytest.fixture
def svc() -> tuple[KnowledgeRetrievalService, PersistenceRegistry, EventBus]:
    persistence = PersistenceRegistry(":memory:")
    bus = EventBus()
    return KnowledgeRetrievalService(persistence, bus), persistence, bus


def test_index_and_query(svc) -> None:
    s, _, _ = svc
    a = _node("n1", "ws1", [1.0, 0.0, 0.0])
    b = _node("n2", "ws1", [0.9, 0.1, 0.0])
    c = _node("n3", "ws1", [0.0, 1.0, 0.0])
    for n in (a, b, c):
        s.index(n)
    # query with vector close to a/b -> b should rank highest after a
    res = s.query([1.0, 0.0, 0.0], "ws1", top_k=2)
    assert res[0][0] == "n1"
    assert res[1][0] == "n2"
    # n1 is an exact match (score 1.0); n2 is the next closest
    assert res[0][1] > res[1][1]


def test_workspace_isolation(svc) -> None:
    s, _, _ = svc
    a = _node("n1", "wsA", [1.0, 0.0])
    b = _node("n2", "wsB", [1.0, 0.0])  # identical embedding, other ws
    s.index(a)
    s.index(b)
    # query in wsA must not surface the wsB node
    res = s.query([1.0, 0.0], "wsA", top_k=5)
    assert all(nid == "n1" for nid, _ in res)
    assert len(res) == 1


async def test_persist_and_reload(svc) -> None:
    s, persistence, _ = svc
    a = _node("n1", "ws1", [1.0, 0.0, 0.0], "alpha")
    b = _node("n2", "ws1", [0.0, 1.0, 0.0], "beta")
    await s.index_and_persist(a)
    await s.index_and_persist(b)

    # fresh service reloads from persistence
    s2 = KnowledgeRetrievalService(persistence, EventBus())
    loaded = await s2.load_from_persistence("ws1")
    assert loaded == 2
    res = s2.query([1.0, 0.0, 0.0], "ws1", top_k=1)
    assert res[0][0] == "n1"


async def test_auto_index_on_graph_event(svc) -> None:
    s, persistence, bus = svc
    await s.start()
    # persist a node, then emit graph.updated -> service should index it
    n = _node("nX", "ws1", [0.5, 0.5])
    await persistence.save(n)
    bus.publish(
        __import__("kernel.domain", fromlist=["Event"]).Event(
            type="graph.updated",
            source="graph",
            payload={"node_id": "nX", "edges": [], "workspace_id": "ws1"},
        )
    )
    # give the subscriber task a moment
    import asyncio

    await asyncio.sleep(0.1)
    res = s.query([0.5, 0.5], "ws1", top_k=1)
    assert res and res[0][0] == "nX"
    await s.stop()
