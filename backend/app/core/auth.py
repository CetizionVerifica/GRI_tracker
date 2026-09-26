"""Authentication primitives: password hashing, login tokens and the caller's identity.

Resolving a token to an identity, and an identity to organization access, lives in the tenancy
service because it needs tenancy data. Other modules get both from there.
"""

import hashlib
import secrets
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # Argon2id with the library's current recommended parameters


class OrgRole(StrEnum):
    """Roles a user can hold in an organization. Stored as data in role_assignment.role."""

    ORG_ADMIN = "org_admin"
    CONTRIBUTOR = "contributor"
    APPROVER = "approver"
    VIEWER = "viewer"
    AUDITOR = "auditor"


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: UUID
    is_platform_admin: bool


@dataclass(frozen=True, slots=True)
class OrgAccess:
    """The caller's access to one organization, resolved once per request."""

    organization_id: UUID
    user_id: UUID
    is_platform_admin: bool
    roles: frozenset[str]

    def has_any_role(self, *roles: str) -> bool:
        """Platform admins pass every role check."""
        return self.is_platform_admin or not self.roles.isdisjoint(roles)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def verify_password(password_hash: str | None, password: str) -> bool:
    """Check a password. Without a hash, still does the work, so timing reveals nothing."""
    try:
        return _hasher.verify(password_hash or _dummy_hash(), password) and bool(password_hash)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_session_token() -> tuple[str, bytes]:
    """A new login token and the digest to store. Only the digest is ever persisted."""
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
