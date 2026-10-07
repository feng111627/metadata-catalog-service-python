"""Authorization boundaries: namespace scoping, no existence leakage, audit access."""
from .conftest import ADMIN, ALICE, BOB


def _register(client, headers, entity_id, namespace):
    return client.post("/entities", json={
        "entity_id": entity_id, "name": entity_id, "namespace": namespace,
        "kind": "table", "owner": "tester",
        "fields": [{"name": "id", "type": "long", "required": True}],
    }, headers=headers)


def test_unauthenticated_requests_rejected(client):
    assert client.get("/entities").status_code == 401
    assert client.get("/entities", headers={"X-API-Key": "wrong"}).status_code == 401


def test_list_entities_scoped_to_own_namespace(client):
    _register(client, ALICE, "a1", "analytics")
    _register(client, BOB, "b1", "billing")
    alice_ids = [e["entity_id"] for e in client.get("/entities", headers=ALICE).json()]
    bob_ids = [e["entity_id"] for e in client.get("/entities", headers=BOB).json()]
    admin_ids = [e["entity_id"] for e in client.get("/entities", headers=ADMIN).json()]
    assert alice_ids == ["a1"]
    assert bob_ids == ["b1"]
    assert sorted(admin_ids) == ["a1", "b1"]


def test_cross_namespace_read_indistinguishable_from_missing(client):
    _register(client, ALICE, "secret", "analytics")
    forbidden = client.get("/entities/secret", headers=BOB)
    missing = client.get("/entities/never-existed", headers=BOB)
    # same status and same body shape: no existence leakage
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json()["error"]["code"] == missing.json()["error"]["code"] == "NOT_FOUND"


def test_cross_namespace_write_rejected(client):
    resp = _register(client, BOB, "intrusion", "analytics")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_cross_namespace_schema_evolution_hidden(client):
    _register(client, ALICE, "a1", "analytics")
    resp = client.post("/entities/a1/schema-versions", headers=BOB, json={
        "expected_revision": 1,
        "fields": [{"name": "id", "type": "long", "required": True},
                   {"name": "x", "type": "string"}],
    })
    assert resp.status_code == 404  # not 403: do not reveal existence


def test_cross_namespace_lineage_hidden(client):
    _register(client, ALICE, "a1", "analytics")
    _register(client, BOB, "b1", "billing")
    resp = client.post("/lineage", headers=BOB,
                       json={"upstream_id": "a1", "downstream_id": "b1"})
    assert resp.status_code == 404


def test_cross_namespace_revoke_hidden_and_changes_nothing(client):
    _register(client, ALICE, "a1", "analytics")
    _register(client, ALICE, "a2", "analytics")
    client.post("/lineage", headers=ALICE,
                json={"upstream_id": "a1", "downstream_id": "a2"})
    # bob cannot see a1: revoke is indistinguishable from "not found"...
    resp = client.post("/lineage/revoke", headers=BOB,
                       json={"upstream_id": "a1", "downstream_id": "a2"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    # ...and the edge alice owns is untouched
    lineage = client.get("/entities/a2/lineage", headers=ALICE).json()
    assert [e["upstream_id"] for e in lineage["upstream"]] == ["a1"]


def test_impact_results_filtered_by_visibility(client):
    _register(client, ALICE, "src", "analytics")
    _register(client, ALICE, "mid", "analytics")
    _register(client, BOB, "sink", "billing")
    client.post("/lineage", headers=ADMIN, json={"upstream_id": "src", "downstream_id": "mid"})
    client.post("/lineage", headers=ADMIN, json={"upstream_id": "mid", "downstream_id": "sink"})
    alice_view = client.post("/entities/src/impact", headers=ALICE, json={}).json()
    admin_view = client.post("/entities/src/impact", headers=ADMIN, json={}).json()
    assert [i["entity_id"] for i in alice_view["impacted"]] == ["mid"]
    assert [i["entity_id"] for i in admin_view["impacted"]] == ["mid", "sink"]


def test_audit_log_admin_only(client):
    _register(client, ALICE, "a1", "analytics")
    assert client.get("/audit", headers=ALICE).status_code == 403
    assert client.get("/audit", headers=BOB).status_code == 403
    records = client.get("/audit", headers=ADMIN).json()
    assert len(records) >= 1
    rec = records[0]
    assert rec["actor"] == "alice"
    assert rec["action"] == "register_entity"
    assert rec["outcome"] == "success"
    assert rec["timestamp"]


def test_audit_records_rejections_with_reasons(client):
    _register(client, ALICE, "a1", "analytics")
    client.post("/entities/a1/schema-versions", headers=ALICE, json={
        "expected_revision": 1,
        "fields": [{"name": "id", "type": "string", "required": True}],
    })
    records = client.get("/audit", headers=ADMIN).json()
    rejected = [r for r in records if r["outcome"] == "rejected"]
    assert len(rejected) == 1
    assert "FIELD_TYPE_CHANGED" in rejected[0]["detail"]
