"""tests/test_scanner.py — FileScanner discovery + workspace scoping."""

from pathlib import Path

import pytest

from kernel.bus import EventBus
from kernel.scanner import FileScanner


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def _name(event) -> str:
    return Path(event.payload["path"]).name


async def test_scan_once_finds_files(tmp_path, bus) -> None:
    (tmp_path / "a.md").write_text("# a")
    (tmp_path / "b.md").write_text("# b")

    scanner = FileScanner(
        workspace_id="ws1", paths=[tmp_path], bus=bus, extensions=[".md"]
    )
    events = scanner.scan_once()

    assert len(events) == 2
    assert all(e.type == "document.scanned" for e in events)
    assert sorted(_name(e) for e in events) == ["a.md", "b.md"]


async def test_scan_filters_extensions(tmp_path, bus) -> None:
    (tmp_path / "keep.md").write_text("# keep")
    (tmp_path / "drop.jpg").write_bytes(b"\xff\xd8\xff")

    scanner = FileScanner(
        workspace_id="ws1", paths=[tmp_path], bus=bus, extensions=[".md"]
    )
    events = scanner.scan_once()

    assert len(events) == 1
    assert _name(events[0]) == "keep.md"


async def test_scan_workspace_scoped(tmp_path, bus) -> None:
    (tmp_path / "note.md").write_text("# n")

    scanner = FileScanner(
        workspace_id="workspace-42", paths=[tmp_path], bus=bus, extensions=[".md"]
    )
    events = scanner.scan_once()

    assert len(events) == 1
    payload = events[0].payload
    assert payload["workspace_id"] == "workspace-42"
    assert payload["path"].endswith("note.md")
    assert payload["mime_type"] is not None
    assert events[0].source == "scanner:workspace-42"


async def test_scan_recursive(tmp_path, bus) -> None:
    (tmp_path / "top.md").write_text("# t")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# n")

    rec = FileScanner("ws", [tmp_path], bus, extensions=[".md"], recursive=True)
    flat = FileScanner("ws", [tmp_path], bus, extensions=[".md"], recursive=False)

    assert len(rec.scan_once()) == 2
    assert len(flat.scan_once()) == 1


async def test_start_stop_watch(tmp_path, bus) -> None:
    (tmp_path / "first.md").write_text("# 1")
    scanner = FileScanner(
        "ws", [tmp_path], bus, extensions=[".md"], interval=0.05
    )
    got = bus.wait_for(["document.scanned"])
    await scanner.start()
    evt = await __import__("asyncio").wait_for(got, timeout=2.0)
    assert evt.payload["workspace_id"] == "ws"
    await scanner.stop()
    assert scanner._task is None


async def test_missing_path_is_skipped(tmp_path, bus) -> None:
    scanner = FileScanner(
        "ws", [tmp_path / "does-not-exist"], bus, extensions=[".md"]
    )
    assert scanner.scan_once() == []
