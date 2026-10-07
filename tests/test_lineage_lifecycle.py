"""Lineage lifecycle: incremental batch registration, revoke/rollback,
idempotency, impact convergence, audit trail and failure atomicity."""
import pytest

from app.errors import ValidationError
from app.lineage import assert_acyclic
from app.models import ColumnMapping, LineageEdge

from .conftest import make_entity

UP_FIELDS = [
    {"name": "a", "type": "long"},
    {"name": "b", "type": "string"},
    {"name": "c", "type": "double"},
]
DOWN_FIELDS = [
    {"name": "x", "type": "long"},
    {"name": "y", "type": "string"},
    {"name": "z", "type": "double"},
]


@pytest.fixture
def pair(service, alice):
    make_entity(service, alice, "up", fields=UP_FIELDS)
    make_entity(service, alice, "down", fields=DOWN_FIELDS)
    return service


def _mappings(svc, alice):
    upstream = svc.get_lineage(alice, "down")["upstream"]
    assert len(upstream) == 1  # endpoint pair is always a single edge
    return {(m.upstream, m.downstream) for m in upstream[0].columns}


# --- incremental batch registration ----------------------------------

def test_batches_merge_and_dedupe_into_one_edge(pair, alice):
    assert pair.register_lineage(alice, "up", "down",
                                 [{"upstream": "a", "downstream": "x"}]) is True
    assert pair.register_lineage(alice, "up", "down",
                                 [{"upstream": "b", "downstream": "y"},
                                  {"upstream": "c", "downstream": "z"}]) is True
    assert _mappings(pair, alice) == {("a", "x"), ("b", "y"), ("c", "z")}


def test_batch_with_nothing_new_changes_no_state(pair, alice):
    pair.register_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    # exact replay and another no-new-mapping batch both report False
    assert pair.register_lineage(alice, "up", "down",
                                 [{"upstream": "a", "downstream": "x"}]) is False
    assert pair.register_lineage(alice, "up", "down",
                                 [{"upstream": "a", "downstream": "x"}]) is False
    assert _mappings(pair, alice) == {("a", "x")}


def test_mixed_batch_reports_true_only_for_new_part(pair, alice):
    pair.register_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    # half already known, half new -> still one edge, merged result, True
    assert pair.register_lineage(alice, "up", "down",
                                 [{"upstream": "a", "downstream": "x"},
                                  {"upstream": "b", "downstream": "y"}]) is True
    assert _mappings(pair, alice) == {("a", "x"), ("b", "y")}


def test_incremental_batch_still_rejects_cycles(pair, alice):
    from app.errors import CycleDetectedError

    make_entity(pair, alice, "mid", fields=UP_FIELDS)
    pair.register_lineage(alice, "up", "mid", [{"upstream": "a", "downstream": "a"}])
    pair.register_lineage(alice, "mid", "down", [{"upstream": "a", "downstream": "x"}])
    # adding one more mapping onto the existing up->down edge would close
    # a cycle only on a *new* edge; here down->up is the closing edge:
    with pytest.raises(CycleDetectedError):
        pair.register_lineage(alice, "down", "up", [{"upstream": "x", "downstream": "a"}])
    # failed registration leaves nothing behind
    assert pair.get_lineage(alice, "up")["upstream"] == []


# --- full / partial revoke -------------------------------------------

def test_full_revoke_removes_the_edge(pair, alice):
    pair.register_lineage(alice, "up", "down",
                          [{"upstream": "a", "downstream": "x"},
                           {"upstream": "b", "downstream": "y"}])
    assert pair.revoke_lineage(alice, "up", "down") is True
    assert pair.get_lineage(alice, "down")["upstream"] == []
    assert pair.get_lineage(alice, "up")["downstream"] == []


def test_full_revoke_removes_table_level_edge_without_columns(pair, alice):
    # an edge registered at table level (no column mappings) is still real
    assert pair.register_lineage(alice, "up", "down") is True
    assert pair.revoke_lineage(alice, "up", "down") is True
    assert pair.get_lineage(alice, "down")["upstream"] == []
    assert pair.revoke_lineage(alice, "up", "down") is False


def test_partial_revoke_keeps_remaining_mappings(pair, alice):
    pair.register_lineage(alice, "up", "down",
                          [{"upstream": "a", "downstream": "x"},
                           {"upstream": "b", "downstream": "y"},
                           {"upstream": "c", "downstream": "z"}])
    assert pair.revoke_lineage(alice, "up", "down",
                               [{"upstream": "a", "downstream": "x"}]) is True
    assert _mappings(pair, alice) == {("b", "y"), ("c", "z")}


def test_revoke_of_missing_edge_or_mapping_is_idempotent_success(pair, alice):
    # never existed: successful no-op, returns False, no error
    assert pair.revoke_lineage(alice, "up", "down") is False
    assert pair.revoke_lineage(alice, "up", "down",
                               [{"upstream": "a", "downstream": "x"}]) is False

    pair.register_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    pair.revoke_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    # revoking again, and revoking a mapping that was never registered
    assert pair.revoke_lineage(alice, "up", "down",
                               [{"upstream": "a", "downstream": "x"}]) is False
    assert pair.revoke_lineage(alice, "up", "down",
                               [{"upstream": "b", "downstream": "y"}]) is False


