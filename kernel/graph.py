"""kernel/graph.py — knowledge graph stage (final stage of P2).

Subscribes to ``chunk.embedded`` events, materialises each chunk as a
``KnowledgeNode``, links nodes whose embeddings are cosine-similar above a
threshold (``Relation`` edges), and republishes ``graph.updated``.

AXIS CONTRACT: depends on kernel.domain (KnowledgeNode, Relation, Chunk, Event)
+ kernel.bus (EventBus) only.

The graph is workspace-scoped: nodes carry ``domain = workspace_id`` (per
ADR-007 graph isolation) and similarity search never crosses workspaces.
Storage is an in-memory adjacency structure — a persistent backend is a future
concern (KnowledgeRetrievalService), the kernel only owns the live graph.
"""

from __future__ import annotations

import logging
import math

from kernel.bus import EventBus
from kernel.domain import Chunk, Event, KnowledgeNode, Relation

logger = logging.getLogger("hermes.graph")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class KnowledgeGraph:
    """In-memory, workspace-scoped similarity graph over embedded chunks."""

    def __init__(self, bus: EventBus, similarity_threshold: float = 0.8) -> None:
        self._bus = bus
        self.similarity_threshold = similarity_threshold
        self._sub_id = None
        # node_id -> KnowledgeNode
        self._nodes: dict[str, KnowledgeNode] = {}
        # node_id -> embedding
        self._embeddings: dict[str, list[float]] = {}
        # node_id -> list[Relation]
        self._edges: dict[str, list[Relation]] = {}

    # -- graph ops -------------------------------------------------------- #
    def add_node(self, chunk: Chunk) -> KnowledgeNode:
        """Materialise a chunk as a KnowledgeNode and link it to similar nodes."""
        embedding = chunk.embedding or []
        workspace_id = chunk.workspace_id
        node = KnowledgeNode(
            label=(chunk.text or "")[:80],
            type="chunk",
            domain=workspace_id,
            workspace_id=workspace_id,
            properties={
                "chunk_id": chunk.id,
                "text": chunk.text,
                "document_id": chunk.document_id,
            },
        )
        # find similar nodes BEFORE registering self (no self-edge)
        similar = self.find_similar(embedding, workspace_id=workspace_id)

        self._nodes[node.id] = node
        self._embeddings[node.id] = embedding
        self._edges[node.id] = []

        for other_id, score in similar:
            edge = Relation(
                source_id=node.id,
                target_id=other_id,
                type="similar_to",
                properties={"score": score},
                workspace_id=workspace_id,
            )
            self._edges[node.id].append(edge)
            # symmetric back-edge
            self._edges.setdefault(other_id, []).append(
                Relation(
                    source_id=other_id,
                    target_id=node.id,
                    type="similar_to",
                    properties={"score": score},
                    workspace_id=workspace_id,
                )
            )
        return node

    def find_similar(
        self, embedding: list[float], workspace_id: str | None = None
    ) -> list[tuple[str, float]]:
        """Return [(node_id, score), ...] above threshold, sorted desc.

        When ``workspace_id`` is given, only nodes in that workspace are
        considered (graph isolation).
        """
        out: list[tuple[str, float]] = []
        for node_id, other_emb in self._embeddings.items():
            if workspace_id is not None and self._nodes[node_id].domain != workspace_id:
                continue
            score = cosine_similarity(embedding, other_emb)
            if score >= self.similarity_threshold:
                out.append((node_id, score))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    def edges_of(self, node_id: str) -> list[Relation]:
        return self._edges.get(node_id, [])

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    # -- event wiring ----------------------------------------------------- #
    async def _on_embedded(self, event: Event) -> None:
        payload = event.payload
        try:
            chunk = Chunk(
                document_id=payload.get("document_path", ""),
                text=payload.get("text", ""),
                embedding=payload.get("embedding"),
                workspace_id=payload.get("workspace_id", "default"),
            )
            # carry through the originating chunk_id if present
            if payload.get("chunk_id"):
                chunk.id = payload["chunk_id"]
            node = self.add_node(chunk)
        except Exception:  # noqa: BLE001 — fault containment
            logger.exception("graph update failed for %s", payload.get("chunk_id"))
            return
        self._bus.publish(
            Event(
                type="graph.updated",
                source="graph",
                payload={
                    "node_id": node.id,
                    "edges": [e.target_id for e in self._edges[node.id]],
                    "workspace_id": node.workspace_id,
                },
            )
        )

    async def start(self) -> None:
        """Subscribe to ``chunk.embedded``."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe("chunk.embedded", self._on_embedded)

    async def stop(self) -> None:
        """Unsubscribe from the bus."""
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None
