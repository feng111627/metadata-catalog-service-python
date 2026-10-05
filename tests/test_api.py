"""HTTP API 端到端测试：登记、演进、血缘、影响分析、权限不泄露。"""
import pytest
from fastapi.testclient import TestClient

from catalog.api import app

ALICE = {"X-Caller-Id": "alice", "X-Caller-Namespace": "finance"}
BOB = {"X-Caller-Id": "bob", "X-Caller-Namespace": "marketing"}
ADMIN = {"X-Caller-Id": "root", "X-Caller-Admin": "true"}

SCHEMA_V1 = {"fields": [
    {"name": "id", "type": "INT", "nullable": False},
    {"name": "amt", "type": "DOUBLE"},
]}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def register(client, headers, entity_id, namespace, schema=None):
    return client.post("/entities", headers=headers, json={
        "entity_id": entity_id, "namespace": namespace,
        "name": entity_id, "owner": "tester", "schema": schema or SCHEMA_V1,
    })


def test_full_lifecycle(client):
    assert register(client, ALICE, "api_raw", "finance").status_code == 201
    assert register(client, ALICE, "api_stg", "finance").status_code == 201

    # 兼容演进被接受
    r = client.put("/entities/api_raw/schema", headers=ALICE, json={
        "schema": {"fields": SCHEMA_V1["fields"] + [{"name": "note", "type": "STRING"}]},
        "expected_lock_version": 1,
    })
    assert r.status_code == 200 and r.json()["current_version"] == 2

    # 不兼容演进被拒绝，原因可区分（仅 id 类型 INT->STRING，其余字段保留）
    r = client.put("/entities/api_raw/schema", headers=ALICE, json={
        "schema": {"fields": [
            {"name": "id", "type": "STRING", "nullable": False},
            {"name": "amt", "type": "DOUBLE"},
            {"name": "note", "type": "STRING"},
        ]},
        "expected_lock_version": 2,
    })
    assert r.status_code == 409
    assert r.json()["reason"] == "INCOMPATIBLE_TYPE_CHANGED"

    # 血缘登记（幂等）与影响分析
    edge = {"upstream": "api_raw", "downstream": "api_stg", "column_lineage": [
        {"upstream_entity": "api_raw", "upstream_column": "id", "downstream_column": "id"},
    ]}
    assert client.post("/lineage", headers=ALICE, json=edge).status_code == 201
    assert client.post("/lineage", headers=ALICE, json=edge).status_code == 201  # 幂等
    lineage = client.get("/lineage/api_stg", headers=ALICE).json()
    assert len(lineage["upstreams"]) == 1

    r = client.post("/impact/api_raw", headers=ALICE, json={"columns": ["id"]})
    assert r.status_code == 200
    assert r.json()["impacted"] == [
        {"entity_id": "api_stg", "level": 1, "columns": ["id"]}]

    # 审计可还原
    audit = client.get("/audit", headers=ADMIN, params={"target": "api_raw"}).json()
    outcomes = [rec["outcome"] for rec in audit["records"]]
    assert "SUCCESS" in outcomes and "REJECTED:INCOMPATIBLE_TYPE_CHANGED" in outcomes


def test_permission_does_not_leak_existence(client):
    register(client, ALICE, "hidden_tbl", "finance")
    r_hidden = client.get("/entities/hidden_tbl", headers=BOB)
    r_missing = client.get("/entities/never_existed", headers=BOB)
    # 无权查看与不存在：状态码与响应体完全一致
    assert r_hidden.status_code == r_missing.status_code == 404
    assert r_hidden.json() == r_missing.json()


def test_invalid_input_not_silently_written(client):
    r = register(client, ALICE, "bad_schema", "finance", schema={"fields": []})
    assert r.status_code == 422 and r.json()["reason"] == "MISSING_FIELD"
    r = client.post("/entities", headers=ALICE, json={"entity_id": "x"})  # 缺字段
    assert r.status_code == 422
    r = register(client, ALICE, "bad_type", "finance",
                 schema={"fields": [{"name": "a", "type": "NOPE"}]})
    assert r.status_code == 422 and r.json()["reason"] == "INVALID_STRUCTURE"


def test_caller_identity_required(client):
    assert client.get("/entities/whatever").status_code == 404
