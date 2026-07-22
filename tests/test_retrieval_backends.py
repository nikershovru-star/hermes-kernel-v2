"""tests/test_retrieval_backends.py — Retrieval backends (ADR-009).

Replaces the old test_retrieval.py: the public API is now backend-agnostic and
async.  MemoryBackend tests always run; Faiss/SQLite-VSS tests skip cleanly when
the optional deps are absent.
"""

import math

import pytest

from kernel.bus import EventBus
from kernel.domain import KnowledgeNode
from kernel.persistence import PersistenceRegistry
from kernel.retrieval import KnowledgeRetrievalService
from kernel.retrieval_backends import BaseRetrievalBackend, MemoryBackend


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


# -- service integration (backend-agnostic, default MemoryBackend) ------- #
async def test_index_and_query(svc) -> None:
    s, _, _ = svc
    await s.index_and_persist(_node("n1", "ws1", [1.0, 0.0, 0.0]))
    await s.index_and_persist(_node("n2", "ws1", [0.9, 0.1, 0.0]))
    res = await s.query([1.0, 0.0, 0.0], "ws1", top_k=2)
    assert res[0][0] == "n1" and res[1][0] == "n2"


async def test_workspace_isolation(svc) -> None:
    s, _, _ = svc
    await s.index_and_persist(_node("n1", "wsA", [1.0, 0.0]))
    await s.index_and_persist(_node("n2", "wsB", [1.0, 0.0]))
    res = await s.query([1.0, 0.0], "wsA", top_k=5)
    assert len(res) == 1 and res[0][0] == "n1"


async def test_persist_and_reload(svc) -> None:
    s, persistence, _ = svc
    await s.index_and_persist(_node("n1", "ws1", [1.0, 0.0, 0.0], "alpha"))
    await s.index_and_persist(_node("n2", "ws1", [0.0, 1.0, 0.0], "beta"))
    s2 = KnowledgeRetrievalService(persistence, EventBus())
    loaded = await s2.load_from_persistence("ws1")
    assert loaded == 2
    res = await s2.query([1.0, 0.0, 0.0], "ws1", top_k=1)
    assert res[0][0] == "n1"


async def test_auto_index_on_graph_event(svc) -> None:
    s, persistence, bus = svc
    await s.start()
    n = _node("nX", "ws1", [0.5, 0.5])
    await persistence.save(n)
    bus.publish(
        __import__("kernel.domain", fromlist=["Event"]).Event(
            type="graph.updated", source="graph", payload={"node_id": "nX"}
        )
    )
    import asyncio

    await asyncio.sleep(0.1)
    res = await s.query([0.5, 0.5], "ws1", top_k=1)
    assert res and res[0][0] == "nX"
    await s.stop()


# -- MemoryBackend unit tests -------------------------------------------- #
class TestMemoryBackend:
    async def test_add_and_query(self) -> None:
        be = MemoryBackend()
        await be.add("n1", [1.0, 0.0, 0.0], "ws1")
        result = await be.query([1.0, 0.0, 0.0], "ws1", top_k=3)
        assert len(result) == 1 and result[0][0] == "n1"
        assert math.isclose(result[0][1], 1.0, rel_tol=1e-5)

    async def test_workspace_isolation(self) -> None:
        be = MemoryBackend()
        await be.add("n1", [1.0, 0.0, 0.0], "ws1")
        await be.add("n2", [1.0, 0.0, 0.0], "ws2")
        result = await be.query([1.0, 0.0, 0.0], "ws1", top_k=3)
        assert len(result) == 1 and result[0][0] == "n1"

    async def test_query_ranking(self) -> None:
        be = MemoryBackend()
        await be.add("n1", [1.0, 0.0, 0.0], "ws1")
        await be.add("n2", [0.0, 1.0, 0.0], "ws1")
        result = await be.query([1.0, 0.0, 0.0], "ws1", top_k=2)
        assert result[0][0] == "n1" and result[1][0] == "n2"
        assert result[0][1] > result[1][1]

    async def test_remove(self) -> None:
        be = MemoryBackend()
        await be.add("n1", [1.0, 0.0, 0.0], "ws1")
        await be.remove("n1", "ws1")
        assert await be.query([1.0, 0.0, 0.0], "ws1", top_k=3) == []

    async def test_clear_workspace(self) -> None:
        be = MemoryBackend()
        await be.add("n1", [1.0, 0.0, 0.0], "ws1")
        await be.add("n2", [1.0, 0.0, 0.0], "ws2")
        await be.clear_workspace("ws1")
        assert await be.query([1.0, 0.0, 0.0], "ws1", top_k=3) == []
        assert len(await be.query([1.0, 0.0, 0.0], "ws2", top_k=3)) == 1

    async def test_top_k_limits(self) -> None:
        be = MemoryBackend()
        for i in range(10):
            await be.add(f"n{i}", [1.0, 0.0, 0.0], "ws1")
        assert len(await be.query([1.0, 0.0, 0.0], "ws1", top_k=3)) == 3

    def test_is_backend(self) -> None:
        assert isinstance(MemoryBackend(), BaseRetrievalBackend)


