"""元数据模型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class FieldType(str, Enum):
    STRING = "STRING"
    INT = "INT"
    BIGINT = "BIGINT"
    DOUBLE = "DOUBLE"
    DECIMAL = "DECIMAL"
    BOOL = "BOOL"
    TIMESTAMP = "TIMESTAMP"
    DATE = "DATE"


@dataclass(frozen=True)
class Field:
    """schema 中的一个列/字段。"""

    name: str
    type: FieldType
    nullable: bool = True
    comment: str = ""


@dataclass(frozen=True)
class Schema:
    """一个实体某版本的 schema。"""

    fields: tuple[Field, ...]

    def field_map(self) -> dict[str, Field]:
        return {f.name: f for f in self.fields}


@dataclass(frozen=True)
class SchemaVersion:
    """实体的一个 schema 版本。"""

    version: int
    schema: Schema
    created_by: str
    created_at: datetime


@dataclass
class Entity:
    """数据实体（表/视图/文件等），带乐观锁版本号。"""

    entity_id: str
    namespace: str
    name: str
    owner: str
    lock_version: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_versions: list[SchemaVersion] = field(default_factory=list)

    @property
    def current_schema(self) -> Schema | None:
        return self.schema_versions[-1].schema if self.schema_versions else None


@dataclass(frozen=True)
class ColumnLineage:
    """列级血缘：上游实体.列 -> 下游实体.列。"""

    upstream_entity: str
    upstream_column: str
    downstream_column: str


@dataclass(frozen=True)
class LineageEdge:
    """表级血缘边：upstream -> downstream，附带列级映射。"""

    upstream: str
    downstream: str
    column_lineage: tuple[ColumnLineage, ...] = ()
    created_by: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class Caller:
    """调用方身份与可见范围（命名空间集合）。"""

    caller_id: str
    namespaces: frozenset[str]
    is_admin: bool = False

    def can_see(self, namespace: str) -> bool:
        return self.is_admin or namespace in self.namespaces


@dataclass(frozen=True)
class ImpactItem:
    """影响分析中的一个受影响节点。"""

    entity_id: str
    level: int
    columns: tuple[str, ...] = ()  # 受影响的列；空表示整表受影响
