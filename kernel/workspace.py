"""kernel/workspace.py — WorkspaceRegistry (async CRUD, ADR-011 isolation).

AXIS CONTRACT: imports only kernel.domain (Workspace). No I/O. Mirrors the
async/lock design of ToolRegistry / CapabilityRegistry. A "default" workspace
is seeded on the first empty list() so every entity (which carries
workspace_id) has a sensible home.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from kernel.domain import Workspace

logger = logging.getLogger(__name__)


class WorkspaceRegistry:
    """Async CRUD for Workspace entities with a single active workspace."""

    DEFAULT_NAME = "default"

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._active_id: Optional[str] = None
        self._lock = asyncio.Lock()

    async def _ensure_default(self) -> None:
        """Seed the 'default' workspace when the registry is empty (first list)."""
        if not self._workspaces:
            ws = Workspace(name=self.DEFAULT_NAME, owner_id="system", settings={})
            self._workspaces[ws.id] = ws
            if self._active_id is None:
                self._active_id = ws.id

    async def create(
        self, name: str, owner_id: str, settings: Optional[dict[str, Any]] = None
    ) -> Workspace:
        async with self._lock:
            if any(w.name == name for w in self._workspaces.values()):
                raise ValueError(f"workspace name {name!r} already exists")
            ws = Workspace(name=name, owner_id=owner_id, settings=settings or {})
            self._workspaces[ws.id] = ws
            if self._active_id is None:
                self._active_id = ws.id
            return ws

    async def get(self, id: str) -> Optional[Workspace]:
        async with self._lock:
            return self._workspaces.get(id)

    async def get_by_name(self, name: str) -> Optional[Workspace]:
        async with self._lock:
            for w in self._workspaces.values():
                if w.name == name:
                    return w
            return None

    async def list(self) -> list[Workspace]:
        async with self._lock:
            await self._ensure_default()
            return list(self._workspaces.values())

    async def update(self, id: str, **fields: Any) -> Optional[Workspace]:
        """Update name/owner_id/settings. Nonexistent id -> None.

        Changing name to a name already used by another workspace -> ValueError.
        """
        async with self._lock:
            ws = self._workspaces.get(id)
            if ws is None:
                return None
            if "name" in fields:
                new_name = fields["name"]
                if any(w.name == new_name and w.id != id for w in self._workspaces.values()):
                    raise ValueError(f"workspace name {new_name!r} already exists")
            allowed = {k: fields[k] for k in ("name", "owner_id", "settings") if k in fields}
            allowed["updated_at"] = datetime.now(timezone.utc).isoformat()
            updated = ws.model_copy(update=allowed)
            self._workspaces[id] = updated
            return updated

    async def delete(self, id: str) -> bool:
        """Delete a workspace. Nonexistent id -> False (never raises)."""
        async with self._lock:
            ws = self._workspaces.pop(id, None)
            if ws is None:
                return False
            if self._active_id == id:
                self._active_id = None
            return True

    async def set_active(self, workspace_id: str) -> Optional[str]:
        """Set the active workspace; returns the previous active id (may be None)."""
        async with self._lock:
            if workspace_id not in self._workspaces:
                raise ValueError(f"workspace {workspace_id!r} not found")
            prev = self._active_id
            self._active_id = workspace_id
            return prev
