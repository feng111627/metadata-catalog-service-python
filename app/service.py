"""Business operations tying store, compatibility, lineage, audit and auth together."""
from __future__ import annotations

from app.audit import AuditLog
from app.auth import Principal, can_access
from app.compatibility import check_compatible
from app.errors import (
    CatalogError,
    ForbiddenError,
    NotFoundError,
    ReasonCode,
    ValidationError,
)
from app.lineage import ImpactedEntity, assert_no_cycle, downstream_impact
from app.models import (
    AuditRecord,
    ColumnMapping,
    DataEntity,
    EntityKind,
    Field,
    LineageEdge,
    SchemaVersion,
)
from app.store import CatalogStore


def _parse_fields(raw: list[dict]) -> tuple[Field, ...]:
    """Strictly validate and parse field definitions.

    Anything malformed (missing keys, wrong types, duplicates) is rejected;
    nothing is silently coerced or dropped.
    """
    if not isinstance(raw, list) or not raw:
        raise ValidationError("fields must be a non-empty list")
    fields: list[Field] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"fields[{i}] must be an object")
        name = item.get("name")
        ftype = item.get("type")
        required = item.get("required", False)
        if not isinstance(name, str) or not name:
            raise ValidationError(f"fields[{i}].name must be a non-empty string")
        if not isinstance(ftype, str) or not ftype:
            raise ValidationError(f"fields[{i}].type must be a non-empty string")
        if not isinstance(required, bool):
            raise ValidationError(f"fields[{i}].required must be a boolean")
        if name in seen:
            raise ValidationError(f"duplicate field name {name!r}")
        seen.add(name)
        fields.append(Field(name, ftype, required))
    return tuple(fields)


