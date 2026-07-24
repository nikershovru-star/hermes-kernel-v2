"""tests/test_marketplace_store.py — MarketplaceStore persistence (ADR-026)."""

from __future__ import annotations

from kernel.marketplace_domain import (
    CatalogEntry,
    NodeInfo,
    PluginPackage,
    PluginSource,
    PluginStatus,
)
from kernel.marketplace_store import MarketplaceStore


def _pkg(pid="pkg.x", name="x", caps=None, source=PluginSource.LOCAL, entry="plugins.x:X"):
    return PluginPackage(
        package_id=pid, name=name, version="1.0", source=source, entrypoint=entry,
        capabilities=caps or ["cap.x"], status=PluginStatus.INSTALLED,
    )


def test_put_get_delete_package_memory() -> None:
    s = MarketplaceStore()
    s.put_package(_pkg())
    assert s.get_package("pkg.x") is not None
    assert s.delete_package("pkg.x") is True
    assert s.get_package("pkg.x") is None


def test_list_packages_by_status_memory() -> None:
    s = MarketplaceStore()
    s.put_package(_pkg())
    s.put_package(PluginPackage(package_id="p2", name="y", version="1", source=PluginSource.MARKETPLACE, entrypoint="e", status=PluginStatus.AVAILABLE))
    installed = s.list_packages(status=PluginStatus.INSTALLED)
    assert len(installed) == 1
    assert len(s.list_packages()) == 2


def test_sqlite_package_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "mp.db")
    s = MarketplaceStore(db)
    s.put_package(_pkg())
    s2 = MarketplaceStore(db)
    loaded = s2.get_package("pkg.x")
    assert loaded is not None
    assert loaded.status == PluginStatus.INSTALLED


def test_sqlite_catalog_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "mp.db")
    s = MarketplaceStore(db)
    s.put_catalog_entry(CatalogEntry(entry_id="e1", package_id="pkg.x", source_url="http://c"))
    s2 = MarketplaceStore(db)
    assert len(s2.list_catalog()) == 1
    assert s2.list_catalog()[0].package_id == "pkg.x"


def test_sqlite_node_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "mp.db")
    s = MarketplaceStore(db)
    s.put_node(NodeInfo(node_id="n1", address="a1", capabilities=["c1"]))
    s2 = MarketplaceStore(db)
    n = s2.get_node("n1")
    assert n is not None
    assert n.capabilities == ["c1"]
    assert s2.delete_node("n1") is True
    assert s2.get_node("n1") is None


def test_list_catalog_packages_reconstructs(tmp_path) -> None:
    db = str(tmp_path / "mp.db")
    s = MarketplaceStore(db)
    s.put_catalog_entry(CatalogEntry(entry_id="e1", package_id="pkg.vision", source_url="http://c", rating=4.5))
    s.put_package(_pkg("pkg.vision", "vision"))
    s2 = MarketplaceStore(db)
    pkgs = s2.list_catalog_packages()
    assert any(p.package_id == "pkg.vision" for p in pkgs)


def test_get_topology_from_store(tmp_path) -> None:
    db = str(tmp_path / "mp.db")
    s = MarketplaceStore(db)
    s.put_node(NodeInfo(node_id="n1", address="a1", capabilities=["c1"]))
    s.put_node(NodeInfo(node_id="n2", address="a2", capabilities=["c2"]))
    s2 = MarketplaceStore(db)
    topo = s2.get_topology("cluster-a")
    assert set(topo.nodes.keys()) == {"n1", "n2"}


def test_list_nodes(tmp_path) -> None:
    s = MarketplaceStore()
    s.put_node(NodeInfo(node_id="n1", address="a1"))
    s.put_node(NodeInfo(node_id="n2", address="a2"))
    assert len(s.list_nodes()) == 2
