"""kernel/marketplace_store.py — plugin/marketplace persistence (ADR-026).

In-memory CRUD + optional SQLite, mirroring ``PlanStore`` / ``GraphStore`` /
``SwarmStore``. Tables: ``packages``, ``catalog``, ``nodes``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from kernel.marketplace_domain import CatalogEntry, ClusterTopology, NodeInfo, PluginPackage, PluginStatus


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class MarketplaceStore:
    """Persist plugin packages, catalog entries and cluster nodes."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._mem_packages: dict[str, PluginPackage] = {}
        self._mem_catalog: dict[str, CatalogEntry] = {}
        self._mem_nodes: dict[str, NodeInfo] = {}
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_db()
            self._load_all()

    # -- schema ---------------------------------------------------------- #
    def _init_db(self) -> None:
        assert self._conn is not None
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS packages (package_id TEXT PRIMARY KEY, data TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS catalog (entry_id TEXT PRIMARY KEY, data TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS nodes (node_id TEXT PRIMARY KEY, data TEXT)"
        )
        self._conn.commit()

    def _load_all(self) -> None:
        assert self._conn is not None
        for row in self._conn.execute("SELECT package_id, data FROM packages"):
            self._mem_packages[row[0]] = PluginPackage.model_validate_json(row[1])
        for row in self._conn.execute("SELECT entry_id, data FROM catalog"):
            self._mem_catalog[row[0]] = CatalogEntry.model_validate_json(row[1])
        for row in self._conn.execute("SELECT node_id, data FROM nodes"):
            self._mem_nodes[row[0]] = NodeInfo.model_validate_json(row[1])

    # -- packages -------------------------------------------------------- #
    def put_package(self, pkg: PluginPackage) -> None:
        self._mem_packages[pkg.package_id] = pkg
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO packages (package_id, data) VALUES (?, ?)",
                (pkg.package_id, pkg.model_dump_json()),
            )
            self._conn.commit()

    def get_package(self, package_id: str) -> PluginPackage | None:
        return self._mem_packages.get(package_id)

    def delete_package(self, package_id: str) -> bool:
        removed = self._mem_packages.pop(package_id, None)
        if self._conn is not None:
            self._conn.execute("DELETE FROM packages WHERE package_id = ?", (package_id,))
            self._conn.commit()
        return removed is not None

    def list_packages(self, status: PluginStatus | None = None) -> list[PluginPackage]:
        pkgs = list(self._mem_packages.values())
        if status is not None:
            pkgs = [p for p in pkgs if p.status == status]
        return pkgs

    # -- catalog --------------------------------------------------------- #
    def put_catalog_entry(self, entry: CatalogEntry) -> None:
        self._mem_catalog[entry.entry_id] = entry
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO catalog (entry_id, data) VALUES (?, ?)",
                (entry.entry_id, entry.model_dump_json()),
            )
            self._conn.commit()

    def list_catalog(self) -> list[CatalogEntry]:
        return list(self._mem_catalog.values())

    def list_catalog_packages(self) -> list[PluginPackage]:
        """Reconstruct ``AVAILABLE`` packages from catalog entries."""
        out: list[PluginPackage] = []
        for e in self._mem_catalog.values():
            existing = self._mem_packages.get(e.package_id)
            if existing is not None:
                out.append(existing)
            else:
                out.append(
                    PluginPackage(
                        package_id=e.package_id,
                        name=e.package_id,
                        version="0.0.0",
                        source=__import__("kernel.marketplace_domain", fromlist=["PluginSource"]).PluginSource.MARKETPLACE,
                        entrypoint="",
                        status=PluginStatus.AVAILABLE,
                    )
                )
        return out

    # -- nodes ----------------------------------------------------------- #
    def put_node(self, node: NodeInfo) -> None:
        self._mem_nodes[node.node_id] = node
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO nodes (node_id, data) VALUES (?, ?)",
                (node.node_id, node.model_dump_json()),
            )
            self._conn.commit()

    def get_node(self, node_id: str) -> NodeInfo | None:
        return self._mem_nodes.get(node_id)

    def delete_node(self, node_id: str) -> bool:
        removed = self._mem_nodes.pop(node_id, None)
        if self._conn is not None:
            self._conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
            self._conn.commit()
        return removed is not None

    def list_nodes(self) -> list[NodeInfo]:
        return list(self._mem_nodes.values())

    def get_topology(self, cluster_id: str = "default") -> ClusterTopology:
        return ClusterTopology(cluster_id=cluster_id, nodes=dict(self._mem_nodes))
