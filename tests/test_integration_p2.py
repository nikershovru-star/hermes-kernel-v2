"""tests/test_integration_p2.py — end-to-end Knowledge Pipeline (real components).

Wires every P2 stage onto a single EventBus and drives a real tmp .md file
through the full chain:

    FileScanner.scan_once()  -> document.scanned
      -> DocumentParser      -> document.parsed
        -> DocumentChunker   -> chunk.created
          -> ChunkEmbedder   -> chunk.embedded
            -> KnowledgeGraph -> graph.updated

Assertions verify the graph received a node, embeddings drove edge creation, and
the workspace_id survived every hop of the chain. No mocks — real components.
"""

import asyncio

import pytest

from kernel.bus import EventBus
from kernel.chunker import DocumentChunker
from kernel.embedder import ChunkEmbedder
from kernel.graph import KnowledgeGraph
from kernel.parser import DocumentParser
from kernel.scanner import FileScanner


async def test_knowledge_pipeline_end_to_end(tmp_path) -> None:
    workspace_id = "ws-integration"
    bus = EventBus()

    # small chunk_size so a single doc yields several chunks -> similarity edges
    parser = DocumentParser(bus)
    chunker = DocumentChunker(bus, chunk_size=200, overlap=50)
    embedder = ChunkEmbedder(bus, backend="hash")
    graph = KnowledgeGraph(bus, similarity_threshold=0.8)

    await parser.start()
    await chunker.start()
    await embedder.start()
    await graph.start()

    # a real markdown file with repeated content -> similar chunks -> edges
    doc = tmp_path / "knowledge.md"
    doc.write_text(
        "# Knowledge Base\n\n" + ("The kernel is event-driven and async. " * 40),
        encoding="utf-8",
    )

    scanner = FileScanner(
        workspace_id=workspace_id, paths=[tmp_path], bus=bus, extensions=[".md"]
    )

    # collect every graph.updated event the pipeline emits
    updates: list = []
    done = asyncio.Event()

    async def collect(event):
        updates.append(event)
        done.set()

    bus.subscribe("graph.updated", collect)

    # kick off the chain
    scan_events = scanner.scan_once()
    assert len(scan_events) == 1  # one .md discovered

    # wait until the chain reaches the graph (bounded)
    try:
        await asyncio.wait_for(done.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("pipeline did not reach graph.updated in time")

    # let any remaining fan-out chunks settle
    await asyncio.sleep(0.2)

    # --- assertions: the chain actually built a graph ---------------------
    assert graph.node_count >= 1, "graph received no nodes"
    assert len(updates) >= 1, "no graph.updated events emitted"

    # workspace_id propagated end-to-end (scanner -> ... -> graph)
    for evt in updates:
        assert evt.payload["workspace_id"] == workspace_id
        assert "node_id" in evt.payload
        assert isinstance(evt.payload["edges"], list)

    # with repeated content and >1 chunk, at least one node should have an edge
    if graph.node_count >= 2:
        total_edges = sum(len(graph.edges_of(nid)) for nid in graph._nodes)
        assert total_edges >= 1, "similar chunks produced no edges"

    await parser.stop()
    await chunker.stop()
    await embedder.stop()
    await graph.stop()
