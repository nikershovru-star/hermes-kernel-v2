"""plugins/builtin/desktop_control/human_profile.py — HumanBehaviorProfile store (ADR-022).

CRUD + persistence for :class:`kernel.domain.HumanBehaviorProfile` behavior profiles.
Backing store is in-memory by default; passing a ``db_path`` persists profiles
to a small SQLite table (profile JSON blob keyed by ``profile_id``). No cloud
sync — single-node local persistence only (documented in ADR-022).

AXIS CONTRACT: depends on kernel.domain only (HumanBehaviorProfile / BehaviorProfile).
Never imports kernel.bus / kernel.events / plugins.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from kernel.domain import BehaviorProfile, HumanBehaviorProfile


class HumanProfileStore:
    """CRUD store for :class:`HumanBehaviorProfile` (in-memory + optional SQLite)."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._mem: dict[str, HumanBehaviorProfile] = {}
        if db_path is not None:
            self._init_db()
            self._load_all()

    # -- persistence ------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)  # type: ignore[arg-type]

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS human_profiles ("
                "profile_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            conn.commit()

    def _load_all(self) -> None:
        with self._connect() as conn:
            for pid, data in conn.execute("SELECT profile_id, data FROM human_profiles"):
                self._mem[pid] = HumanBehaviorProfile.model_validate_json(data)

    def _persist(self, profile: HumanBehaviorProfile) -> None:
        if self._db_path is None:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO human_profiles (profile_id, data) VALUES (?, ?)",
                (profile.profile_id, profile.model_dump_json()),
            )
            conn.commit()

    # -- CRUD ------------------------------------------------------------- #
    def create(
        self,
        profile_id: str,
        name: str,
        behavior: BehaviorProfile | None = None,
    ) -> HumanBehaviorProfile:
        if profile_id in self._mem:
            raise ValueError(f"profile {profile_id!r} already exists")
        profile = HumanBehaviorProfile(
            profile_id=profile_id,
            name=name,
            behavior=behavior or BehaviorProfile(),
        )
        self._mem[profile_id] = profile
        self._persist(profile)
        return profile

    def get(self, profile_id: str) -> HumanBehaviorProfile | None:
        return self._mem.get(profile_id)

    def list(self) -> list[HumanBehaviorProfile]:
        return list(self._mem.values())

    def update(self, profile_id: str, behavior: BehaviorProfile) -> HumanBehaviorProfile:
        existing = self._mem.get(profile_id)
        if existing is None:
            raise KeyError(profile_id)
        updated = existing.model_copy(
            update={"behavior": behavior, "updated_at": datetime.now(timezone.utc)}
        )
        self._mem[profile_id] = updated
        self._persist(updated)
        return updated

    def delete(self, profile_id: str) -> bool:
        if profile_id not in self._mem:
            return False
        del self._mem[profile_id]
        if self._db_path is not None:
            with self._connect() as conn:
                conn.execute("DELETE FROM human_profiles WHERE profile_id = ?", (profile_id,))
                conn.commit()
        return True

    def get_or_default(self, profile_id: str, name: str = "default") -> HumanBehaviorProfile:
        """Return the profile, creating a default one if it does not exist."""
        existing = self._mem.get(profile_id)
        if existing is not None:
            return existing
        return self.create(profile_id, name)


__all__ = ["HumanProfileStore"]
