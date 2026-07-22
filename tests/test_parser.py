"""tests/test_parser.py — DocumentParser extraction + bus chaining."""

import asyncio
from unittest.mock import patch

import pytest

from kernel.bus import EventBus
from kernel.domain import Event
from kernel.parser import DocumentParser


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


async def test_parse_md(tmp_path, bus) -> None:
    f = tmp_path / "note.md"
    f.write_text("# Title\n\nBody text.", encoding="utf-8")
    parser = DocumentParser(bus)
    content = parser.parse(f, "text/markdown")
    assert "# Title" in content
    assert "Body text." in content


async def test_parse_pdf(tmp_path, bus) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    parser = DocumentParser(bus)

    # mock pdfminer.high_level.extract_text so the test needs no real dependency
    import sys
    import types

    fake_high = types.ModuleType("pdfminer.high_level")
    fake_high.extract_text = lambda _p: "EXTRACTED PDF TEXT"
    fake_pkg = types.ModuleType("pdfminer")
    with patch.dict(sys.modules, {"pdfminer": fake_pkg, "pdfminer.high_level": fake_high}):
        content = parser.parse(f, "application/pdf")
    assert content == "EXTRACTED PDF TEXT"


async def test_parse_pdf_without_pdfminer(tmp_path, bus) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    parser = DocumentParser(bus)

    import builtins

    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("pdfminer"):
            raise ImportError("pdfminer blocked for test")
        return real_import(name, *a, **k)

    with patch.object(builtins, "__import__", blocked):
        content = parser.parse(f, "application/pdf")
    assert content == "[pdf:doc.pdf]"


async def test_parser_subscribes_to_scanner(tmp_path, bus) -> None:
    f = tmp_path / "chain.md"
    f.write_text("chained content", encoding="utf-8")

    parser = DocumentParser(bus)
    await parser.start()

    parsed = bus.wait_for(["document.parsed"])
    bus.publish(
        Event(
            type="document.scanned",
            source="scanner:ws1",
            payload={
                "path": str(f),
                "mime_type": "text/markdown",
                "workspace_id": "ws1",
            },
        )
    )
    evt = await asyncio.wait_for(parsed, timeout=2.0)

    assert evt.type == "document.parsed"
    assert evt.payload["content"] == "chained content"
    assert evt.payload["workspace_id"] == "ws1"
    assert evt.payload["path"] == str(f)

    await parser.stop()
    assert bus.subscriber_count("document.scanned") == 0


async def test_parse_unsupported_mime(tmp_path, bus) -> None:
    f = tmp_path / "image.bin"
    f.write_bytes(b"\x00\x01\x02")
    parser = DocumentParser(bus)
    assert parser.parse(f, "image/png") == ""
