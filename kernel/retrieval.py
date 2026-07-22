"""kernel/retrieval.py — persistent vector search over stored embeddings (P? B).

A lightweight ``KnowledgeRetrievalService`` that indexes embedded
``KnowledgeNode`` entities and answers cosine-similarity queries. It reuses the
existing ``PersistenceRegistry`` for durable storage (the embedding travels in
``node.properties["embedding"]``) and keeps an in-memory embedding cache for
fast search — no external vector DB / numpy needed, keeping the dependency
surface minimal (the axis contract from ADR-001).

Search is exact cosine over the cached embeddings; for kernel-scale corpora
this is fine. A production deployment can swap the index backend without
changing the query API.

AXIS CONTRACT: depends on kernel.domain (KnowledgeNode) + kernel.persistence
(PersistenceRegistry) + kernel.bus (EventBus).
"""

from __future__ import annotations

import logging
from typing import Optional

from kernel.bus import EventBus
from kernel.domain import KnowledgeNode
from kernel.graph import cosine_similarity
from kernel.persistence import PersistenceRegistry

logger = logging.getLogger("hermes.retrieval")


class KnowledgeRetrievalService:
    """Durable, workspace-scoped vector index over KnowledgeNodes."""

    def __init__(self, persistence: PersistenceRegistry, bus: EventBus) -> None:
        self._persistence = persistence
        self._bus = bus
        # node_id -> (workspace_id, embedding)
        self._index: dict[str, tuple[str, list[float]]] = {}
        self._sub_id = None

    # -- indexing --------------------------------------------------------- #
    def index(self, node: KnowledgeNode) -> None:
        """Add/update a node in the in-memory index (embedding from properties)."""
        embedding = node.properties.get("embedding") or []
        self._index[node.id] = (node.workspace_id, list(embedding))

    async def index_and_persist(self, node: KnowledgeNode) -> str:
        """Index in memory AND save durably (embedding carried in properties)."""
        self.index(node)
        node.properties["embedding"] = node.properties.get("embedding") or []
        return await self._persistence.save(node)

    # -- querying --------------------------------------------------------- #
    def query(
        self, embedding: list[float], workspace_id: str, top_k: int = 5
    ) -> list[tuple[str, float]]:
        """Return [(node_id, score), ...] top_k by cosine, workspace-scoped."""
        scored: list[tuple[str, float]] = []
        for node_id, (ws, emb) in self._index.items():
            if ws != workspace_id:
                continue
            scored.append((node_id, cosine_similarity(embedding, emb)))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:top_k]

    def query_node(
        self, node: KnowledgeNode, top_k: int = 5
    ) -> list[tuple[str, float]]:
        """Convenience: query by another node's embedding."""
        return self.query(
            node.properties.get("embedding") or [], node.workspace_id, top_k
        )

    # -- durability ------------------------------------------------------- #
    async def load_from_persistence(self, workspace_id: str) -> int:
        """Load all KnowledgeNodes for a workspace into the index."""
        nodes = await self._persistence.list(workspace_id, entity_type="KnowledgeNode")
        loaded = 0
        for n in nodes:
            self.index(n)
            loaded += 1
        return loaded

    # -- event wiring ----------------------------------------------------- #
    async def _on_graph_updated(self, event) -> None:
        """Index a freshly-created graph node from a graph.updated event."""
        payload = event.payload
        node_id = payload.get("node_id")
        if node_id is None:
            return
        node = await self._persistence.get(node_id)
        if isinstance(node, KnowledgeNode):
            self.index(node)

    async def start(self) -> None:
        """Subscribe to ``graph.updated`` so new nodes are auto-indexed."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe("graph.updated", self._on_graph_updated)

    async def stop(self) -> None:
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None
