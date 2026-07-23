"""plugins/builtin/human_emulation/profile_manager.py — HumanProfile CRUD.

Thin async wrapper over ``PersistenceRegistry`` for the ``HumanProfile`` entity.
Profiles are workspace-isolated (ADR-007): every call takes an explicit
``workspace_id`` (no thread-local / contextvar propagation).

AXIS CONTRACT: depends on kernel (domain, persistence). Never imports plugins.
"""

from __future__ import annotations

import logging
from typing import Any

from kernel.domain import HumanProfile
from kernel.persistence import PersistenceRegistry

logger = logging.getLogger("hermes.human.profile")


class ProfileManager:
    """Create / read / update / delete human profiles (workspace-scoped)."""

    def __init__(self, persistence: PersistenceRegistry) -> None:
        self._persistence = persistence

    async def create(self, workspace_id: str, **fields: Any) -> HumanProfile:
        """Persist a new profile under ``workspace_id`` and return it."""
        profile = HumanProfile(workspace_id=workspace_id, **fields)
        await self._persistence.save(profile)
        return profile

    async def get(self, workspace_id: str, profile_id: str) -> HumanProfile | None:
        """Fetch one profile by id, scoped to ``workspace_id``."""
        entity = await self._persistence.get(profile_id)
        if isinstance(entity, HumanProfile) and entity.workspace_id == workspace_id:
            return entity
        return None

    async def list(self, workspace_id: str) -> list[HumanProfile]:
        """List all profiles in ``workspace_id``."""
        rows = await self._persistence.list(workspace_id, "HumanProfile")
        return [r for r in rows if isinstance(r, HumanProfile)]

    async def delete(self, profile_id: str) -> bool:
        """Delete a profile by id (any workspace; callers scope by id)."""
        return await self._persistence.delete(profile_id)
