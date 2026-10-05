"""Schema evolution: compatible changes accepted, incompatible ones rejected
with distinguishable reason codes, malformed input never silently written."""
import pytest

from app.errors import IncompatibleSchemaError, ReasonCode, ValidationError

from .conftest import make_entity

BASE = [{"name": "id", "type": "long", "required": True},
        {"name": "name", "type": "string"}]


def test_adding_optional_field_is_compatible(service, alice):
    make_entity(service, alice, "t1", fields=BASE)
    updated = service.evolve_schema(alice, "t1", 1, BASE + [{"name": "tag", "type": "string"}])
    assert updated.current_schema.version == 2
    assert updated.revision == 2
    assert [f.name for f in updated.current_schema.fields] == ["id", "name", "tag"]


def test_type_widening_is_compatible(service, alice):
    make_entity(service, alice, "t1", fields=[{"name": "n", "type": "int"}])
    updated = service.evolve_schema(alice, "t1", 1, [{"name": "n", "type": "long"}])
    assert updated.current_schema.fields[0].type == "long"


@pytest.mark.parametrize("new_fields,code", [
    ([{"name": "id", "type": "long", "required": True}], ReasonCode.FIELD_REMOVED),
    ([{"name": "id", "type": "string", "required": True}, {"name": "name", "type": "string"}],
     ReasonCode.FIELD_TYPE_CHANGED),
    (BASE + [{"name": "req", "type": "string", "required": True}],
     ReasonCode.REQUIRED_FIELD_ADDED),
    ([{"name": "id", "type": "long", "required": True},
      {"name": "name", "type": "string", "required": True}],
     ReasonCode.FIELD_BECAME_REQUIRED),
], ids=["remove-field", "change-type", "add-required", "optional-to-required"])
def test_incompatible_changes_rejected_with_distinct_codes(service, alice, new_fields, code):
    make_entity(service, alice, "t1", fields=BASE)
    with pytest.raises(IncompatibleSchemaError) as exc_info:
        service.evolve_schema(alice, "t1", 1, new_fields)
    assert exc_info.value.code is code


def test_rejected_evolution_leaves_no_partial_write(service, alice):
    make_entity(service, alice, "t1", fields=BASE)
    with pytest.raises(IncompatibleSchemaError):
        service.evolve_schema(alice, "t1", 1, [{"name": "id", "type": "string", "required": True}])
    entity = service.get_entity(alice, "t1")
    assert entity.revision == 1
    assert len(entity.schemas) == 1
    assert entity.current_schema.fields[0].type == "long"


@pytest.mark.parametrize("bad_fields", [
    [],
    [{"type": "long"}],                                  # missing name
    [{"name": "id"}],                                    # missing type
    [{"name": "id", "type": 42}],                        # wrong type type
    [{"name": "id", "type": "long", "required": "yes"}], # required not bool
    [{"name": "id", "type": "long"}, {"name": "id", "type": "long"}],  # duplicate
    ["not-a-dict"],
], ids=["empty", "no-name", "no-type", "bad-type", "bad-required", "duplicate", "not-object"])
def test_malformed_fields_rejected(service, alice, bad_fields):
    with pytest.raises(ValidationError) as exc_info:
        make_entity(service, alice, "bad", fields=bad_fields)
    assert exc_info.value.code is ReasonCode.INVALID_INPUT
    # nothing was written
    with pytest.raises(Exception):
        service.get_entity(alice, "bad")


def test_rejection_is_audited(service, alice, admin):
    make_entity(service, alice, "t1", fields=BASE)
    with pytest.raises(IncompatibleSchemaError):
        service.evolve_schema(alice, "t1", 1, [{"name": "id", "type": "string", "required": True},
                                               {"name": "name", "type": "string"}])
    records = [r for r in service.list_audit(admin)
               if r.action == "evolve_schema" and r.outcome == "rejected"]
    assert len(records) == 1
    assert "FIELD_TYPE_CHANGED" in records[0].detail
