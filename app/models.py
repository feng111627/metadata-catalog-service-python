"""Core domain models for the metadata catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EntityKind(str, Enum):
    TABLE = "table"
    VIEW = "view"
    STREAM = "stream"


@dataclass(frozen=True)
class Field:
    """A single column/field of a schema."""

    name: str
    type: str
    required: bool = False


@dataclass(frozen=True)
class SchemaVersion:
    """An immutable schema version of an entity."""

    version: int
    fields: tuple[Field, ...]


@dataclass
class DataEntity:
    """A registered data entity with its schema history."""

    entity_id: str
    name: str
    namespace: str
    kind: EntityKind
    owner: str
    schemas: list[SchemaVersion] = field(default_factory=list)
    # optimistic-concurrency token, bumped on every mutation
    revision: int = 0

    @property
    def current_schema(self) -> SchemaVersion | None:
        return self.schemas[-1] if self.schemas else None


@dataclass(frozen=True)
class ColumnMapping:
    """Column-level lineage: upstream column -> downstream column."""

    upstream: str
    downstream: str


@dataclass(frozen=True)
class LineageEdge:
    """Table-level lineage edge with optional column mappings."""

    upstream_id: str
    downstream_id: str
    columns: tuple[ColumnMapping, ...] = ()


@dataclass(frozen=True)
class AuditRecord:
    """One audit log entry."""

    seq: int
    timestamp: str
    actor: str
    action: str
    target: str
    outcome: str
    detail: str
