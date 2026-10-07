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


def test_incremental_lineage_batches_merge_into_one_edge(client):
    _register(client, "x")
    _register(client, "y")
    first = client.post("/lineage", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "id", "downstream": "id"}]})
    second = client.post("/lineage", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "id", "downstream": "id"}]})
    assert first.status_code == 201
    assert first.json()["added"] is True
    assert second.status_code == 201  # replay stays a successful call...
    assert second.json()["added"] is False  # ...that visibly changed nothing
    lineage = client.get("/entities/y/lineage", headers=ALICE).json()
    assert len(lineage["upstream"]) == 1
    assert lineage["upstream"][0]["columns"] == [
        {"upstream": "id", "downstream": "id"}]


def test_revoke_endpoint_partial_full_and_idempotent(client):
    _register(client, "x")
    _register(client, "y")
    client.post("/lineage", headers=ALICE, json={"upstream_id": "x", "downstream_id": "y"})

    # full revoke: first call removes, replay is an idempotent success no-op
    resp = client.post("/lineage/revoke", headers=ALICE,
                       json={"upstream_id": "x", "downstream_id": "y"})
    replay = client.post("/lineage/revoke", headers=ALICE,
                         json={"upstream_id": "x", "downstream_id": "y"})
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert replay.status_code == 200
    assert replay.json()["removed"] is False
    assert client.get("/entities/y/lineage", headers=ALICE).json()["upstream"] == []


def test_revoke_unknown_column_returns_400_and_changes_nothing(client):
    _register(client, "x")
    _register(client, "y")
    client.post("/lineage", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "id", "downstream": "id"}]})
    resp = client.post("/lineage/revoke", headers=ALICE, json={
        "upstream_id": "x", "downstream_id": "y",
        "columns": [{"upstream": "nope", "downstream": "id"}]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_INPUT"
    lineage = client.get("/entities/y/lineage", headers=ALICE).json()
    assert lineage["upstream"][0]["columns"] == [
        {"upstream": "id", "downstream": "id"}]  # no partial removal
