"""kernel/rbac.py — role-based access control (P5.2).

A lightweight, in-memory RBAC engine that gates kernel operations. Roles bundle
permissions; users (from ``AuthRegistry``) hold role names. ``RBACRegistry`` is a
**guard**, not a wrapper: it does not modify the existing registries — callers
check ``check_permission`` / ``require_permission`` *before* invoking an
operation on ``WorkspaceRegistry`` / ``AgentRegistry`` / ``KnowledgeGraph``.

AXIS CONTRACT: depends on kernel.domain (User) + kernel.auth (AuthRegistry)
only. No I/O.

Design notes
------------
- ``Permission`` / ``Role`` are value objects (frozen, hashable).
- ``RBACRegistry`` resolves a user's roles via the injected ``AuthRegistry`` and
  fast-forwards through each role's permission set.
- ``require_permission`` raises ``PermissionError`` on denial — the call sites
  use it as a guard clause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from kernel.auth import AuthRegistry


@dataclass(frozen=True)
class Permission:
    """A (resource, action) capability, e.g. ('workspace', 'create')."""

    resource: str
    action: str

    def __str__(self) -> str:  # human-readable, also used in error messages
        return f"{self.resource}:{self.action}"


@dataclass(frozen=True)
class Role:
    """A named bundle of permissions."""

    name: str
    permissions: frozenset[Permission] = field(default_factory=frozenset)

    @classmethod
    def from_lists(
        cls, name: str, permissions: Iterable[Permission]
    ) -> "Role":
        return cls(name=name, permissions=frozenset(permissions))


class RBACRegistry:
    """In-memory role store + permission checker bound to an AuthRegistry."""

    def __init__(self, auth: AuthRegistry) -> None:
        self._auth = auth
        self._roles: dict[str, Role] = {}

    # -- role management -------------------------------------------------- #
    def create_role(
        self, name: str, permissions: Iterable[Permission] | None = None
    ) -> Role:
        """Define a role. Duplicate name overwrites (idempotent re-config)."""
        role = Role.from_lists(name, permissions or [])
        self._roles[name] = role
        return role

    def assign_role(self, user_id: str, role_name: str) -> None:
        """Grant a role to a user (mutates the shared User.roles in memory)."""
        if role_name not in self._roles:
            raise ValueError(f"role {role_name!r} does not exist")
        user = self._auth.get_user(user_id)
        if user is None:
            raise ValueError(f"user {user_id!r} not found")
        if role_name not in user.roles:
            user.roles.append(role_name)

    def list_roles(self) -> list[Role]:
        return list(self._roles.values())

    def get_role(self, name: str) -> Role | None:
        return self._roles.get(name)

    # -- permission checking --------------------------------------------- #
    def check_permission(self, user_id: str, permission: Permission) -> bool:
        """Return True if the user holds any role granting ``permission``."""
        user = self._auth.get_user(user_id)
        if user is None:
            return False
        for role_name in user.roles:
            role = self._roles.get(role_name)
            if role is not None and permission in role.permissions:
                return True
        return False

    def require_permission(self, user_id: str, permission: Permission) -> None:
        """Raise ``PermissionError`` unless the user has ``permission``."""
        if not self.check_permission(user_id, permission):
            raise PermissionError(
                f"user {user_id!r} lacks permission {permission}"
            )

    def permissions_of(self, user_id: str) -> set[Permission]:
        """All permissions a user effectively holds (across all roles)."""
        out: set[Permission] = set()
        user = self._auth.get_user(user_id)
        if user is None:
            return out
        for role_name in user.roles:
            role = self._roles.get(role_name)
            if role is not None:
                out |= set(role.permissions)
        return out
