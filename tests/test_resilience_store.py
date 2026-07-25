"""tests/test_resilience_store.py — ResilienceStore persistence tests (ADR-031).

Covers the SQLite backend and the pure in-memory fallback. Uses a tempdir DB
file; the connection is closed via reload(None) before teardown to avoid a
Windows file-lock on cleanup.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from kernel.resilience_domain import ResilienceDeadLetterEntry
from kernel.resilience_store import ResilienceStore


@pytest.fixture()
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.remove(path)
    except (OSError, PermissionError):
        pass


def _dle(entry_id="d1", task=None, error="boom", attempts=0, status="pending"):
    return ResilienceDeadLetterEntry(
        entry_id=entry_id,
        original_task=task or {"k": "v"},
        error=error,
        attempts=attempts,
        enqueued_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        status=status,
    )


# 1 — SQLite roundtrip circuits/retries/DLQ
def test_sqlite_roundtrip(db_path):
    st = ResilienceStore(db_path=db_path)
    st.put_circuit("c", "open", 3, datetime(2026, 7, 25, tzinfo=timezone.utc), '{"name":"c"}')
    st.put_retry("t1", 1, 100, "err", datetime(2026, 7, 25, tzinfo=timezone.utc))
    st.put_dead_letter(_dle(entry_id="d1", task={"a": 1}))
    circuit = st.get_circuit("c")
    assert circuit["state"] == "open" and circuit["failure_count"] == 3
    assert len(st.list_retries("t1")) == 1
    got = st.get_dead_letter("d1")
    assert got is not None and got.original_task == {"a": 1}
    st.reload(None)


# 2 — list_dead_letter filtered by status
def test_list_dead_letter_filtered(db_path):
    st = ResilienceStore(db_path=db_path)
    st.put_dead_letter(_dle(entry_id="d1", status="pending"))
    st.put_dead_letter(_dle(entry_id="d2", status="pending"))
    st.put_dead_letter(_dle(entry_id="d3", status="replayed"))
    assert len(st.list_dead_letter("pending")) == 2
    assert len(st.list_dead_letter("replayed")) == 1
    assert len(st.list_dead_letter(None)) == 3  # all
    st.reload(None)


# 3 — update_dead_letter_status transitions
def test_update_dead_letter_status(db_path):
    st = ResilienceStore(db_path=db_path)
    st.put_dead_letter(_dle(entry_id="d1", status="pending"))
    la = datetime(2026, 7, 25, 13, 0, tzinfo=timezone.utc)
    assert st.update_dead_letter_status("d1", "replayed", la) is True
    got = st.get_dead_letter("d1")
    assert got.status == "replayed" and got.last_attempt == la
    assert st.update_dead_letter_status("missing", "discarded") is False
    st.reload(None)


# 4 — in-memory fallback (db_path=None)
def test_in_memory_fallback():
    st = ResilienceStore()
    st.put_circuit("c", "closed", 0, None, "{}")
    st.put_retry("t1", 1, 50, "e", datetime.now(timezone.utc))
    st.put_dead_letter(_dle(entry_id="d1"))
    assert st.get_circuit("c")["state"] == "closed"
    assert len(st.list_retries("t1")) == 1
    assert len(st.list_dead_letter("pending")) == 1
    assert st._conn is None  # never opened a connection


# 5 — repo-reload on db_path
def test_repo_reload(db_path):
    st = ResilienceStore(db_path=db_path)
    st.put_dead_letter(_dle(entry_id="persist"))
    st.reload()  # re-open same path
    assert st.get_dead_letter("persist") is not None
    st.reload(None)


# 6 — list_retries filtered by task_id
def test_list_retries_filtered(db_path):
    st = ResilienceStore(db_path=db_path)
    base = datetime(2026, 7, 25, tzinfo=timezone.utc)
    st.put_retry("t1", 1, 100, "e", base)
    st.put_retry("t1", 2, 200, "e", base + timedelta(seconds=1))
    st.put_retry("t2", 1, 100, "e", base)
    assert len(st.list_retries("t1")) == 2
    assert len(st.list_retries("t2")) == 1
    assert len(st.list_retries()) == 3  # all
    st.reload(None)


# 7 — put_dead_letter upsert (attempts/status overwrite, no duplicate row)
def test_dead_letter_upsert(db_path):
    st = ResilienceStore(db_path=db_path)
    st.put_dead_letter(_dle(entry_id="d1", attempts=0, status="pending"))
    st.put_dead_letter(_dle(entry_id="d1", attempts=2, status="pending"))
    assert len(st.list_dead_letter(None)) == 1  # upsert, not append
    assert st.get_dead_letter("d1").attempts == 2
    st.reload(None)


# 8 — original_task JSON survives roundtrip with nested structure
def test_original_task_json_roundtrip(db_path):
    st = ResilienceStore(db_path=db_path)
    task = {"workflow_id": "wf1", "params": {"nested": [1, 2, 3], "flag": True}}
    st.put_dead_letter(_dle(entry_id="d1", task=task))
    st.reload()  # force read from disk
    got = st.get_dead_letter("d1")
    assert got.original_task == task
    st.reload(None)


# 9 — in-memory fallback: get_dead_letter / update_dead_letter_status / list_retries(no-task)
def test_in_memory_get_and_update():
    st = ResilienceStore()  # no db_path → pure in-memory
    st.put_dead_letter(_dle(entry_id="m1", task={"x": 1}))
    got = st.get_dead_letter("m1")
    assert got is not None and got.original_task == {"x": 1}
    assert st.get_dead_letter("missing") is None
    la = datetime(2026, 7, 25, 13, 0, tzinfo=timezone.utc)
    assert st.update_dead_letter_status("m1", "replayed", la) is True
    assert st.get_dead_letter("m1").status == "replayed"
    assert st.update_dead_letter_status("ghost", "discarded") is False
    # list_retries with no task_id filter + with filter (in-memory)
    base = datetime(2026, 7, 25, tzinfo=timezone.utc)
    st.put_retry("r1", 1, 100, "e", base)
    assert len(st.list_retries("r1")) == 1
    assert len(st.list_retries()) == 1