def test_revoke_converges_lineage_query_and_impact_immediately(pair, alice):
    make_entity(pair, alice, "further", fields=DOWN_FIELDS)
    pair.register_lineage(alice, "up", "down",
                          [{"upstream": "a", "downstream": "x"},
                           {"upstream": "b", "downstream": "y"}])
    pair.register_lineage(alice, "down", "further",
                          [{"upstream": "x", "downstream": "x"},
                           {"upstream": "y", "downstream": "y"}])

    impacted = {i.entity_id: i.columns
                for i in pair.impact_analysis(alice, "up", ["a", "b"])}
    assert impacted == {"down": ["x", "y"], "further": ["x", "y"]}

    # revoke a->x: column-level impact must shrink on both hops straight away
    pair.revoke_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    assert _mappings(pair, alice) == {("b", "y")}
    impacted = {i.entity_id: i.columns
                for i in pair.impact_analysis(alice, "up", ["a", "b"])}
    assert impacted == {"down": ["y"], "further": ["y"]}
    # the revoked upstream column alone now reaches nothing at all
    assert pair.impact_analysis(alice, "up", ["a"]) == []

    # full revoke cuts the whole branch, for both column- and table-level
    pair.revoke_lineage(alice, "up", "down")
    assert pair.impact_analysis(alice, "up", ["a", "b"]) == []
    assert pair.impact_analysis(alice, "up") == []


def test_re_register_after_full_revoke_starts_clean(pair, alice):
    pair.register_lineage(alice, "up", "down",
                          [{"upstream": "a", "downstream": "x"},
                           {"upstream": "b", "downstream": "y"}])
    pair.revoke_lineage(alice, "up", "down")
    # old mappings must not resurface; only the fresh one is visible
    assert pair.register_lineage(alice, "up", "down",
                                 [{"upstream": "c", "downstream": "z"}]) is True
    assert _mappings(pair, alice) == {("c", "z")}


def test_partial_revoke_then_merge_back_is_explicit(pair, alice):
    pair.register_lineage(alice, "up", "down",
                          [{"upstream": "a", "downstream": "x"},
                           {"upstream": "b", "downstream": "y"}])
    pair.revoke_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    # re-adding a previously revoked mapping is a normal new registration
    assert pair.register_lineage(alice, "up", "down",
                                 [{"upstream": "a", "downstream": "x"}]) is True
    assert _mappings(pair, alice) == {("a", "x"), ("b", "y")}


# --- audit -------------------------------------------------------------

def test_revoke_is_audited_with_actor_scope_and_target(pair, alice, admin):
    pair.register_lineage(alice, "up", "down",
                          [{"upstream": "a", "downstream": "x"},
                           {"upstream": "b", "downstream": "y"}])
    pair.revoke_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    pair.revoke_lineage(alice, "up", "down")

    records = [r for r in pair.list_audit(admin) if r.action == "revoke_lineage"]
    assert len(records) == 2
    partial, full = records
    assert partial.actor == "alice"
    assert partial.target == "up->down"
    assert partial.outcome == "success"
    assert "scope=columns" in partial.detail
    assert "mappings_left=1" in partial.detail
    assert full.outcome == "success"
    assert "scope=edge" in full.detail
    assert "edge_removed=True" in full.detail
    # timestamps and sequence make "when" queryable and ordered
    assert partial.seq < full.seq
    assert partial.timestamp <= full.timestamp


def test_idempotent_revoke_audit_is_distinguishable(pair, alice, admin):
    pair.revoke_lineage(alice, "up", "down")
    rec = [r for r in pair.list_audit(admin) if r.action == "revoke_lineage"][0]
    assert rec.outcome == "success (idempotent no-op)"
    assert "removed=0" in rec.detail


# --- failure atomicity -------------------------------------------------

def test_register_batch_with_any_bad_column_changes_nothing(pair, alice):
    pair.register_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    with pytest.raises(ValidationError):
        pair.register_lineage(alice, "up", "down",
                              [{"upstream": "b", "downstream": "y"},
                               {"upstream": "nope", "downstream": "x"}])
    # the good mapping in that batch must not have landed either
    assert _mappings(pair, alice) == {("a", "x")}


def test_revoke_batch_with_any_bad_column_changes_nothing(pair, alice):
    pair.register_lineage(alice, "up", "down",
                          [{"upstream": "a", "downstream": "x"},
                           {"upstream": "b", "downstream": "y"}])
    with pytest.raises(ValidationError):
        pair.revoke_lineage(alice, "up", "down",
                            [{"upstream": "a", "downstream": "x"},
                             {"upstream": "nope", "downstream": "y"}])
    assert _mappings(pair, alice) == {("a", "x"), ("b", "y")}


def test_rejected_register_is_audited_and_graph_identical(pair, alice, admin):
    pair.register_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    before = pair.store.edges()
    with pytest.raises(ValidationError):
        pair.register_lineage(alice, "up", "down",
                              [{"upstream": "a", "downstream": "nope"}])
    assert pair.store.edges() == before
    rec = [r for r in pair.list_audit(admin) if r.action == "register_lineage"][-1]
    assert rec.outcome == "rejected"


# --- graph validity after revoke --------------------------------------

def test_graph_is_acyclic_after_partial_and_full_revoke(pair, alice):
    make_entity(pair, alice, "mid", fields=DOWN_FIELDS)
    pair.register_lineage(alice, "up", "mid", [{"upstream": "a", "downstream": "x"}])
    pair.register_lineage(alice, "mid", "down", [{"upstream": "x", "downstream": "x"}])
    pair.register_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "y"}])
    pair.revoke_lineage(alice, "up", "mid", [{"upstream": "a", "downstream": "x"}])
    assert_acyclic(pair.store.edges())  # partial revoke keeps valid graph
    pair.revoke_lineage(alice, "mid", "down")
    assert_acyclic(pair.store.edges())  # full revoke keeps valid graph
    assert pair.store.edges() == [
        LineageEdge("up", "down", (ColumnMapping("a", "y"),))
    ]
