"""kernel/retrieval_backends.py — Pluggable retrieval backends (ADR-009).

Backends are swappable implementations of vector search behind
``KnowledgeRetrievalService``.  The default ``MemoryBackend`` is zero-dep;
``FaissBackend`` and ``SQLiteVSSBackend`` are optional and lazily import their
heavy dependencies so the kernel remains runnable without them.

AXIS CONTRACT: depends only on stdlib.  Optional deps (faiss, sqlite-vss) are
lazily imported inside class constructors / methods.
"""

from __future__ import annotations

import json
import logging
import math
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes.retrieval.backends")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors; 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class BaseRetrievalBackend(ABC):
    """Abstract retrieval backend.  All methods are async."""

    @abstractmethod
    async def add(
        self, node_id: str, embedding: list[float], workspace_id: str
    ) -> None:
        """Index a node embedding."""

    @abstractmethod
    async def query(
        self, embedding: list[float], workspace_id: str, top_k: int
    ) -> list[tuple[str, float]]:
        """Return [(node_id, score), ...] sorted by descending similarity."""

    @abstractmethod
    async def remove(self, node_id: str, workspace_id: str) -> None:
        """Remove a node from the index."""

    @abstractmethod
    async def clear_workspace(self, workspace_id: str) -> None:
        """Drop every node belonging to *workspace_id*."""

    async def persist(self) -> None:
        """Flush state to durable storage (no-op for in-memory backends)."""
        return None

    async def load(self) -> None:
        """Rebuild state from durable storage (no-op for in-memory backends)."""
        return None


