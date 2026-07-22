"""kernel/retrieval.py — KnowledgeRetrievalService (ADR-008, ADR-009).

Durable, workspace-scoped vector search over ``KnowledgeNode`` embeddings. The
search backend is pluggable (``MemoryBackend``, ``FaissBackend``,
``SQLiteVSSBackend`` from ``kernel.retrieval_backends``); the default is the
zero-dep in-memory cosine backend.

Auto-indexes new nodes on the ``graph.updated`` event. Workspace isolation is
enforced by the active backend (no post-query filtering here).

AXIS CONTRACT: depends on kernel.domain (KnowledgeNode) + kernel.persistence
(PersistenceRegistry) + kernel.bus (EventBus) + kernel.retrieval_backends.
"""

from __future__ import annotations

import logging
from typing import Any

from kernel.bus import EventBus
from kernel.domain import KnowledgeNode
from kernel.persistence import PersistenceRegistry

from kernel.retrieval_backends import BaseRetrievalBackend, MemoryBackend

logger = logging.getLogger("hermes.retrieval")


class KnowledgeRetrievalService:
    """Workspace-scoped retrieval with a pluggable backend."""

    def __init__(
        self,
        persistence: PersistenceRegistry,
        bus: EventBus,
        backend: BaseRetrievalBackend | None = None,
    ) -> None:
        self._persistence = persistence
        self._bus = bus
        self._backend = backend or MemoryBackend()
        self._sub_id = None

    # -- indexing --------------------------------------------------------- #
    async def index_and_persist(self, node: KnowledgeNode) -> None:
        """Index *node* (embedding from ``node.properties['embedding']``) + persist."""
        embedding = node.properties.get("embedding") or []
        if not embedding:
            logger.warning("Node %s has no embedding; skipping index", node.id)
            return
        await self._backend.add(node.id, embedding, node.workspace_id)
        await self._persistence.save(node)

    # -- legacy/compat helpers (delegate to backend) --------------------- #
    async def index(self, node: KnowledgeNode) -> None:
        """Compat: index a node in the backend (no persistence write)."""
        embedding = node.properties.get("embedding") or []
        if embedding:
            await self._backend.add(node.id, embedding, node.workspace_id)

    # -- querying --------------------------------------------------------- #
    async def query(
        self, embedding: list[float], workspace_id: str, top_k: int = 5
    ) -> list[tuple[str, float]]:
        """Return [(node_id, similarity_score), ...] sorted descending."""
        return await self._backend.query(embedding, workspace_id, top_k)

    async def query_node(
        self, node: KnowledgeNode, top_k: int = 5
    ) -> list[tuple[str, float]]:
        """Convenience: query by another node's embedding."""
        return await self.query(
            node.properties.get("embedding") or [], node.workspace_id, top_k
        )

    async def remove(self, node_id: str, workspace_id: str) -> None:
        await self._backend.remove(node_id, workspace_id)

    async def clear_workspace(self, workspace_id: str) -> None:
        await self._backend.clear_workspace(workspace_id)

    # -- durability ------------------------------------------------------- #
    async def persist(self) -> None:
        await self._backend.persist()

    async def load_from_persistence(self, workspace_id: str) -> int:
        """Load all KnowledgeNodes for a workspace into the backend."""
        nodes = await self._persistence.list(workspace_id, entity_type="KnowledgeNode")
        loaded = 0
        for n in nodes:
            embedding = n.properties.get("embedding") or []
            if embedding:
                await self._backend.add(n.id, embedding, n.workspace_id)
                loaded += 1
        return loaded

    # -- event wiring ----------------------------------------------------- #
    async def _on_graph_updated(self, event: Any) -> None:
        """Index a freshly-created graph node from a graph.updated event."""
        payload = getattr(event, "payload", None) or {}
        node_id = payload.get("node_id")
        if not node_id:
            return
        node = await self._persistence.get(node_id)
        if isinstance(node, KnowledgeNode):
            await self.index_and_persist(node)

    async def start(self) -> None:
        """Subscribe to ``graph.updated`` so new nodes are auto-indexed."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe("graph.updated", self._on_graph_updated)

    async def stop(self) -> None:
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None
