"""下线回滚：整条撤销、按列映射撤销、幂等、影响分析立即收敛、审计留痕、干净重登。"""
import pytest

from app.errors import ValidationError

from .conftest import make_entity

BATCH1 = [{"upstream": "id", "downstream": "rid"},
          {"upstream": "payload", "downstream": "clean"}]
BATCH2 = [{"upstream": "payload", "downstream": "note"}]


@pytest.fixture
def pipeline(service, alice):
    make_entity(service, alice, "raw",
                fields=[{"name": "id", "type": "long"},
                        {"name": "payload", "type": "string"}])
    make_entity(service, alice, "staging",
                fields=[{"name": "rid", "type": "long"},
                        {"name": "clean", "type": "string"},
                        {"name": "note", "type": "string"}])
    make_entity(service, alice, "mart",
                fields=[{"name": "sid", "type": "long"}])
    service.register_lineage(alice, "raw", "staging", BATCH1)
    service.register_lineage(alice, "raw", "staging", BATCH2)
    service.register_lineage(alice, "staging", "mart",
                             [{"upstream": "rid", "downstream": "sid"}])
    return service


def _raw_staging_mappings(svc, actor):
    edge = svc.get_lineage(actor, "staging")["upstream"][0]
    return sorted((m.upstream, m.downstream) for m in edge.columns)


def test_revoke_whole_edge_disappears_from_queries(pipeline, alice):
    assert len(pipeline.get_lineage(alice, "raw")["downstream"]) == 1
    assert pipeline.deregister_lineage(alice, "staging", "mart") is True
    assert pipeline.get_lineage(alice, "staging")["downstream"] == []
    assert pipeline.get_lineage(alice, "mart")["upstream"] == []


def test_revoke_whole_edge_is_idempotent(pipeline, alice):
    assert pipeline.deregister_lineage(alice, "raw", "staging") is True
    assert pipeline.deregister_lineage(alice, "raw", "staging") is False
    assert pipeline.deregister_lineage(alice, "raw", "staging") is False
    assert pipeline.get_lineage(alice, "staging")["upstream"] == []


def test_revoke_specific_mappings_keeps_single_edge(pipeline, alice):
    removed = pipeline.deregister_lineage(
        alice, "raw", "staging",
        [{"upstream": "id", "downstream": "rid"}],
    )
    assert removed is True
    # one edge remains with the other two mappings
    assert _raw_staging_mappings(pipeline, alice) == [
        ("payload", "clean"), ("payload", "note")
    ]
    assert len(pipeline.get_lineage(alice, "raw")["downstream"]) == 1


def test_revoke_mapping_is_idempotent_and_reports_no_change(pipeline, alice):
    ref = [{"upstream": "id", "downstream": "rid"}]
    assert pipeline.deregister_lineage(alice, "raw", "staging", ref) is True
    assert pipeline.deregister_lineage(alice, "raw", "staging", ref) is False
    # revoking a mapping that never existed is also a successful no-op
    assert pipeline.deregister_lineage(
        alice, "raw", "staging",
        [{"upstream": "id", "downstream": "note"}],
    ) is False


def test_revoking_last_mapping_removes_the_edge(pipeline, alice):
    pipeline.deregister_lineage(
        alice, "raw", "staging",
        [{"upstream": "id", "downstream": "rid"}],
    )
    pipeline.deregister_lineage(
        alice, "raw", "staging",
        [{"upstream": "payload", "downstream": "clean"},
         {"upstream": "payload", "downstream": "note"}],
    )
    assert pipeline.get_lineage(alice, "raw")["downstream"] == []
    assert pipeline.get_lineage(alice, "staging")["upstream"] == []


def test_impact_analysis_converges_immediately_after_revoke(pipeline, alice):
    # before revoke: raw.id propagates rid -> sid all the way to mart
    impacted = {i.entity_id: i for i in pipeline.impact_analysis(alice, "raw", ["id"])}
    assert set(impacted) == {"staging", "mart"}
    # revoke the first-hop mapping: chain breaks immediately
    pipeline.deregister_lineage(
        alice, "raw", "staging", [{"upstream": "id", "downstream": "rid"}]
    )
    assert pipeline.impact_analysis(alice, "raw", ["id"]) == []
    # whole-edge revoke: payload impact disappears too
    pipeline.deregister_lineage(alice, "raw", "staging")
    assert pipeline.impact_analysis(alice, "raw") == []


