"""tests/test_domain.py — domain entity contract & all 13 entities."""

import uuid

import pytest

from kernel import domain as d


def _make(entity_name: str) -> d.BaseEntity:
    cases = {
        "Document": dict(source="s", format="md", content="x"),
        "Chunk": dict(document_id="d1", text="t"),
        "KnowledgeNode": dict(label="L", type="T", domain="prog"),
        "Entity": dict(name="N", type="T", domain="prog"),
        "Relation": dict(source_id="a", target_id="b", type="rel"),
        "Task": dict(name="n"),
        "Event": dict(type="evt"),
        "Memory": dict(type="session", content="c"),
        "Tool": dict(name="t", capability="hermes.search"),
        "Agent": dict(name="a"),
        "Workflow": dict(name="w"),
        "Project": dict(name="p", domain="prog"),
        "Artifact": dict(type="note", content="c"),
        "Capability": dict(name="hermes.search", description="d", tools=["filesystem"], schema={}),
        "Workspace": dict(name="ws", owner_id="u1", settings={}),
        "Dataset": dict(name="ds", source="s3", format="csv"),
        "Conversation": dict(title="t", messages=[{"role": "user", "content": "x"}]),
        "User": dict(username="alice", hashed_password="salt$hash", roles=["admin"]),
        "HumanProfile": dict(name="hp", typing_speed_wpm=60, typo_rate=0.02),
        "BrowserSession": dict(profile_id="p1", url="about:blank", status="idle"),
        "ActionLog": dict(session_id="s1", action_type="click", target="#btn"),
    }
    return d.ENTITY_TYPES[entity_name](**cases[entity_name])


@pytest.mark.parametrize("name", list(d.ENTITY_TYPES.keys()))
def test_entity_instantiates(name: str) -> None:
    e = _make(name)
    assert isinstance(e, d.BaseEntity)


def test_base_entity_id_is_uuid_string() -> None:
    doc = d.Document(source="s", format="md", content="x")
    # valid uuid4 string
    parsed = uuid.UUID(doc.id, version=4)
    assert str(parsed) == doc.id


def test_base_entity_timestamps_and_version() -> None:
    e = _make("Document")
    assert e.version == 1
    assert e.created_at
    assert e.updated_at
    # timestamps are ISO / sortable / tz-aware
    assert e.created_at.endswith(("+00:00", "Z")) or "T" in e.created_at


def test_base_entity_metadata_default() -> None:
    e = _make("Chunk")
    assert e.metadata == {}


def test_with_version_bumps_and_refreshes() -> None:
    e = _make("Document")
    old_created = e.created_at
    old_content = e.content
    bumped = e.with_version(5)
    assert bumped.version == 5
    assert bumped.version != e.version
    assert bumped.created_at == old_created  # created stays
    assert bumped.updated_at >= e.updated_at  # updated refreshed
    assert bumped.content == old_content  # payload preserved
    assert bumped.id == e.id


def test_chunk_dim() -> None:
    c_none = d.Chunk(document_id="d1", text="t", embedding=None)
    assert c_none.dim == 0
    c_vec = d.Chunk(document_id="d1", text="t", embedding=[0.1, 0.2, 0.3])
    assert c_vec.dim == 3


def test_event_source_default_and_override() -> None:
    assert d.Event(type="x").source == "kernel"
    assert d.Event(type="x", source="agent:planner").source == "agent:planner"


def test_tool_schema_alias() -> None:
    t = d.Tool(name="t", capability="hermes.search", schema={"type": "object"})
    assert t.input_schema == {"type": "object"}


def test_capability_fields() -> None:
    c = d.Capability(name="hermes.search", tools=["fs", "pdf"], schema={"k": "v"})
    assert c.name == "hermes.search"
    assert c.tools == ["fs", "pdf"]
    assert c.input_schema == {"k": "v"}  # alias schema -> input_schema
    assert c.workspace_id == "default"


def test_workspace_fields() -> None:
    w = d.Workspace(name="team", owner_id="u1", settings={"plan": "pro"})
    assert w.owner_id == "u1" and w.settings == {"plan": "pro"}
    assert w.workspace_id == "default"


def test_dataset_conversation_placeholders() -> None:
    ds = d.Dataset(name="raw", source="s3", format="csv")
    conv = d.Conversation(title="t", messages=[{"role": "user", "content": "hi"}])
    assert ds.format == "csv"
    assert conv.messages[0]["role"] == "user"
    assert ds.workspace_id == conv.workspace_id == "default"
