"""tests/test_artifact.py — unified Artifact entity (ADR-016)."""

from __future__ import annotations

from kernel.domain import Artifact


def test_artifact_defaults() -> None:
    a = Artifact(type="text", content="hello")
    assert a.format == "text"
    assert a.provenance == []
    assert a.source is None
    assert a.workspace_id == "default"


def test_artifact_content_any_type() -> None:
    # content: Any — str, bytes, dict all valid
    a_str = Artifact(type="text", content="x")
    a_bytes = Artifact(type="screenshot", content=b"png-bytes", format="png")
    a_dict = Artifact(type="result", content={"k": 1}, format="json")
    assert a_str.content == "x"
    assert a_bytes.content == b"png-bytes"
    assert a_dict.content == {"k": 1}


def test_artifact_provenance_chain() -> None:
    a = Artifact(
        type="screenshot",
        content="base64",
        format="png",
        source="agent:browser",
        provenance=["cap:browser.screenshot", "task:123"],
    )
    assert a.provenance == ["cap:browser.screenshot", "task:123"]


def test_artifact_persistence_roundtrip_fields() -> None:
    # exercise the fields the capability executor relies on
    a = Artifact(type="code", content="print(1)", format="py", source="agent:coder")
    assert a.type == "code" and a.format == "py"
