"""tests/test_human_profile.py — HumanProfileStore CRUD + persistence (ADR-022)."""

from __future__ import annotations

import pytest
from kernel.domain import BehaviorProfile, HumanBehaviorProfile
from plugins.builtin.desktop_control.human_profile import HumanProfileStore


def test_create_and_get() -> None:
    store = HumanProfileStore()
    p = store.create("u1", "Nikita")
    assert isinstance(p, HumanBehaviorProfile)
    assert store.get("u1").name == "Nikita"
    assert store.get("missing") is None


def test_create_duplicate_raises() -> None:
    store = HumanProfileStore()
    store.create("u1", "A")
    with pytest.raises(ValueError):
        store.create("u1", "B")


def test_create_with_custom_behavior() -> None:
    store = HumanProfileStore()
    b = BehaviorProfile(typing_wpm=90, mouse_curve="linear")
    p = store.create("fast", "Speedy", behavior=b)
    assert p.behavior.typing_wpm == 90
    assert p.behavior.mouse_curve == "linear"


def test_list() -> None:
    store = HumanProfileStore()
    store.create("a", "A")
    store.create("b", "B")
    assert len(store.list()) == 2


def test_update_changes_behavior_and_timestamp() -> None:
    store = HumanProfileStore()
    p = store.create("u1", "N")
    old_updated = p.updated_at
    new_b = BehaviorProfile(typing_wpm=120)
    updated = store.update("u1", new_b)
    assert updated.behavior.typing_wpm == 120
    assert updated.updated_at >= old_updated


def test_update_missing_raises() -> None:
    store = HumanProfileStore()
    with pytest.raises(KeyError):
        store.update("nope", BehaviorProfile())


def test_delete() -> None:
    store = HumanProfileStore()
    store.create("u1", "N")
    assert store.delete("u1") is True
    assert store.delete("u1") is False
    assert store.get("u1") is None


def test_get_or_default_creates() -> None:
    store = HumanProfileStore()
    p = store.get_or_default("new")
    assert p.profile_id == "new"
    # second call returns the same, does not raise
    assert store.get_or_default("new").profile_id == "new"


def test_default_profile_serialization_roundtrip() -> None:
    p = HumanBehaviorProfile(profile_id="x", name="X")
    dumped = p.model_dump_json()
    restored = HumanBehaviorProfile.model_validate_json(dumped)
    assert restored.profile_id == "x"
    assert restored.behavior.mouse_speed == 1.0


def test_sqlite_persistence(tmp_path) -> None:
    db = str(tmp_path / "profiles.db")
    store1 = HumanProfileStore(db_path=db)
    store1.create("u1", "Nikita", behavior=BehaviorProfile(typing_wpm=55))
    store1.update("u1", BehaviorProfile(typing_wpm=77))

    # New store instance loads from disk.
    store2 = HumanProfileStore(db_path=db)
    loaded = store2.get("u1")
    assert loaded is not None
    assert loaded.behavior.typing_wpm == 77

    # Delete persists too.
    store2.delete("u1")
    store3 = HumanProfileStore(db_path=db)
    assert store3.get("u1") is None
