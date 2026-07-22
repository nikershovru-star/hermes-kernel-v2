"""tests/test_rbac.py — RBACRegistry roles, permission checks, integration."""

import pytest

from kernel.auth import AuthRegistry
from kernel.domain import Workspace
from kernel.rbac import Permission, RBACRegistry, Role
from kernel.workspace import WorkspaceRegistry


@pytest.fixture
def rbac() -> RBACRegistry:
    auth = AuthRegistry()
    auth.register("alice", "pw", roles=[])
    auth.register("bob", "pw", roles=[])
    return RBACRegistry(auth)


def test_create_role_assign(rbac: RBACRegistry) -> None:
    perm = Permission("workspace", "create")
    role = rbac.create_role("admin", [perm])
    assert isinstance(role, Role)
    assert role.name == "admin"
    assert perm in role.permissions

    alice = rbac._auth.get_by_username("alice")
    assert alice is not None
    rbac.assign_role(alice.id, "admin")
    assert "admin" in alice.roles
    # list_roles reflects the created role
    assert any(r.name == "admin" for r in rbac.list_roles())
    # duplicate create_role name is idempotent (overwrites, no error)
    rbac.create_role("admin", [Permission("workspace", "delete")])
    assert "workspace:delete" in {str(p) for p in rbac.get_role("admin").permissions}


def test_check_permission_granted(rbac: RBACRegistry) -> None:
    rbac.create_role("editor", [Permission("workspace", "create")])
    alice = rbac._auth.get_by_username("alice")
    bob = rbac._auth.get_by_username("bob")
    assert alice and bob
    rbac.assign_role(alice.id, "editor")

    assert rbac.check_permission(alice.id, Permission("workspace", "create")) is True
    # bob has no roles -> denied
    assert rbac.check_permission(bob.id, Permission("workspace", "create")) is False
    # unknown user -> denied (no crash)
    assert rbac.check_permission("ghost", Permission("workspace", "create")) is False


def test_permission_denied(rbac: RBACRegistry) -> None:
    rbac.create_role("viewer", [Permission("workspace", "read")])
    alice = rbac._auth.get_by_username("alice")
    assert alice
    rbac.assign_role(alice.id, "viewer")

    # viewer may read but not create
    assert rbac.check_permission(alice.id, Permission("workspace", "read")) is True
    assert rbac.check_permission(alice.id, Permission("workspace", "create")) is False

    # require_permission raises PermissionError on denial
    with pytest.raises(PermissionError):
        rbac.require_permission(alice.id, Permission("workspace", "create"))
    # and passes when granted
    rbac.require_permission(alice.id, Permission("workspace", "read"))


async def test_workspace_rbac_integration(rbac: RBACRegistry) -> None:
    """RBAC gates WorkspaceRegistry.create without modifying the registry."""
    ws_reg = WorkspaceRegistry()
    rbac.create_role(
        "workspace-admin",
        [Permission("workspace", "create"), Permission("workspace", "read")],
    )
    alice = rbac._auth.get_by_username("alice")
    bob = rbac._auth.get_by_username("bob")
    assert alice and bob
    rbac.assign_role(alice.id, "workspace-admin")

    # alice is allowed -> operation proceeds
    rbac.require_permission(alice.id, Permission("workspace", "create"))
    ws_alice = await ws_reg.create("alice-proj", alice.id)
    assert isinstance(ws_alice, Workspace)
    assert ws_alice.name == "alice-proj"

    # bob is denied -> guard raises before any registry call
    with pytest.raises(PermissionError):
        rbac.require_permission(bob.id, Permission("workspace", "create"))
        await ws_reg.create("bob-proj", bob.id)  # never reached

    # registry state is unaffected by the denied attempt (only alice's ws exists;
    # default is auto-seeded only when the registry is empty, so it is absent now)
    names = {w.name for w in await ws_reg.list()}
    assert names == {"alice-proj"}
