"""增量补登：同一对表分批登记列映射，合并去重、始终一条边、无新增不改状态。"""
import pytest

from app.errors import CycleDetectedError, ValidationError

from .conftest import make_entity

FIELDS_ABC = [
    {"name": "a", "type": "long"},
    {"name": "b", "type": "long"},
    {"name": "c", "type": "string"},
]
FIELDS_XYZ = [
    {"name": "x", "type": "long"},
    {"name": "y", "type": "long"},
    {"name": "z", "type": "string"},
]


@pytest.fixture
def pair(service, alice):
    make_entity(service, alice, "up", fields=FIELDS_ABC)
    make_entity(service, alice, "down", fields=FIELDS_XYZ)
    return service


def _mappings(svc, actor, downstream="down"):
    edges = svc.get_lineage(actor, downstream)["upstream"]
    assert len(edges) == 1
    return sorted((m.upstream, m.downstream) for m in edges[0].columns)


def test_batches_merge_into_single_deduplicated_edge(pair, alice):
    batch1 = [{"upstream": "a", "downstream": "x"},
              {"upstream": "b", "downstream": "y"}]
    batch2 = [{"upstream": "c", "downstream": "z"}]
    assert pair.register_lineage(alice, "up", "down", batch1) is True
    assert pair.register_lineage(alice, "up", "down", batch2) is True
    assert _mappings(pair, alice) == [("a", "x"), ("b", "y"), ("c", "z")]
    # the pair is still represented by exactly one edge, seen from both ends
    assert len(pair.get_lineage(alice, "down")["upstream"]) == 1
    assert len(pair.get_lineage(alice, "up")["downstream"]) == 1


def test_exact_replay_and_no_new_mappings_change_nothing(pair, alice):
    batch = [{"upstream": "a", "downstream": "x"},
             {"upstream": "b", "downstream": "y"}]
    assert pair.register_lineage(alice, "up", "down", batch) is True
    # identical replay: no state change, caller sees False
    assert pair.register_lineage(alice, "up", "down", batch) is False
    # subset replay: also nothing new
    assert pair.register_lineage(
        alice, "up", "down", [{"upstream": "a", "downstream": "x"}]
    ) is False
    # mappings within a batch are deduplicated too (overlapping partial batch)
    assert pair.register_lineage(
        alice, "up", "down",
        [{"upstream": "a", "downstream": "x"}, {"upstream": "c", "downstream": "z"}],
    ) is True
    assert _mappings(pair, alice) == [("a", "x"), ("b", "y"), ("c", "z")]


def test_return_value_distinguishes_new_vs_merged_mappings(pair, alice):
    assert pair.register_lineage(
        alice, "up", "down", [{"upstream": "a", "downstream": "x"}]
    ) is True
    assert pair.register_lineage(
        alice, "up", "down", [{"upstream": "a", "downstream": "x"}]
    ) is False
    assert pair.register_lineage(
        alice, "up", "down", [{"upstream": "b", "downstream": "y"}]
    ) is True


def test_failed_batch_is_all_or_nothing(pair, alice):
    pair.register_lineage(
        alice, "up", "down", [{"upstream": "a", "downstream": "x"}]
    )
    # one good + one mapping referencing a non-existent column: whole batch fails
    with pytest.raises(ValidationError):
        pair.register_lineage(
            alice, "up", "down",
            [{"upstream": "b", "downstream": "y"},
             {"upstream": "c", "downstream": "nope"}],
        )
    # the good half must not have landed; state is exactly as before
    assert _mappings(pair, alice) == [("a", "x")]


def test_adding_mappings_to_existing_pair_never_cycle_checks(pair, alice):
    # up -> down exists; a reverse pair would be a cycle and stays rejected,
    # but extending the existing forward pair with columns always succeeds
    pair.register_lineage(alice, "up", "down", [{"upstream": "a", "downstream": "x"}])
    with pytest.raises(CycleDetectedError):
        pair.register_lineage(alice, "down", "up")
    assert pair.register_lineage(
        alice, "up", "down", [{"upstream": "c", "downstream": "z"}]
    ) is True
    assert _mappings(pair, alice) == [("a", "x"), ("c", "z")]


def test_table_edge_then_column_batch_merges_into_same_edge(pair, alice):
    assert pair.register_lineage(alice, "up", "down") is True
    assert pair.register_lineage(
        alice, "up", "down", [{"upstream": "a", "downstream": "x"}]
    ) is True
    # still one edge, now carrying the column mapping
    edges = pair.get_lineage(alice, "down")["upstream"]
    assert len(edges) == 1
    assert [(m.upstream, m.downstream) for m in edges[0].columns] == [("a", "x")]


def test_column_order_in_merged_edge_is_deterministic(pair, alice):
    pair.register_lineage(
        alice, "up", "down", [{"upstream": "c", "downstream": "z"}]
    )
    pair.register_lineage(
        alice, "up", "down", [{"upstream": "a", "downstream": "x"}]
    )
    first = _mappings(pair, alice)
    for _ in range(5):
        assert _mappings(pair, alice) == first
    assert first == [("a", "x"), ("c", "z")]
