"""Typed errors with stable, distinguishable reason codes."""
from __future__ import annotations

from enum import Enum


class ReasonCode(str, Enum):
    # validation
    INVALID_INPUT = "INVALID_INPUT"
    # compatibility
    FIELD_REMOVED = "FIELD_REMOVED"
    FIELD_TYPE_CHANGED = "FIELD_TYPE_CHANGED"
    REQUIRED_FIELD_ADDED = "REQUIRED_FIELD_ADDED"
    FIELD_BECAME_REQUIRED = "FIELD_BECAME_REQUIRED"
    # concurrency
    REVISION_CONFLICT = "REVISION_CONFLICT"
    # lineage
    CYCLE_DETECTED = "CYCLE_DETECTED"
    # lookup / auth
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    UNAUTHENTICATED = "UNAUTHENTICATED"


class CatalogError(Exception):
    """Base error carrying a machine-readable reason code."""

    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ValidationError(CatalogError):
    def __init__(self, message: str) -> None:
        super().__init__(ReasonCode.INVALID_INPUT, message)


class IncompatibleSchemaError(CatalogError):
    """Raised when a schema evolution violates compatibility rules."""


class RevisionConflictError(CatalogError):
    def __init__(self, entity_id: str) -> None:
        super().__init__(
            ReasonCode.REVISION_CONFLICT,
            f"entity {entity_id!r} was modified concurrently; retry with fresh revision",
        )


class CycleDetectedError(CatalogError):
    def __init__(self, path: list[str]) -> None:
        super().__init__(
            ReasonCode.CYCLE_DETECTED,
            "lineage cycle detected: " + " -> ".join(path),
        )
        self.path = path


class NotFoundError(CatalogError):
    def __init__(self, what: str) -> None:
        super().__init__(ReasonCode.NOT_FOUND, f"{what} not found")


class ForbiddenError(CatalogError):
    def __init__(self, message: str = "forbidden") -> None:
        super().__init__(ReasonCode.FORBIDDEN, message)


class UnauthenticatedError(CatalogError):
    def __init__(self) -> None:
        super().__init__(ReasonCode.UNAUTHENTICATED, "missing or invalid credentials")