class MemoryBackend(BaseRetrievalBackend):
    """Brute-force cosine similarity over an in-memory dict.  Zero dependencies.

    The store key is ``(workspace_id, node_id)`` so isolation is enforced at the
    data level — there is no post-query filtering (ADR-007).
    """

    def __init__(self) -> None:
        # key: (workspace_id, node_id) -> value: embedding vector
        self._store: dict[tuple[str, str], list[float]] = {}

    async def add(
        self, node_id: str, embedding: list[float], workspace_id: str
    ) -> None:
        self._store[(workspace_id, node_id)] = list(embedding)

    async def query(
        self, embedding: list[float], workspace_id: str, top_k: int
    ) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for (ws, nid), vec in self._store.items():
            if ws != workspace_id:
                continue
            scored.append((nid, _cosine_similarity(embedding, vec)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    async def remove(self, node_id: str, workspace_id: str) -> None:
        self._store.pop((workspace_id, node_id), None)

    async def clear_workspace(self, workspace_id: str) -> None:
        for key in [k for k in self._store if k[0] == workspace_id]:
            del self._store[key]


class FaissBackend(BaseRetrievalBackend):
    """Faiss-based nearest-neighbour backend.  One index per workspace.

    Isolation is by construction (separate index per workspace).  Index files
    persist as ``{persist_dir}/{workspace_id}.faiss`` + a ``*.json`` id-map.

    Requires ``faiss-cpu`` (or ``faiss-gpu``)::

        pip install faiss-cpu
    """

    def __init__(
        self,
        *,
        persist_dir: str | os.PathLike[str] = ".hermes/faiss",
        embedding_dim: int = 384,
        use_ivf: bool = False,
        nlist: int = 100,
    ) -> None:
        self._persist_dir = Path(persist_dir)
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._dim = embedding_dim
        self._use_ivf = use_ivf
        self._nlist = nlist

        self._indices: dict[str, Any] = {}
        self._id_maps: dict[str, dict[int, str]] = {}
        self._counters: dict[str, int] = {}

        try:
            import faiss  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "FaissBackend requires 'faiss-cpu' (or 'faiss-gpu'). "
                "Install: pip install faiss-cpu"
            ) from exc
        self._faiss = faiss

    def _create_index(self) -> Any:
        if self._use_ivf:
            quantizer = self._faiss.IndexFlatIP(self._dim)
            base = self._faiss.IndexIVFFlat(
                quantizer, self._dim, self._nlist, self._faiss.METRIC_INNER_PRODUCT
            )
            dummy = self._faiss.rand((self._nlist * 39, self._dim))
            base.train(dummy)
            return self._faiss.IndexIDMap(base)
        # IndexIDMap wrapper enables add_with_ids (flat IP alone rejects it)
        return self._faiss.IndexIDMap(self._faiss.IndexFlatIP(self._dim))

    def _get_index(self, workspace_id: str) -> Any:
        if workspace_id not in self._indices:
            path = self._persist_dir / f"{workspace_id}.faiss"
            if path.exists():
                self._indices[workspace_id] = self._faiss.read_index(str(path))
                meta_path = path.with_suffix(".json")
                if meta_path.exists():
                    with open(meta_path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._id_maps[workspace_id] = {
                        int(k): v for k, v in data["id_map"].items()
                    }
                    self._counters[workspace_id] = data["counter"]
                else:
                    self._id_maps[workspace_id] = {}
                    self._counters[workspace_id] = 0
            else:
                self._indices[workspace_id] = self._create_index()
                self._id_maps[workspace_id] = {}
                self._counters[workspace_id] = 0
        return self._indices[workspace_id]

    async def add(
        self, node_id: str, embedding: list[float], workspace_id: str
    ) -> None:
        import numpy as np

        idx = self._get_index(workspace_id)
        fid = self._counters[workspace_id]
        self._counters[workspace_id] = fid + 1
        self._id_maps[workspace_id][fid] = node_id

        vec = np.array([embedding], dtype=np.float32)
        self._faiss.normalize_L2(vec)
        idx.add_with_ids(vec, np.array([fid], dtype=np.int64))

    async def query(
        self, embedding: list[float], workspace_id: str, top_k: int
    ) -> list[tuple[str, float]]:
        import numpy as np

        idx = self._get_index(workspace_id)
        if idx.ntotal == 0:
            return []

        vec = np.array([embedding], dtype=np.float32)
        self._faiss.normalize_L2(vec)
        distances, ids = idx.search(vec, min(top_k, idx.ntotal))

        result: list[tuple[str, float]] = []
        id_map = self._id_maps.get(workspace_id, {})
        for dist, fid in zip(distances[0], ids[0]):
            nid = id_map.get(int(fid))
            if nid is not None:
                result.append((nid, float(dist)))
        return result

    async def remove(self, node_id: str, workspace_id: str) -> None:
        """Faiss has no single-id removal; rebuild the workspace index without it.

        NOTE: full rebuild relies on ``reconstruct`` which works for IndexFlatIP
        but not for trained IVF indexes — use the default flat index for removal
        workloads.
        """
        old_idx = self._get_index(workspace_id)
        if old_idx.ntotal == 0:
            return

        id_map = self._id_maps.get(workspace_id, {})
        rev = {v: k for k, v in id_map.items()}
        fid = rev.get(node_id)
        if fid is None:
            return

        import numpy as np

        new_idx = self._create_index()
        new_map: dict[int, str] = {}
        new_counter = 0
        for old_fid, nid in id_map.items():
            if nid == node_id:
                continue
            vec = old_idx.reconstruct(int(old_fid))
            new_idx.add_with_ids(
                np.array([vec], dtype=np.float32),
                np.array([new_counter], dtype=np.int64),
            )
            new_map[new_counter] = nid
            new_counter += 1

        self._indices[workspace_id] = new_idx
        self._id_maps[workspace_id] = new_map
        self._counters[workspace_id] = new_counter

    async def clear_workspace(self, workspace_id: str) -> None:
        self._indices.pop(workspace_id, None)
        self._id_maps.pop(workspace_id, None)
        self._counters.pop(workspace_id, None)
        path = self._persist_dir / f"{workspace_id}.faiss"
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)

    async def persist(self) -> None:
        for ws_id, idx in self._indices.items():
            path = self._persist_dir / f"{ws_id}.faiss"
            self._faiss.write_index(idx, str(path))
            meta = {
                "id_map": {str(k): v for k, v in self._id_maps[ws_id].items()},
                "counter": self._counters[ws_id],
            }
            with open(path.with_suffix(".json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)

    async def load(self) -> None:
        # indices are lazy-loaded on first access; nothing to do here
        return None


class SQLiteVSSBackend(BaseRetrievalBackend):
    """sqlite-vss vector-search backend.  Native SQLite persistence.

    Stores embeddings in ``vss_nodes`` and creates a VSS virtual table per
    workspace.  Every query filters by ``workspace_id`` (ADR-007).

    Requires ``sqlite-vss``::

        pip install sqlite-vss
    """

    def __init__(
        self,
        *,
        db_path: str | os.PathLike[str] = ".hermes/retrieval_vss.db",
        embedding_dim: int = 384,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._dim = embedding_dim
        self._conn: Any = None

        try:
            import sqlite_vss  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "SQLiteVSSBackend requires 'sqlite-vss'. "
                "Install: pip install sqlite-vss"
            ) from exc
        self._sqlite_vss = sqlite_vss

    def _connect(self) -> Any:
        if self._conn is None:
            import sqlite3

            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.enable_load_extension(True)
            self._sqlite_vss.load(self._conn)
            self._conn.enable_load_extension(False)
            self._ensure_schema()
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._conn
        assert conn is not None
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vss_nodes (
                workspace_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                embedding BLOB NOT NULL,
                PRIMARY KEY (workspace_id, node_id)
            )
            """
        )
        conn.commit()

    def _vss_table(self, workspace_id: str) -> str:
        return f"vss_{workspace_id.replace('-', '_')}"

    def _ensure_vss_table(self, workspace_id: str) -> None:
        conn = self._connect()
        table = self._vss_table(workspace_id)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vss0(embedding({self._dim}))"
        )
        conn.commit()

    async def add(
        self, node_id: str, embedding: list[float], workspace_id: str
    ) -> None:
        import struct

        conn = self._connect()
        self._ensure_vss_table(workspace_id)
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn.execute(
            "INSERT OR REPLACE INTO vss_nodes (workspace_id, node_id, embedding) VALUES (?, ?, ?)",
            (workspace_id, node_id, blob),
        )
        table = self._vss_table(workspace_id)
        conn.execute(
            f"DELETE FROM {table} WHERE rowid IN "
            f"(SELECT v.rowid FROM {table} v JOIN vss_nodes n ON v.rowid = n.rowid "
            f"WHERE n.workspace_id = ? AND n.node_id = ?)",
            (workspace_id, node_id),
        )
        conn.execute(
            f"INSERT INTO {table}(rowid, embedding) "
            f"SELECT rowid, embedding FROM vss_nodes WHERE workspace_id = ? AND node_id = ?",
            (workspace_id, node_id),
        )
        conn.commit()

    async def query(
        self, embedding: list[float], workspace_id: str, top_k: int
    ) -> list[tuple[str, float]]:
        import struct

        conn = self._connect()
        self._ensure_vss_table(workspace_id)
        table = self._vss_table(workspace_id)
        blob = struct.pack(f"{len(embedding)}f", *embedding)

        rows = conn.execute(
            f"""
            SELECT n.node_id, {table}.distance
            FROM {table} v
            JOIN vss_nodes n ON v.rowid = n.rowid
            WHERE n.workspace_id = ?
            ORDER BY v.distance ASC
            LIMIT ?
            """,
            (workspace_id, top_k),
        ).fetchall()

        # sqlite-vss distance is L2; convert to a similarity score (higher = closer)
        return [(nid, 1.0 / (1.0 + float(dist))) for nid, dist in rows]

    async def remove(self, node_id: str, workspace_id: str) -> None:
        conn = self._connect()
        table = self._vss_table(workspace_id)
        conn.execute(
            "DELETE FROM vss_nodes WHERE workspace_id = ? AND node_id = ?",
            (workspace_id, node_id),
        )
        conn.execute(
            f"DELETE FROM {table} WHERE rowid NOT IN "
            f"(SELECT rowid FROM vss_nodes WHERE workspace_id = ?)",
            (workspace_id,),
        )
        conn.commit()

    async def clear_workspace(self, workspace_id: str) -> None:
        conn = self._connect()
        table = self._vss_table(workspace_id)
        conn.execute("DELETE FROM vss_nodes WHERE workspace_id = ?", (workspace_id,))
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()

    async def persist(self) -> None:
        conn = self._connect()
        conn.execute("VACUUM")
        conn.commit()

    async def load(self) -> None:
        return None