# -- FaissBackend (skipped without faiss) -------------------------------- #
@pytest.fixture
def faiss_backend(tmp_path):
    pytest.importorskip("faiss")
    from kernel.retrieval_backends import FaissBackend

    return FaissBackend(persist_dir=tmp_path, embedding_dim=3)


class TestFaissBackend:
    async def test_add_and_query(self, faiss_backend) -> None:
        await faiss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        result = await faiss_backend.query([1.0, 0.0, 0.0], "ws1", top_k=3)
        assert len(result) == 1 and result[0][0] == "n1" and result[0][1] > 0.99

    async def test_workspace_isolation(self, faiss_backend) -> None:
        await faiss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        await faiss_backend.add("n2", [1.0, 0.0, 0.0], "ws2")
        result = await faiss_backend.query([1.0, 0.0, 0.0], "ws1", top_k=3)
        assert len(result) == 1 and result[0][0] == "n1"

    async def test_persist_and_load(self, faiss_backend, tmp_path) -> None:
        await faiss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        await faiss_backend.persist()
        from kernel.retrieval_backends import FaissBackend

        be2 = FaissBackend(persist_dir=tmp_path, embedding_dim=3)
        result = await be2.query([1.0, 0.0, 0.0], "ws1", top_k=3)
        assert len(result) == 1 and result[0][0] == "n1"

    async def test_remove(self, faiss_backend) -> None:
        await faiss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        await faiss_backend.remove("n1", "ws1")
        assert await faiss_backend.query([1.0, 0.0, 0.0], "ws1", top_k=3) == []

    async def test_clear_workspace(self, faiss_backend) -> None:
        await faiss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        await faiss_backend.add("n2", [1.0, 0.0, 0.0], "ws2")
        await faiss_backend.clear_workspace("ws1")
        assert await faiss_backend.query([1.0, 0.0, 0.0], "ws1", top_k=3) == []
        assert len(await faiss_backend.query([1.0, 0.0, 0.0], "ws2", top_k=3)) == 1


@pytest.fixture
def faiss_ivf_backend(tmp_path):
    pytest.importorskip("faiss")
    from kernel.retrieval_backends import FaissBackend

    return FaissBackend(persist_dir=tmp_path, embedding_dim=3, use_ivf=True, nlist=4)


class TestFaissIVFBackend:
    """Approximate index path (IVFFlat).  remove() rebuilds via reconstruct,
    which IVFFlat does not support — that path is documented as flat-only.
    IVF search is approximate (nprobe=1 default), so we assert the nearest
    node is returned rather than an exact count."""

    async def test_add_query(self, faiss_ivf_backend) -> None:
        await faiss_ivf_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        await faiss_ivf_backend.add("n2", [0.0, 1.0, 0.0], "ws1")
        result = await faiss_ivf_backend.query([1.0, 0.0, 0.0], "ws1", top_k=2)
        assert len(result) >= 1
        assert result[0][0] == "n1" and result[0][1] > 0.99


# -- SQLiteVSSBackend (skipped without sqlite_vss) ---------------------- #
@pytest.fixture
def vss_backend(tmp_path):
    pytest.importorskip("sqlite_vss")
    from kernel.retrieval_backends import SQLiteVSSBackend

    return SQLiteVSSBackend(db_path=tmp_path / "vss.db", embedding_dim=3)


class TestSQLiteVSSBackend:
    async def test_add_and_query(self, vss_backend) -> None:
        await vss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        result = await vss_backend.query([1.0, 0.0, 0.0], "ws1", top_k=3)
        assert len(result) == 1 and result[0][0] == "n1"

    async def test_workspace_isolation(self, vss_backend) -> None:
        await vss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        await vss_backend.add("n2", [1.0, 0.0, 0.0], "ws2")
        result = await vss_backend.query([1.0, 0.0, 0.0], "ws1", top_k=3)
        assert len(result) == 1 and result[0][0] == "n1"

    async def test_remove(self, vss_backend) -> None:
        await vss_backend.add("n1", [1.0, 0.0, 0.0], "ws1")
        await vss_backend.remove("n1", "ws1")
        assert await vss_backend.query([1.0, 0.0, 0.0], "ws1", top_k=3) == []
