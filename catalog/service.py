"""门面服务：编排实体登记、schema 演进、血缘、影响分析、审计与权限。"""
from __future__ import annotations

from datetime import datetime, timezone

from .audit import AuditLog, AuditRecord
from .compatibility import check_compatibility
from .errors import (
    CatalogError,
    CompatibilityError,
    PermissionDeniedError,
    RejectReason,
    ValidationError,
)
from .impact import ImpactAnalyzer
from .lineage import LineageGraph
from .models import Caller, Entity, Field, FieldType, ImpactItem, LineageEdge, Schema, SchemaVersion
from .store import EntityStore

_ACCESS_DENIED = "access denied"  # 统一文案：不暴露实体是否存在


class MetadataCatalogService:
    """元数据目录服务门面。

    - 所有操作以 Caller 身份执行，按命名空间控制可见范围；
    - 对无权访问的实体，"不存在"与"无权限"返回完全相同的错误，
      不通过错误信息暴露实体是否存在；
    - 每个操作（无论成败）都会写入审计记录。
    """

    def __init__(self) -> None:
        self._store = EntityStore()
        self._graph = LineageGraph()
        self._analyzer = ImpactAnalyzer(self._graph)
        self._audit = AuditLog()

    # ---------- 输入校验 ----------

    @staticmethod
    def _validate_schema(schema: Schema) -> None:
        if not isinstance(schema, Schema) or not isinstance(schema.fields, tuple):
            raise ValidationError(RejectReason.INVALID_STRUCTURE, "schema must be a Schema with tuple fields")
        if not schema.fields:
            raise ValidationError(RejectReason.MISSING_FIELD, "schema must contain at least one field")
        seen: set[str] = set()
        for f in schema.fields:
            if not isinstance(f, Field):
                raise ValidationError(RejectReason.INVALID_STRUCTURE, "field must be a Field instance")
            if not f.name or not isinstance(f.name, str):
                raise ValidationError(RejectReason.MISSING_FIELD, "field name is required")
            if not isinstance(f.type, FieldType):
                raise ValidationError(RejectReason.INVALID_STRUCTURE, f"field '{f.name}' has invalid type")
            if f.name in seen:
                raise ValidationError(RejectReason.INVALID_STRUCTURE, f"duplicate field '{f.name}'")
            seen.add(f.name)

    @staticmethod
    def _require_text(value: str, what: str) -> None:
        if not value or not isinstance(value, str):
            raise ValidationError(RejectReason.MISSING_FIELD, f"{what} is required")

    # ---------- 权限 ----------

    def _visible_entity(self, caller: Caller, entity_id: str) -> Entity:
        """取实体并校验可见性；不存在与无权限返回相同错误。"""
        entity = self._store.get(entity_id)
        if entity is None or not caller.can_see(entity.namespace):
            raise PermissionDeniedError(_ACCESS_DENIED)
        return entity

    # ---------- 实体与 schema ----------

    def register_entity(self, caller: Caller, entity_id: str, namespace: str,
                        name: str, owner: str, schema: Schema) -> Entity:
        action, target = "register_entity", entity_id
        try:
            self._require_text(entity_id, "entity_id")
            self._require_text(namespace, "namespace")
            self._require_text(name, "name")
            self._require_text(owner, "owner")
            self._validate_schema(schema)
            if not caller.can_see(namespace):
                raise PermissionDeniedError(_ACCESS_DENIED)
            entity = Entity(entity_id=entity_id, namespace=namespace, name=name, owner=owner)
            entity.schema_versions.append(SchemaVersion(
                version=1, schema=schema, created_by=caller.caller_id,
                created_at=datetime.now(timezone.utc),
            ))
            created = self._store.create(entity)
            self._audit.record(caller.caller_id, action, target, "SUCCESS",
                               f"namespace={namespace} schema_v=1")
            return created
        except CatalogError as exc:
            self._audit.record(caller.caller_id, action, target, f"REJECTED:{exc.reason.value}", str(exc))
            raise

    def get_entity(self, caller: Caller, entity_id: str) -> Entity:
        return self._visible_entity(caller, entity_id)

    def update_schema(self, caller: Caller, entity_id: str, new_schema: Schema,
                      expected_lock_version: int) -> Entity:
        action, target = "update_schema", entity_id
        try:
            self._validate_schema(new_schema)
            current = self._visible_entity(caller, entity_id)
            result = check_compatibility(current.current_schema, new_schema)
            if not result.compatible:
                raise CompatibilityError(result.reason, result.detail)

            def mutate(e: Entity) -> None:
                e.schema_versions.append(SchemaVersion(
                    version=len(e.schema_versions) + 1, schema=new_schema,
                    created_by=caller.caller_id,
                    created_at=datetime.now(timezone.utc),
                ))

            updated = self._store.update(entity_id, expected_lock_version, mutate)
            self._audit.record(caller.caller_id, action, target, "SUCCESS",
                               f"schema_v={len(updated.schema_versions)}")
            return updated
        except CatalogError as exc:
            self._audit.record(caller.caller_id, action, target, f"REJECTED:{exc.reason.value}", str(exc))
            raise

    # ---------- 血缘 ----------

    def register_lineage(self, caller: Caller, edge: LineageEdge) -> LineageEdge:
        action = "register_lineage"
        target = f"{edge.upstream}->{edge.downstream}"
        try:
            up = self._visible_entity(caller, edge.upstream)
            down = self._visible_entity(caller, edge.downstream)
            for cl in edge.column_lineage:
                if cl.upstream_entity != edge.upstream:
                    raise ValidationError(RejectReason.INVALID_STRUCTURE,
                                          "column lineage upstream_entity must match edge upstream")
                up_schema = up.current_schema.field_map()
                down_schema = down.current_schema.field_map()
                if cl.upstream_column not in up_schema:
                    raise ValidationError(RejectReason.COLUMN_NOT_FOUND,
                                          f"column '{cl.upstream_column}' not in '{edge.upstream}'")
                if cl.downstream_column not in down_schema:
                    raise ValidationError(RejectReason.COLUMN_NOT_FOUND,
                                          f"column '{cl.downstream_column}' not in '{edge.downstream}'")
            stamped = LineageEdge(
                upstream=edge.upstream, downstream=edge.downstream,
                column_lineage=edge.column_lineage,
                created_by=caller.caller_id,
                created_at=datetime.now(timezone.utc),
            )
            registered = self._graph.register(stamped)
            self._audit.record(caller.caller_id, action, target, "SUCCESS")
            return registered
        except CatalogError as exc:
            self._audit.record(caller.caller_id, action, target, f"REJECTED:{exc.reason.value}", str(exc))
            raise

    def get_lineage(self, caller: Caller, entity_id: str) -> dict[str, list[LineageEdge]]:
        self._visible_entity(caller, entity_id)

        def visible(edges: list[LineageEdge]) -> list[LineageEdge]:
            result = []
            for e in edges:
                other_up = self._store.get(e.upstream)
                other_down = self._store.get(e.downstream)
                if (other_up and caller.can_see(other_up.namespace)
                        and other_down and caller.can_see(other_down.namespace)):
                    result.append(e)
            return result

        return {
            "upstreams": visible(self._graph.upstreams(entity_id)),
            "downstreams": visible(self._graph.downstreams(entity_id)),
        }

    # ---------- 影响分析 ----------

    def analyze_impact(self, caller: Caller, entity_id: str,
                       columns: tuple[str, ...] = ()) -> list[ImpactItem]:
        action, target = "impact_analysis", entity_id
        try:
            self._visible_entity(caller, entity_id)
            items = self._analyzer.analyze(entity_id, columns)
            visible_items = []
            for item in items:
                ent = self._store.get(item.entity_id)
                if ent and caller.can_see(ent.namespace):
                    visible_items.append(item)
            self._audit.record(caller.caller_id, action, target, "SUCCESS",
                               f"impacted={len(visible_items)}")
            return visible_items
        except CatalogError as exc:
            self._audit.record(caller.caller_id, action, target, f"REJECTED:{exc.reason.value}", str(exc))
            raise

    # ---------- 审计 ----------

    def query_audit(self, caller: Caller, target: str | None = None) -> list[AuditRecord]:
        """管理员可见全部；普通调用方只能看到自己的操作记录。"""
        if caller.is_admin:
            return self._audit.query(target=target)
        return self._audit.query(target=target, caller_id=caller.caller_id)
