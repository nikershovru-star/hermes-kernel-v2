"""kernel/marketplace.py — PluginMarketplace (ADR-026).

Distributed plugin discovery + install with deterministic, injectable I/O.

AXIS CONTRACT: imports only ``kernel.marketplace_domain`` + ``kernel.events``
(and ``kernel.domain`` for shared types where needed). It never imports
``plugins/`` directly — remote packages are fetched via an injected
``http_client`` and validated locally, so no real network dependency is
required.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kernel.config_domain import ConfigScope
from kernel.events import EventBus, EventStore, PluginDiscovered, PluginInstallFailed, PluginInstalled
from kernel.marketplace_domain import (
    CatalogEntry,
    ClusterTopology,
    NodeInfo,
    PluginPackage,
    PluginSource,
    PluginStatus,
)
from kernel.security_domain import Permission  # ADR-028: validate policy actions

logger = logging.getLogger("hermes.kernel.marketplace")


class PluginMarketplace:
    """Discover, validate, install and uninstall plugin packages.

    All external I/O is injected: ``http_client.get(url) -> str`` (JSON catalog),
    ``clock() -> float/datetime``, ``rng`` for tie-breaking, ``sleep`` for
    deterministic async delays. Falls back to in-memory when no ``store``.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        registry: Any | None = None,
        store: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rng: random.Random | None = None,
        http_client: Any | None = None,
        sleep: Callable[..., Awaitable[None]] = asyncio.sleep,
        guard: Any | None = None,
        mcp: Any | None = None,
        vault: Any | None = None,
    ) -> None:
        self._bus = event_bus
        self._event_store = event_store
        self._registry = registry
        self._store = store
        self._clock = clock
        self._rng = rng or random.Random()
        self._http = http_client
        self._sleep = sleep
        self._guard = guard  # ADR-028: optional CapabilityGuard
        self._mcp = mcp  # ADR-029: optional McpGateway
        self._vault = vault  # ADR-030: optional ConfigVault
        # in-memory caches (mirror store when no persistence)
        self._installed: dict[str, PluginPackage] = {}
        self._catalog: dict[str, CatalogEntry] = {}
        self._available: dict[str, PluginPackage] = {}
        self._mcp_packages: dict[str, PluginPackage] = {}  # ADR-029: virtual MCP-tool packages

    # -- discovery ------------------------------------------------------- #
    async def discover(self, source_url: str) -> list[CatalogEntry]:
        """Fetch a remote catalog (JSON) and record entries.

        The catalog JSON is a list of package dicts. Each becomes a
        ``CatalogEntry`` + ``PluginPackage`` (status AVAILABLE) and emits
        ``PluginDiscovered``. Requires an injected ``http_client``.
        """
        if self._http is None:
            raise RuntimeError("discover requires an injected http_client")
        raw = await self._http.get(source_url)
        data = raw if isinstance(raw, list) else _loads(raw)
        entries: list[CatalogEntry] = []
        for i, item in enumerate(data):
            pkg = PluginPackage(
                package_id=item.get("package_id", item.get("name", f"pkg-{i}")),
                name=item["name"],
                version=item.get("version", "0.0.0"),
                source=PluginSource(item.get("source", "marketplace")),
                entrypoint=item.get("entrypoint", ""),
                capabilities=list(item.get("capabilities", [])),
                dependencies=list(item.get("dependencies", [])),
                checksum=item.get("checksum"),
                signature=item.get("signature"),
                metadata=dict(item.get("metadata", {})),
                status=PluginStatus.AVAILABLE,
                created_at=self._clock(),
            )
            entry = CatalogEntry(
                entry_id=uuid.uuid4().hex,
                package_id=pkg.package_id,
                source_url=source_url,
                rating=float(item.get("rating", 0.0)),
                download_count=int(item.get("download_count", 0)),
                last_synced=self._clock(),
            )
            self._catalog[entry.entry_id] = entry
            self._available[pkg.package_id] = pkg
            if self._store is not None:
                self._store.put_catalog_entry(entry)
                self._store.put_package(pkg)
            await self._emit(
                PluginDiscovered(pkg.package_id, pkg.name, pkg.source.value, pkg.version, source_url)
            )
            entries.append(entry)
        return entries

    # -- validation ------------------------------------------------------ #
    # Allow-list of recognised permission actions. A package policy must only
    # declare actions from this set (basic validation, not full PKI — see ADR-028).
    _ALLOWED_ACTIONS = frozenset({
        "execute", "discover", "network", "file.read", "file.write",
        "subprocess", "memory", "cpu",
    })

    def validate_package(self, package: PluginPackage) -> tuple[bool, str]:
        """Validate checksum (if present), declared dependencies, and policy actions.

        Returns ``(ok, reason)``. A present checksum must match the SHA-256 of
        ``entrypoint``. Dependencies must be installed or available. If a
        ``policy`` is declared, every permission ``action`` must be in the
        allow-list (a guard against arbitrary-code pseudo-permissions).
        """
        if package.policy is not None:
            bad = [p.action for p in package.policy.permissions
                   if p.action not in self._ALLOWED_ACTIONS]
            if bad:
                return False, f"disallowed policy actions: {sorted(set(bad))}"
        if package.checksum:
            digest = hashlib.sha256(package.entrypoint.encode("utf-8")).hexdigest()
            if digest != package.checksum:
                return False, "checksum mismatch"
        if self._store is not None:
            installed = {p.package_id for p in self._store.list_packages()}
            available = {e.package_id for e in self._store.list_catalog()}
        else:
            installed = set(self._installed.keys())
            available = set(p.package_id for p in self._catalog.values())
        missing = [d for d in package.dependencies if d not in installed and d not in available]
        if missing:
            return False, f"missing dependencies: {missing}"
        return True, ""

    # -- install / uninstall -------------------------------------------- #
    async def install(self, package: PluginPackage) -> PluginPackage:
        ok, reason = self.validate_package(package)
        if not ok:
            package.status = PluginStatus.FAILED
            await self._emit(PluginInstallFailed(package.package_id, package.name, reason))
            if self._store is not None:
                self._store.put_package(package)
            return package
        # ADR-030: if the package declares required_secrets and a vault is wired,
        # verify every secret exists (scope=PLUGIN, scope_id=package_id) before
        # installing. Missing any -> PluginInstallFailed(missing_required_secrets).
        # No vault wired => precondition skipped (zero regression).
        secrets_resolved = False
        if package.required_secrets and self._vault is not None:
            missing: list[str] = []
            for skey in package.required_secrets:
                try:
                    await self._vault.resolve_secret(
                        skey,
                        scope=ConfigScope.PLUGIN,
                        scope_id=package.package_id,
                        accessor=f"marketplace:{package.package_id}",
                    )
                except (KeyError, RuntimeError):
                    missing.append(skey)
            if missing:
                package.status = PluginStatus.FAILED
                await self._emit(
                    PluginInstallFailed(
                        package.package_id,
                        package.name,
                        f"missing_required_secrets: {missing}",
                    )
                )
                if self._store is not None:
                    self._store.put_package(package)
                return package
            secrets_resolved = True
        # ADR-028: register the package's sandbox policy with the guard (optional).
        if self._guard is not None and package.policy is not None:
            await self._guard.register_policy(package.package_id, package.policy)
        package.status = PluginStatus.INSTALLING
        await self._sleep(0)
        package.status = PluginStatus.INSTALLED
        package.installed_at = self._clock()
        self._installed[package.package_id] = package
        if self._store is not None:
            self._store.put_package(package)
        await self._emit(
            PluginInstalled(
                package.package_id,
                package.name,
                package.version,
                package.source.value,
                secrets_resolved=secrets_resolved,
            )
        )
        return package

    async def uninstall(self, package_id: str) -> bool:
        removed = self._installed.pop(package_id, None)
        if removed is None and self._store is not None:
            removed = self._store.get_package(package_id)
        if removed is None:
            return False
        if self._store is not None:
            self._store.delete_package(package_id)
        return True

    # -- queries --------------------------------------------------------- #
    def list_installed(self) -> list[PluginPackage]:
        if self._store is not None:
            return self._store.list_packages(status=PluginStatus.INSTALLED)
        return [p for p in self._installed.values() if p.status == PluginStatus.INSTALLED]

    def get_package(self, package_id: str) -> PluginPackage | None:
        if self._store is not None:
            return self._store.get_package(package_id)
        return self._installed.get(package_id) or self._available.get(package_id)

    def list_available(self) -> list[PluginPackage]:
        if self._store is not None:
            packages = self._store.list_catalog_packages()
        else:
            packages = list(self._available.values())
        # ADR-029: augment with virtual packages for discovered MCP tools.
        if self._mcp is not None and self._mcp_packages:
            packages = packages + list(self._mcp_packages.values())
        return packages

    # -- MCP discovery (ADR-029) ------------------------------------------ #
    async def discover_mcp_tools(self, source_url: str) -> list[Any]:
        """Connect to an MCP server + list its tools as catalog entries.

        Requires a wired ``McpGateway``. Each discovered tool becomes a
        ``CatalogEntry`` (source=MCP_SERVER) + a virtual ``PluginPackage``
        exposing the ``mcp:<server_url>::<tool>`` capability.
        """
        if self._mcp is None:
            raise RuntimeError("MCP gateway not wired")
        await self._mcp.connect(source_url)
        tools = await self._mcp.list_tools(source_url)
        for tool in tools:
            package_id = f"mcp:{source_url}::{tool.name}"
            pkg = PluginPackage(
                package_id=package_id,
                name=tool.name,
                version="0.0.0",
                source=PluginSource.MCP_SERVER,
                entrypoint=package_id,
                capabilities=[package_id],
                metadata={"description": tool.description, "input_schema": tool.input_schema},
                status=PluginStatus.AVAILABLE,
                created_at=self._clock(),
            )
            self._mcp_packages[package_id] = pkg
            entry = CatalogEntry(
                entry_id=uuid.uuid4().hex,
                package_id=package_id,
                source_url=source_url,
                last_synced=self._clock(),
            )
            self._catalog[entry.entry_id] = entry
            if self._store is not None:
                try:
                    self._store.put_catalog_entry(entry)
                except Exception:  # noqa: BLE001 - store schema may not persist MCP entries
                    pass
            await self._emit(
                PluginDiscovered(package_id, tool.name, PluginSource.MCP_SERVER.value, "0.0.0", source_url)
            )
        return tools


    # -- local registration --------------------------------------------- #
    async def register_local(self, entrypoint: str, capabilities: list[str], name: str | None = None) -> PluginPackage:
        pkg = PluginPackage(
            package_id=entrypoint,
            name=name or entrypoint.split(".")[-1],
            version="0.0.0",
            source=PluginSource.LOCAL,
            entrypoint=entrypoint,
            capabilities=list(capabilities),
            status=PluginStatus.INSTALLED,
            installed_at=self._clock(),
            created_at=self._clock(),
        )
        self._installed[pkg.package_id] = pkg
        if self._store is not None:
            self._store.put_package(pkg)
        await self._emit(PluginInstalled(pkg.package_id, pkg.name, pkg.version, pkg.source.value))
        return pkg

    # -- helpers --------------------------------------------------------- #
    async def _emit(self, event: Any) -> None:
        if self._bus is not None:
            self._bus.publish(event)
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001
                pass


def _loads(raw: Any) -> list[dict]:
    import json

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)