class CatalogService:
    def __init__(
        self, store: CatalogStore | None = None, audit: AuditLog | None = None
    ) -> None:
        self.store = store or CatalogStore()
        self.audit = audit or AuditLog()

    # --- helpers ------------------------------------------------------
    def _visible_entity(self, actor: Principal, entity_id: str) -> DataEntity:
        """Return the entity if the actor may see it.

        Missing entities and entities outside the actor's scope both raise
        NotFoundError, so callers cannot probe for existence.
        """
        entity = self.store.get_entity(entity_id)
        if entity is None or not can_access(actor, entity.namespace):
            raise NotFoundError(f"entity {entity_id!r}")
        return entity

    def _require_namespace_write(self, actor: Principal, namespace: str) -> None:
        if not can_access(actor, namespace):
            raise ForbiddenError(f"no write access to namespace {namespace!r}")

    # --- entities & schemas ------------------------------------------
    def register_entity(
        self,
        actor: Principal,
        entity_id: str,
        name: str,
        namespace: str,
        kind: str,
        owner: str,
        fields: list[dict],
    ) -> DataEntity:
        try:
            for label, value in (("entity_id", entity_id), ("name", name), ("namespace", namespace), ("owner", owner)):
                if not isinstance(value, str) or not value:
                    raise ValidationError(f"{label} must be a non-empty string")
            self._require_namespace_write(actor, namespace)
            try:
                entity_kind = EntityKind(kind)
            except ValueError:
                raise ValidationError(
                    f"kind must be one of {[k.value for k in EntityKind]}"
                ) from None
            parsed = _parse_fields(fields)
            entity = DataEntity(
                entity_id=entity_id,
                name=name,
                namespace=namespace,
                kind=entity_kind,
                owner=owner,
                schemas=[SchemaVersion(version=1, fields=parsed)],
                revision=1,
            )
            self.store.create_entity(entity)
        except CatalogError as e:
            self.audit.record(actor.name, "register_entity", entity_id, "rejected", f"{e.code}: {e.message}")
            raise
        self.audit.record(actor.name, "register_entity", entity_id, "success", f"namespace={namespace} kind={kind}")
        return entity

    def evolve_schema(
        self,
        actor: Principal,
        entity_id: str,
        expected_revision: int,
        fields: list[dict],
    ) -> DataEntity:
        try:
            parsed = _parse_fields(fields)
            entity = self._visible_entity(actor, entity_id)

            def mutate(e: DataEntity) -> None:
                current = e.current_schema
                assert current is not None
                check_compatible(current, SchemaVersion(current.version + 1, parsed))
                e.schemas.append(SchemaVersion(current.version + 1, parsed))

            updated = self.store.update_entity(entity_id, expected_revision, mutate)
        except CatalogError as e:
            self.audit.record(actor.name, "evolve_schema", entity_id, "rejected", f"{e.code}: {e.message}")
            raise
        new_version = updated.current_schema.version  # type: ignore[union-attr]
        self.audit.record(actor.name, "evolve_schema", entity_id, "success", f"schema_version={new_version}")
        return updated

    def get_entity(self, actor: Principal, entity_id: str) -> DataEntity:
        return self._visible_entity(actor, entity_id)

    def list_entities(self, actor: Principal) -> list[DataEntity]:
        entities = self.store.list_entities()
        return [e for e in entities if can_access(actor, e.namespace)]

    # --- lineage ------------------------------------------------------
    def register_lineage(
        self,
        actor: Principal,
        upstream_id: str,
        downstream_id: str,
        columns: list[dict] | None = None,
    ) -> bool:
        """Register a lineage edge. Idempotent; cycles are rejected."""
        try:
            upstream = self._visible_entity(actor, upstream_id)
            downstream = self._visible_entity(actor, downstream_id)
            mappings = self._parse_column_mappings(columns or [], upstream, downstream)
            edge = LineageEdge(upstream_id, downstream_id, mappings)
            added = self.store.add_edge_guarded(
                edge, lambda existing: assert_no_cycle(existing, edge)
            )
        except CatalogError as e:
            self.audit.record(actor.name, "register_lineage", f"{upstream_id}->{downstream_id}", "rejected", f"{e.code}: {e.message}")
            raise
        outcome = "success" if added else "success (idempotent replay)"
        self.audit.record(actor.name, "register_lineage", f"{upstream_id}->{downstream_id}", outcome)
        return added

    def _parse_column_mappings(
        self, raw: list[dict], upstream: DataEntity, downstream: DataEntity
    ) -> tuple[ColumnMapping, ...]:
        if not isinstance(raw, list):
            raise ValidationError("columns must be a list")
        up_cols = {f.name for f in (upstream.current_schema.fields if upstream.current_schema else ())}
        down_cols = {f.name for f in (downstream.current_schema.fields if downstream.current_schema else ())}
        mappings: list[ColumnMapping] = []
        seen: set[tuple[str, str]] = set()
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValidationError(f"columns[{i}] must be an object")
            up, down = item.get("upstream"), item.get("downstream")
            if not isinstance(up, str) or not isinstance(down, str) or not up or not down:
                raise ValidationError(f"columns[{i}] needs non-empty 'upstream' and 'downstream'")
            if up not in up_cols:
                raise ValidationError(f"upstream column {up!r} does not exist on {upstream.entity_id!r}")
            if down not in down_cols:
                raise ValidationError(f"downstream column {down!r} does not exist on {downstream.entity_id!r}")
            if (up, down) in seen:
                raise ValidationError(f"duplicate column mapping {up!r}->{down!r}")
            seen.add((up, down))
            mappings.append(ColumnMapping(up, down))
        return tuple(mappings)

    def get_lineage(self, actor: Principal, entity_id: str) -> dict:
        self._visible_entity(actor, entity_id)
        edges = self.store.edges()
        visible = {
            e.entity_id
            for e in self.store.list_entities()
            if can_access(actor, e.namespace)
        }
        upstream = sorted(
            (e for e in edges if e.downstream_id == entity_id and e.upstream_id in visible),
            key=lambda e: e.upstream_id,
        )
        downstream = sorted(
            (e for e in edges if e.upstream_id == entity_id and e.downstream_id in visible),
            key=lambda e: e.downstream_id,
        )
        return {"entity_id": entity_id, "upstream": upstream, "downstream": downstream}

    # --- impact & audit ----------------------------------------------
    def impact_analysis(
        self, actor: Principal, entity_id: str, columns: list[str] | None = None
    ) -> list[ImpactedEntity]:
        entity = self._visible_entity(actor, entity_id)
        if columns is not None:
            known = {f.name for f in (entity.current_schema.fields if entity.current_schema else ())}
            unknown = [c for c in columns if c not in known]
            if unknown:
                raise ValidationError(f"unknown columns on {entity_id!r}: {unknown}")
        impacted = downstream_impact(self.store.edges(), entity_id, columns)
        result = []
        for item in impacted:
            target = self.store.get_entity(item.entity_id)
            if target is not None and can_access(actor, target.namespace):
                result.append(item)
        self.audit.record(
            actor.name,
            "impact_analysis",
            entity_id,
            "success",
            f"columns={columns or 'ALL'} impacted={len(result)}",
        )
        return result

    def list_audit(self, actor: Principal) -> list[AuditRecord]:
        if not actor.is_admin:
            raise ForbiddenError("only admins may read the audit log")
        return self.audit.list()
