"""FastAPI HTTP layer."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import Principal, authenticate
from app.errors import (
    CatalogError,
    ReasonCode,
    UnauthenticatedError,
)
from app.models import DataEntity, LineageEdge
from app.service import CatalogService

_STATUS_BY_CODE = {
    ReasonCode.INVALID_INPUT: 400,
    ReasonCode.UNAUTHENTICATED: 401,
    ReasonCode.FORBIDDEN: 403,
    ReasonCode.NOT_FOUND: 404,
    ReasonCode.REVISION_CONFLICT: 409,
    ReasonCode.CYCLE_DETECTED: 409,
    ReasonCode.FIELD_REMOVED: 422,
    ReasonCode.FIELD_TYPE_CHANGED: 422,
    ReasonCode.REQUIRED_FIELD_ADDED: 422,
    ReasonCode.FIELD_BECAME_REQUIRED: 422,
}


class RegisterEntityRequest(BaseModel):
    entity_id: str
    name: str
    namespace: str
    kind: str
    owner: str
    fields: list[dict[str, Any]]


class EvolveSchemaRequest(BaseModel):
    expected_revision: int
    fields: list[dict[str, Any]]


class RegisterLineageRequest(BaseModel):
    upstream_id: str
    downstream_id: str
    columns: list[dict[str, Any]] | None = None


class ImpactRequest(BaseModel):
    columns: list[str] | None = None


def _entity_view(e: DataEntity) -> dict[str, Any]:
    return {
        "entity_id": e.entity_id,
        "name": e.name,
        "namespace": e.namespace,
        "kind": e.kind.value,
        "owner": e.owner,
        "revision": e.revision,
        "schemas": [
            {
                "version": s.version,
                "fields": [
                    {"name": f.name, "type": f.type, "required": f.required}
                    for f in s.fields
                ],
            }
            for s in e.schemas
        ],
    }


def _edge_view(e: LineageEdge) -> dict[str, Any]:
    return {
        "upstream_id": e.upstream_id,
        "downstream_id": e.downstream_id,
        "columns": [
            {"upstream": m.upstream, "downstream": m.downstream} for m in e.columns
        ],
    }


def create_app(service: CatalogService | None = None) -> FastAPI:
    app = FastAPI(title="Metadata Catalog Service")
    app.state.service = service or CatalogService()

    @app.exception_handler(CatalogError)
    async def catalog_error_handler(_: Request, exc: CatalogError) -> JSONResponse:
        status = _STATUS_BY_CODE.get(exc.code, 500)
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code.value, "message": exc.message}},
        )

    def get_principal(x_api_key: str | None = Header(default=None)) -> Principal:
        return authenticate(x_api_key)

    def get_service() -> CatalogService:
        return app.state.service

    @app.post("/entities", status_code=201)
    def register_entity(
        body: RegisterEntityRequest,
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> dict[str, Any]:
        entity = svc.register_entity(
            principal,
            body.entity_id,
            body.name,
            body.namespace,
            body.kind,
            body.owner,
            body.fields,
        )
        return _entity_view(entity)

    @app.get("/entities")
    def list_entities(
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> list[dict[str, Any]]:
        return [_entity_view(e) for e in svc.list_entities(principal)]

    @app.get("/entities/{entity_id}")
    def get_entity(
        entity_id: str,
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> dict[str, Any]:
        return _entity_view(svc.get_entity(principal, entity_id))

    @app.post("/entities/{entity_id}/schema-versions", status_code=201)
    def evolve_schema(
        entity_id: str,
        body: EvolveSchemaRequest,
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> dict[str, Any]:
        entity = svc.evolve_schema(
            principal, entity_id, body.expected_revision, body.fields
        )
        return _entity_view(entity)

    @app.post("/lineage", status_code=201)
    def register_lineage(
        body: RegisterLineageRequest,
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> dict[str, Any]:
        added = svc.register_lineage(
            principal, body.upstream_id, body.downstream_id, body.columns
        )
        return {"added": added}

    @app.post("/lineage/revoke", status_code=200)
    def revoke_lineage(
        body: RegisterLineageRequest,
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> dict[str, Any]:
        removed = svc.revoke_lineage(
            principal, body.upstream_id, body.downstream_id, body.columns
        )
        return {"removed": removed}

    @app.get("/entities/{entity_id}/lineage")
    def get_lineage(
        entity_id: str,
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> dict[str, Any]:
        lineage = svc.get_lineage(principal, entity_id)
        return {
            "entity_id": lineage["entity_id"],
            "upstream": [_edge_view(e) for e in lineage["upstream"]],
            "downstream": [_edge_view(e) for e in lineage["downstream"]],
        }

    @app.post("/entities/{entity_id}/impact")
    def impact(
        entity_id: str,
        body: ImpactRequest,
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> dict[str, Any]:
        impacted = svc.impact_analysis(principal, entity_id, body.columns)
        return {
            "entity_id": entity_id,
            "impacted": [
                {"entity_id": i.entity_id, "depth": i.depth, "columns": i.columns}
                for i in impacted
            ],
        }

    @app.get("/audit")
    def audit(
        principal: Principal = Depends(get_principal),
        svc: CatalogService = Depends(get_service),
    ) -> list[dict[str, Any]]:
        return [
            {
                "seq": r.seq,
                "timestamp": r.timestamp,
                "actor": r.actor,
                "action": r.action,
                "target": r.target,
                "outcome": r.outcome,
                "detail": r.detail,
            }
            for r in svc.list_audit(principal)
        ]

    return app
