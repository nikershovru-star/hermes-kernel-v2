"""kernel/auth.py — basic in-memory authentication (P5.1).

A minimal, dependency-free auth layer: users with salted-hash passwords and
roles, registered and authenticated in memory. Deliberately simple — no JWT, no
sessions, no external crypto lib. Password hashing uses stdlib
``hashlib.pbkdf2_hmac`` (SHA-256, salted, 100k iterations) so plaintext is never
stored. JWT / RBAC enforcement / persistence are later P5 stages.

AXIS CONTRACT: depends on kernel.domain (User) only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from kernel.domain import User

logger = logging.getLogger("hermes.auth")

_ITERATIONS = 100_000
_ALGO = "sha256"


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a ``salt_hex$hash_hex`` string (pbkdf2-hmac-sha256, salted)."""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify a password against a ``salt$hash`` string."""
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)


class AuthRegistry:
    """In-memory user registry: register, authenticate, lookup."""

    def __init__(self) -> None:
        self._by_id: dict[str, User] = {}
        self._by_username: dict[str, str] = {}  # username -> user_id

    def register(
        self, username: str, password: str, roles: list[str] | None = None
    ) -> User:
        """Create a user with a salted-hash password. Duplicate -> ValueError."""
        if username in self._by_username:
            raise ValueError(f"username {username!r} already registered")
        user = User(
            username=username,
            hashed_password=hash_password(password),
            roles=list(roles or []),
        )
        self._by_id[user.id] = user
        self._by_username[username] = user.id
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        """Return the User on correct credentials, else None."""
        user_id = self._by_username.get(username)
        if user_id is None:
            return None
        user = self._by_id[user_id]
        if verify_password(password, user.hashed_password):
            return user
        return None

    def get_user(self, user_id: str) -> User | None:
        return self._by_id.get(user_id)

    def get_by_username(self, username: str) -> User | None:
        user_id = self._by_username.get(username)
        return self._by_id.get(user_id) if user_id else None

    def has_role(self, user_id: str, role: str) -> bool:
        user = self._by_id.get(user_id)
        return bool(user and role in user.roles)

    def list(self) -> list[User]:
        return list(self._by_id.values())
