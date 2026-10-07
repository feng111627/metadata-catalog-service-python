"""HTTP-level semantics: error codes map to stable, distinguishable responses."""
from .conftest import ALICE


def _register(client, entity_id="t1"):
    return client.post("/entities", json={
        "entity_id": entity_id, "name": entity_id, "namespace": "analytics",
        "kind": "table", "owner": "alice",
        "fields": [{"name": "id", "type": "long", "required": True}],
    }, headers=ALICE)


def test_register_and_get_roundtrip(client):
    assert _register(client).status_code == 201
    body = client.get("/entities/t1", headers=ALICE).json()
    assert body["revision"] == 1
    assert body["schemas"][0]["version"] == 1
    assert body["schemas"][0]["fields"][0]["name"] == "id"


def test_incompatible_evolution_returns_422_with_reason(client):
    _register(client)
    resp = client.post("/entities/t1/schema-versions", headers=ALICE, json={
        "expected_revision": 1,
        "fields": [{"name": "id", "type": "string", "required": True}],
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "FIELD_TYPE_CHANGED"


def test_revision_conflict_returns_409(client):
    _register(client)
    payload = {"expected_revision": 1,
               "fields": [{"name": "id", "type": "long", "required": True},
                          {"name": "x", "type": "string"}]}
    assert client.post("/entities/t1/schema-versions", headers=ALICE, json=payload).status_code == 201
    resp = client.post("/entities/t1/schema-versions", headers=ALICE, json=payload)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVISION_CONFLICT"


def test_invalid_input_returns_400(client):
    resp = client.post("/entities", json={
        "entity_id": "bad", "name": "bad", "namespace": "analytics",
        "kind": "table", "owner": "alice", "fields": [],
    }, headers=ALICE)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"


def test_cycle_returns_409_with_path(client):
    _register(client, "x")
    _register(client, "y")
    client.post("/lineage", headers=ALICE, json={"upstream_id": "x", "downstream_id": "y"})
    resp = client.post("/lineage", headers=ALICE, json={"upstream_id": "y", "downstream_id": "x"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CYCLE_DETECTED"
    assert "x -> y -> x" in resp.json()["error"]["message"]


def test_lineage_replay_returns_added_false(client):
    _register(client, "x")
    _register(client, "y")
    first = client.post("/lineage", headers=ALICE, json={"upstream_id": "x", "downstream_id": "y"})
    replay = client.post("/lineage", headers=ALICE, json={"upstream_id": "x", "downstream_id": "y"})
    assert first.json()["added"] is True
    assert replay.json()["added"] is False


def _register_two_with_columns(client):
    client.post("/entities", json={
        "entity_id": "x", "name": "x", "namespace": "analytics",
        "kind": "table", "owner": "alice",
        "fields": [{"name": "a", "type": "long"}, {"name": "b", "type": "long"}],
    }, headers=ALICE)
    client.post("/entities", json={
        "entity_id": "y", "name": "y", "namespace": "analytics",
        "kind": "table", "owner": "alice",
        "fields": [{"name": "p", "type": "long"}, {"name": "q", "type": "long"}],
    }, headers=ALICE)


def test_batch_lineage_merges_over_http(client):
    _register_two_with_columns(client)
    r1 = client.post("/lineage", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "a", "downstream": "p"}]})
    r2 = client.post("/lineage", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "b", "downstream": "q"}]})
    assert r1.json()["added"] is True
    assert r2.json()["added"] is True
    body = client.get("/entities/y/lineage", headers=ALICE).json()
    assert len(body["upstream"]) == 1
    assert body["upstream"][0]["columns"] == [
        {"upstream": "a", "downstream": "p"},
        {"upstream": "b", "downstream": "q"},
    ]


def test_deregister_mappings_and_whole_edge_over_http(client):
    _register_two_with_columns(client)
    client.post("/lineage", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "a", "downstream": "p"},
                    {"upstream": "b", "downstream": "q"}]})
    partial = client.post("/lineage/deregister", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "a", "downstream": "p"}]})
    assert partial.status_code == 200
    assert partial.json()["removed"] is True
    replay = client.post("/lineage/deregister", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "a", "downstream": "p"}]})
    assert replay.json()["removed"] is False
    whole = client.post("/lineage/deregister", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y"})
    assert whole.json()["removed"] is True
    body = client.get("/entities/y/lineage", headers=ALICE).json()
    assert body["upstream"] == []


def test_deregister_unknown_edge_is_200_idempotent(client):
    _register(client, "x")
    _register(client, "y")
    resp = client.post("/lineage/deregister", headers=ALICE,
                       json={"upstream_id": "x", "downstream_id": "y"})
    assert resp.status_code == 200
    assert resp.json()["removed"] is False


def test_deregister_malformed_columns_returns_400(client):
    _register_two_with_columns(client)
    resp = client.post("/lineage/deregister", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y", "columns": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"