def test_revoke_whole_edge_converges_impact_for_remaining_columns(pipeline, alice):
    pipeline.deregister_lineage(alice, "raw", "staging")
    impacted = {i.entity_id: i for i in pipeline.impact_analysis(alice, "raw")}
    assert "staging" not in impacted and "mart" not in impacted


def test_reregistration_after_full_revoke_starts_clean(pipeline, alice):
    pipeline.deregister_lineage(alice, "raw", "staging")
    # old mappings must not resurface: register just one fresh mapping
    pipeline.register_lineage(
        alice, "raw", "staging", [{"upstream": "payload", "downstream": "clean"}]
    )
    assert _raw_staging_mappings(pipeline, alice) == [("payload", "clean")]
    # and re-registration did not bring the rid->sid path back to raw.id
    assert pipeline.impact_analysis(alice, "raw", ["id"]) == []


def test_reregister_after_partial_revoke_does_not_resurrect(pipeline, alice):
    pipeline.deregister_lineage(
        alice, "raw", "staging", [{"upstream": "id", "downstream": "rid"}]
    )
    # re-submit an old mapping alongside a still-live one: only the old is "new"
    added = pipeline.register_lineage(
        alice, "raw", "staging",
        [{"upstream": "id", "downstream": "rid"},
         {"upstream": "payload", "downstream": "clean"}],
    )
    assert added is True
    assert _raw_staging_mappings(pipeline, alice) == [
        ("id", "rid"), ("payload", "clean"), ("payload", "note")
    ]


def test_revoke_is_audited_with_actor_and_scope(pipeline, alice, admin):
    pipeline.deregister_lineage(
        alice, "raw", "staging", [{"upstream": "id", "downstream": "rid"}]
    )
    pipeline.deregister_lineage(alice, "raw", "staging")
    records = [r for r in pipeline.list_audit(admin)
               if r.action == "deregister_lineage"]
    assert len(records) == 2
    assert all(r.actor == "alice" for r in records)
    assert all(r.target == "raw->staging" for r in records)
    assert records[0].outcome == "success"
    assert "id" in records[0].detail and "rid" in records[0].detail
    assert records[1].outcome == "success"
    assert records[1].detail.startswith("scope=edge")


def test_idempotent_revoke_is_audited_as_noop(pipeline, alice, admin):
    pipeline.deregister_lineage(alice, "raw", "staging")
    pipeline.deregister_lineage(alice, "raw", "staging")
    records = [r for r in pipeline.list_audit(admin)
               if r.action == "deregister_lineage"]
    assert [r.outcome for r in records] == [
        "success", "success (idempotent no-op)"
    ]


def test_revoke_rejects_malformed_request_without_state_change(pipeline, alice):
    with pytest.raises(ValidationError):
        pipeline.deregister_lineage(alice, "raw", "staging", columns=[])
    with pytest.raises(ValidationError):
        pipeline.deregister_lineage(
            alice, "raw", "staging", [{"upstream": "id"}]
        )
    # nothing was revoked
    assert _raw_staging_mappings(pipeline, alice) == [
        ("id", "rid"), ("payload", "clean"), ("payload", "note")
    ]


def test_revoke_mapping_referencing_dropped_field_is_idempotent(service, alice):
    # a reference to a field that is not in the current schema: the revoked
    # mapping is simply "already absent" -- success, no error, no edge damage
    make_entity(service, alice, "t1",
                fields=[{"name": "id", "type": "long", "required": True}])
    make_entity(service, alice, "t2",
                fields=[{"name": "id", "type": "long", "required": True}])
    service.register_lineage(
        alice, "t1", "t2", [{"upstream": "id", "downstream": "id"}]
    )
    assert service.deregister_lineage(
        alice, "t1", "t2", [{"upstream": "ghost", "downstream": "id"}]
    ) is False
    # the live mapping is untouched
    edges = service.get_lineage(alice, "t2")["upstream"]
    assert [(m.upstream, m.downstream) for m in edges[0].columns] == [("id", "id")]


