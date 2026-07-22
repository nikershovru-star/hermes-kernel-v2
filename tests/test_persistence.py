"""tests/test_persistence.py — SQLite persistence + registry integration (P5.3)."""

import pytest

from kernel.domain import Chunk, Document, Relation, Workspace
from kernel.graph import KnowledgeGraph
from kernel.persistence import PersistenceRegistry
from kernel.scanner import FileScanner
from kernel.workspace import WorkspaceRegistry


@pytest.fixture
async def persistence() -> PersistenceRegistry:
    reg = PersistenceRegistry(":memory:")
    yield reg
    await reg.close()


async def test_save_get_document(persistence: PersistenceRegistry) -> None:
    doc = Document(
        source="/ws/note.md", format="md", content="hello", workspace_id="ws1"
    )
    await persistence.save(doc)
    got = await persistence.get(doc.id)
    assert got is not None
    assert got.id == doc.id
    assert got.content == "hello"
    assert got.workspace_id == "ws1"


async def test_list_workspace_isolated(persistence: PersistenceRegistry) -> None:
    await persistence.save(Document(source="a", format="md", content="x", workspace_id="ws1"))
    await persistence.save(Document(source="b", format="md", content="y", workspace_id="ws2"))
    await persistence.save(Document(source="c", format="md", content="z", workspace_id="ws1"))

    ws1 = await persistence.list("ws1")
    ws2 = await persistence.list("ws2")
    assert len(ws1) == 2
    assert len(ws2) == 1
    assert {d.source for d in ws1} == {"a", "c"}
    # cross-workspace leak must never happen
    assert all(d.workspace_id == "ws1" for d in ws1)


async def test_persist_workspace_registry(persistence: PersistenceRegistry) -> None:
    reg = WorkspaceRegistry()
    ws_a = await reg.create("alpha", "owner-1")
    ws_b = await reg.create("beta", "owner-2")
    assert await reg.save_to_db(persistence) == 2

    # a fresh registry loads from DB
    reg2 = WorkspaceRegistry()
    loaded = await reg2.load_from_db(persistence)
    assert loaded == 2
    assert (await reg2.get_by_name("alpha")).id == ws_a.id
    assert (await reg2.get_by_name("beta")).id == ws_b.id


async def test_persist_knowledge_graph(persistence: PersistenceRegistry) -> None:
    g = KnowledgeGraph(persistence, similarity_threshold=0.8)
    # build two similar nodes manually
    c1 = Chunk(document_id="/d1", text="same content here", embedding=[1.0, 0.0], workspace_id="ws1")
    c2 = Chunk(document_id="/d2", text="same content here", embedding=[1.0, 0.0], workspace_id="ws1")
    n1 = g.add_node(c1)
    n2 = g.add_node(c2)
    assert len(g.edges_of(n1.id)) >= 1  # similarity edge created

    saved = await g.persist_into(persistence, "ws1")
    assert saved >= 2  # at least 2 nodes + edges

    # fresh graph loads back
    g2 = KnowledgeGraph(persistence, similarity_threshold=0.8)
    loaded = await g2.load_from_db(persistence, "ws1")
    assert loaded == 2
    assert g2.node_count == 2
    assert n1.id in g2._nodes and n2.id in g2._nodes


async def test_idempotency(persistence: PersistenceRegistry) -> None:
    doc = Document(source="dup", format="md", content="v1", workspace_id="ws1")
    await persistence.save(doc)
    # re-save (update) must not create a duplicate row
    doc2 = Document(id=doc.id, source="dup", format="md", content="v2", workspace_id="ws1")
    await persistence.save(doc2)

    rows = await persistence.list("ws1")
    assert len(rows) == 1
    assert rows[0].content == "v2"  # updated, not duplicated
    assert await persistence.get(doc.id) is not None


async def test_scanner_skip_persisted(tmp_path, persistence: PersistenceRegistry) -> None:
    from kernel.bus import EventBus

    # file-backed DB so the marker table is shared with the scanner's sync check
    file_db = PersistenceRegistry(str(tmp_path / "store.db"))
    (tmp_path / "a.md").write_text("# A", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B", encoding="utf-8")

    bus = EventBus()
    # mark a.md as already scanned
    await file_db.mark("scanned:" + str((tmp_path / "a.md").resolve()), "ws1")

    scanner = FileScanner(
        workspace_id="ws1", paths=[tmp_path], bus=bus, persistence=file_db
    )
    events = scanner.scan_once()
    paths = {e.payload["path"] for e in events}
    # a.md skipped (persisted marker), b.md emitted
    assert str((tmp_path / "b.md").resolve()) in paths
    assert str((tmp_path / "a.md").resolve()) not in paths
    await file_db.close()
