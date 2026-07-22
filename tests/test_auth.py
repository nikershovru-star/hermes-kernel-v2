"""tests/test_auth.py — AuthRegistry register / authenticate / roles."""

import pytest

from kernel.auth import AuthRegistry, hash_password, verify_password
from kernel.domain import User


@pytest.fixture
def auth() -> AuthRegistry:
    return AuthRegistry()


def test_register_authenticate(auth) -> None:
    user = auth.register("alice", "s3cret", roles=["admin"])
    assert isinstance(user, User)
    assert user.username == "alice"
    assert user.hashed_password != "s3cret"  # never plaintext

    got = auth.authenticate("alice", "s3cret")
    assert got is not None
    assert got.id == user.id


def test_wrong_password(auth) -> None:
    auth.register("bob", "correct-horse")
    assert auth.authenticate("bob", "wrong") is None
    assert auth.authenticate("nonexistent", "whatever") is None


def test_roles(auth) -> None:
    user = auth.register("carol", "pw", roles=["editor", "reviewer"])
    assert user.roles == ["editor", "reviewer"]
    assert auth.has_role(user.id, "editor") is True
    assert auth.has_role(user.id, "admin") is False
    # default: no roles
    plain = auth.register("dave", "pw")
    assert plain.roles == []


def test_duplicate_username(auth) -> None:
    auth.register("eve", "pw")
    with pytest.raises(ValueError):
        auth.register("eve", "other")


def test_lookup_helpers(auth) -> None:
    u = auth.register("frank", "pw", roles=["x"])
    assert auth.get_user(u.id).username == "frank"
    assert auth.get_by_username("frank").id == u.id
    assert auth.get_user("nope") is None
    assert auth.get_by_username("ghost") is None
    assert len(auth.list()) == 1


def test_password_hashing_primitives() -> None:
    h = hash_password("mypassword")
    assert "$" in h
    assert verify_password("mypassword", h) is True
    assert verify_password("wrong", h) is False
    # malformed stored hash -> False, not crash
    assert verify_password("x", "not-a-valid-hash") is False
    # different salts -> different hashes for same password
    assert hash_password("same") != hash_password("same")
