"""HTTP API 层：把门面服务暴露为 REST 接口。

身份通过请求头注入（演示用）：
- X-Caller-Id: 调用方标识（必填）
- X-Caller-Namespace: 可见命名空间，逗号分隔
- X-Caller-Admin: "true" 表示管理员
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .errors import (
    CatalogError,
    ConcurrencyError,
    CompatibilityError,
    LineageError,
    PermissionDeniedError,
    RejectReason,
    ValidationError,
)
from .models import Caller, ColumnLineage, Field, FieldType, LineageEdge, Schema
from .service import MetadataCatalogService

service = MetadataCatalogService()


def get_caller(
    x_caller_id: str | None = Header(default=None),
    x_caller_namespace: str = Header(default=""),
    x_caller_admin: str = Header(default=""),
) -> Caller:
    if not x_caller_id:
        raise PermissionDeniedError("caller identity required")
    namespaces = frozenset(ns.strip() for ns in x_caller_namespace.split(",") if ns.strip())
    return Caller(x_caller_id, namespaces, is_admin=x_caller_admin.lower() == "true")


def parse_schema(payload: dict) -> Schema:
    if not isinstance(payload, dict) or "fields" not in payload:
        raise ValidationError(RejectReason.MISSING_FIELD, "schema.fields is required")
    fields = payload["fields"]
    if not isinstance(fields, list):
        raise ValidationError(RejectReason.INVALID_STRUCTURE, "schema.fields must be a list")
    parsed = []
    for f in fields:
        if not isinstance(f, dict):
            raise ValidationError(RejectReason.INVALID_STRUCTURE, "field must be an object")
        if "name" not in f or "type" not in f:
            raise ValidationError(RejectReason.MISSING_FIELD, "field requires name and type")
        try:
            ftype = FieldType(f["type"])
        except ValueError:
            raise ValidationError(RejectReason.INVALID_STRUCTURE,
                                  f"unknown field type '{f['type']}'") from None
        parsed.append(Field(name=f["name"], type=ftype,
                            nullable=bool(f.get("nullable", True)),
                            comment=str(f.get("comment", ""))))
    return Schema(fields=tuple(parsed))


def entity_view(e) -> dict:
    return {
        "entity_id": e.entity_id, "namespace": e.namespace, "name": e.name,
        "owner": e.owner, "lock_version": e.lock_version,
        "current_version": len(e.schema_versions),
        "schema": {"fields": [
            {"name": f.name, "type": f.type.value, "nullable": f.nullable,
             "comment": f.comment}
            for f in e.current_schema.fields
        ]} if e.current_schema else None,
    }


def edge_view(e: LineageEdge) -> dict:
    return {
        "upstream": e.upstream, "downstream": e.downstream,
        "column_lineage": [
            {"upstream_entity": c.upstream_entity, "upstream_column": c.upstream_column,
             "downstream_column": c.downstream_column}
            for c in e.column_lineage
        ],
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Metadata Catalog Service")

    @app.exception_handler(CatalogError)
    async def catalog_error_handler(request: Request, exc: CatalogError) -> JSONResponse:
        # 权限错误统一返回 404 + 固定文案，不暴露实体是否存在
        if isinstance(exc, PermissionDeniedError):
            return JSONResponse(status_code=404, content={
                "error": "access denied",
                "reason": RejectReason.PERMISSION_DENIED.value})
        status = 422 if isinstance(exc, ValidationError) else 409
        return JSONResponse(status_code=status,
                            content={"error": str(exc), "reason": exc.reason.value})

    @app.post("/entities", status_code=201)
    def register_entity(payload: dict, caller: Caller = Depends(get_caller)):
        for key in ("entity_id", "namespace", "name", "owner", "schema"):
            if key not in payload:
                raise ValidationError(RejectReason.MISSING_FIELD, f"'{key}' is required")
        entity = service.register_entity(
            caller, payload["entity_id"], payload["namespace"],
            payload["name"], payload["owner"], parse_schema(payload["schema"]),
        )
        return entity_view(entity)

    @app.get("/entities/{entity_id}")
    def get_entity(entity_id: str, caller: Caller = Depends(get_caller)):
        return entity_view(service.get_entity(caller, entity_id))

    @app.put("/entities/{entity_id}/schema")
    def update_schema(entity_id: str, payload: dict, caller: Caller = Depends(get_caller)):
        if "schema" not in payload or "expected_lock_version" not in payload:
            raise ValidationError(RejectReason.MISSING_FIELD,
                                  "'schema' and 'expected_lock_version' are required")
        entity = service.update_schema(caller, entity_id, parse_schema(payload["schema"]),
                                       int(payload["expected_lock_version"]))
        return entity_view(entity)

    @app.post("/lineage", status_code=201)
    def register_lineage(payload: dict, caller: Caller = Depends(get_caller)):
        if "upstream" not in payload or "downstream" not in payload:
            raise ValidationError(RejectReason.MISSING_FIELD,
                                  "'upstream' and 'downstream' are required")
        cols = tuple(
            ColumnLineage(c["upstream_entity"], c["upstream_column"], c["downstream_column"])
            for c in payload.get("column_lineage", [])
        )
        edge = service.register_lineage(
            caller, LineageEdge(payload["upstream"], payload["downstream"], cols))
        return edge_view(edge)

    @app.get("/lineage/{entity_id}")
    def get_lineage(entity_id: str, caller: Caller = Depends(get_caller)):
        result = service.get_lineage(caller, entity_id)
        return {k: [edge_view(e) for e in v] for k, v in result.items()}

    @app.post("/impact/{entity_id}")
    def analyze_impact(entity_id: str, payload: dict | None = None,
                       caller: Caller = Depends(get_caller)):
        columns = tuple((payload or {}).get("columns", ()))
        items = service.analyze_impact(caller, entity_id, columns)
        return {"impacted": [
            {"entity_id": i.entity_id, "level": i.level, "columns": list(i.columns)}
            for i in items
        ]}

    @app.get("/audit")
    def query_audit(target: str | None = None, caller: Caller = Depends(get_caller)):
        records = service.query_audit(caller, target=target)
        return {"records": [
            {"timestamp": r.timestamp.isoformat(), "caller_id": r.caller_id,
             "action": r.action, "target": r.target, "outcome": r.outcome,
             "detail": r.detail}
            for r in records
        ]}

    return app


app = create_app()
