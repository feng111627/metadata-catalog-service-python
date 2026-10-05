"""Lineage: idempotent registration, cycle rejection, deterministic queries."""
import pytest

from app.errors import CycleDetectedError, ReasonCode, ValidationError

from .conftest import make_entity

FIELDS_AB = [{"name": "a", "type": "long"}, {"name": "b", "type": "string"}]
FIELDS_XY = [{"name": "x", "type": "long"}, {"name": "y", "type": "string"}]


@pytest.fixture
def three_entities(service, alice):
    make_entity(service, alice, "e1", fields=FIELDS_AB)
    make_entity(service, alice, "e2", fields=FIELDS_XY)
    make_entity(service, alice, "e3", fields=FIELDS_AB)
    return service


def test_table_level_registration_is_idempotent(three_entities, alice):
    assert three_entities.register_lineage(alice, "e1", "e2") is True
    assert three_entities.register_lineage(alice, "e1", "e2") is False
    assert three_entities.register_lineage(alice, "e1", "e2") is False
    lineage = three_entities.get_lineage(alice, "e2")
    assert len(lineage["upstream"]) == 1  # no duplicates


def test_column_level_registration_is_idempotent(three_entities, alice):
    cols = [{"upstream": "a", "downstream": "x"}]
    assert three_entities.register_lineage(alice, "e1", "e2", cols) is True
    assert three_entities.register_lineage(alice, "e1", "e2", cols) is False
    edge = three_entities.get_lineage(alice, "e2")["upstream"][0]
    assert [(m.upstream, m.downstream) for m in edge.columns] == [("a", "x")]


def test_direct_cycle_rejected(three_entities, alice):
    three_entities.register_lineage(alice, "e1", "e2")
    with pytest.raises(CycleDetectedError) as exc_info:
        three_entities.register_lineage(alice, "e2", "e1")
    assert exc_info.value.code is ReasonCode.CYCLE_DETECTED
    assert exc_info.value.path == ["e1", "e2", "e1"]


def test_indirect_cycle_rejected(three_entities, alice):
    three_entities.register_lineage(alice, "e1", "e2")
    three_entities.register_lineage(alice, "e2", "e3")
    with pytest.raises(CycleDetectedError) as exc_info:
        three_entities.register_lineage(alice, "e3", "e1")
    assert exc_info.value.path == ["e1", "e2", "e3", "e1"]


def test_self_loop_rejected(three_entities, alice):
    with pytest.raises(CycleDetectedError):
        three_entities.register_lineage(alice, "e1", "e1")


def test_rejected_cycle_leaves_graph_unchanged(three_entities, alice):
    three_entities.register_lineage(alice, "e1", "e2")
    with pytest.raises(CycleDetectedError):
        three_entities.register_lineage(alice, "e2", "e1")
    assert three_entities.get_lineage(alice, "e1")["downstream"]
    assert three_entities.get_lineage(alice, "e2")["downstream"] == []


def test_column_mapping_must_reference_existing_columns(three_entities, alice):
    with pytest.raises(ValidationError):
        three_entities.register_lineage(
            alice, "e1", "e2", [{"upstream": "nope", "downstream": "x"}]
        )
    with pytest.raises(ValidationError):
        three_entities.register_lineage(
            alice, "e1", "e2", [{"upstream": "a", "downstream": "nope"}]
        )


def test_lineage_queries_are_deterministic(three_entities, alice):
    three_entities.register_lineage(alice, "e3", "e2")
    three_entities.register_lineage(alice, "e1", "e2")
    three_entities.register_lineage(alice, "e1", "e3")
    first = three_entities.get_lineage(alice, "e2")
    for _ in range(5):
        again = three_entities.get_lineage(alice, "e2")
        assert [e.upstream_id for e in again["upstream"]] == \
               [e.upstream_id for e in first["upstream"]] == ["e1", "e3"]
