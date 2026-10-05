"""API-key based authentication and namespace-scoped authorization."""
from __future__ import annotations

from dataclasses import dataclass

from app.errors import UnauthenticatedError


@dataclass(frozen=True)
class Principal:
    name: str
    is_admin: bool
    namespaces: frozenset[str]  # namespaces the principal may access


# Static API-key registry for local/dev use.
_KEYS: dict[str, Principal] = {
    "admin-key": Principal("admin", True, frozenset()),
    "alice-key": Principal("alice", False, frozenset({"analytics"})),
    "bob-key": Principal("bob", False, frozenset({"billing"})),
}


def authenticate(api_key: str | None) -> Principal:
    """Resolve an API key to a Principal; raise UnauthenticatedError otherwise."""
    if api_key is None:
        raise UnauthenticatedError()
    principal = _KEYS.get(api_key)
    if principal is None:
        raise UnauthenticatedError()
    return principal


def can_access(principal: Principal, namespace: str) -> bool:
    return principal.is_admin or namespace in principal.namespaces
