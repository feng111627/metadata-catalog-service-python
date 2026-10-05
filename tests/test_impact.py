"""Impact analysis: downstream entities, columns and depth levels; audited."""
import pytest

from app.errors import ValidationError

from .conftest import make_entity


@pytest.fixture
def pipeline(service, alice):
    # raw -> staging -> mart, plus a side table joined into mart
    make_entity(service, alice, "raw",
                fields=[{"name": "id", "type": "long"}, {"name": "payload", "type": "string"}])
    make_entity(service, alice, "staging",
                fields=[{"name": "rid", "type": "long"}, {"name": "clean", "type": "string"}])
    make_entity(service, alice, "mart",
                fields=[{"name": "sid", "type": "long"}, {"name": "label", "type": "string"}])
    service.register_lineage(alice, "raw", "staging",
                             [{"upstream": "id", "downstream": "rid"},
                              {"upstream": "payload", "downstream": "clean"}])
    service.register_lineage(alice, "staging", "mart",
                             [{"upstream": "rid", "downstream": "sid"}])
    return service


def test_table_level_impact_gives_all_depths(pipeline, alice):
    impacted = pipeline.impact_analysis(alice, "raw")
    by_id = {i.entity_id: i for i in impacted}
    assert by_id["staging"].depth == 1
    assert by_id["mart"].depth == 2
    assert by_id["staging"].columns == ["clean", "rid"]
    assert by_id["mart"].columns == ["sid"]


def test_column_level_impact_propagates_only_mapped_columns(pipeline, alice):
    impacted = pipeline.impact_analysis(alice, "raw", ["id"])
    by_id = {i.entity_id: i for i in impacted}
    assert by_id["staging"].columns == ["rid"]
    assert by_id["mart"].columns == ["sid"]  # rid -> sid propagates one more level


def test_column_level_impact_stops_when_mapping_chain_ends(pipeline, alice):
    impacted = pipeline.impact_analysis(alice, "raw", ["payload"])
    by_id = {i.entity_id: i for i in impacted}
    assert by_id["staging"].columns == ["clean"]
    assert "mart" not in by_id  # 'clean' feeds nothing downstream


def test_impact_on_leaf_is_empty(pipeline, alice):
    assert pipeline.impact_analysis(alice, "mart") == []


def test_impact_with_unknown_column_rejected(pipeline, alice):
    with pytest.raises(ValidationError):
        pipeline.impact_analysis(alice, "raw", ["nope"])


def test_impact_results_are_deterministic(pipeline, alice):
    first = pipeline.impact_analysis(alice, "raw")
    for _ in range(5):
        again = pipeline.impact_analysis(alice, "raw")
        assert [(i.entity_id, i.depth, i.columns) for i in again] == \
               [(i.entity_id, i.depth, i.columns) for i in first]


def test_impact_analysis_is_audited(pipeline, alice, admin):
    pipeline.impact_analysis(alice, "raw", ["id"])
    records = [r for r in pipeline.list_audit(admin) if r.action == "impact_analysis"]
    assert len(records) == 1
    assert records[0].actor == "alice"
    assert records[0].target == "raw"
    assert records[0].outcome == "success"
    assert "impacted=2" in records[0].detail
