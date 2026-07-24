"""kernel/marketplace_domain.py — Plugin Marketplace / Multi-node domain (ADR-026).

Isolated from ``kernel.domain`` on purpose: ADR-023 already defines
``NodeInfo`` (fields ``load_score`` / ``last_seen``) in ``kernel.domain`` and
``PluginRegistry`` in ``kernel.registry``. The ADR-026 ``NodeInfo`` /
``ClusterTopology`` have a different shape, so redefining them in ``domain``
would clobber ADR-023 and regress the existing 551-test baseline. All ADR-026
marketplace/cluster models live here, axis-clean (stdlib ``datetime`` +
pydantic only).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ADR-028: optional sandbox policy model (leaf module — no dependency cycle).
from kernel.security_domain import SandboxPolicy


class PluginSource(str, Enum):
    """Where a plugin package originates."""

    LOCAL = "local"
    REMOTE = "remote"
    MARKETPLACE = "marketplace"
    BUILTIN = "builtin"


class PluginStatus(str, Enum):
    """Lifecycle state of a plugin package."""

    AVAILABLE = "available"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"
    DISABLED = "disabled"


class PluginPackage(BaseModel):
    """A distributable plugin package declaration."""

    package_id: str
    name: str
    version: str
    source: PluginSource
    entrypoint: str
    capabilities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    checksum: str | None = None
    signature: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: PluginStatus = PluginStatus.AVAILABLE
    installed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # ADR-028: optional permission-based sandbox policy carried by the package.
    # Absence => no guard registered (zero regression; guard treats unknown
    # package as allowed when unwired).
    policy: "SandboxPolicy | None" = None


class CatalogEntry(BaseModel):
    """A listing of a package in a remote/marketplace catalog."""

    entry_id: str
    package_id: str
    source_url: str
    last_synced: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    rating: float = 0.0
    download_count: int = 0


class NodeInfo(BaseModel):
    """A node participating in the (logical) multi-node cluster (ADR-026).

    Note: distinct from ``kernel.domain.NodeInfo`` (ADR-023) which uses
    ``load_score`` / ``last_seen``. This ADR-026 variant uses ``load`` /
    ``last_heartbeat``.
    """

    node_id: str
    address: str = "inproc"
    capabilities: list[str] = Field(default_factory=list)
    last_heartbeat: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    load: float = 0.0


class ClusterTopology(BaseModel):
    """Snapshot of the cluster membership."""

    cluster_id: str
    nodes: dict[str, NodeInfo] = Field(default_factory=dict)
    leader_id: str | None = None
