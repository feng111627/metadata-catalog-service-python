"""Schema evolution compatibility checks."""
from __future__ import annotations

from app.models import SchemaVersion


# Type widening pairs that are considered backward compatible:
# (old_type, new_type) -> allowed
_WIDENING: frozenset[tuple[str, str]] = frozenset(
    {
        ("int", "long"),
        ("int", "double"),
        ("long", "double"),
        ("float", "double"),
    }
)


def check_compatible(old: SchemaVersion, new: SchemaVersion) -> None:
    """Raise IncompatibleSchemaError if `new` is not a compatible evolution of `old`.

    Rules:
    - removing a field is incompatible (FIELD_REMOVED)
    - changing a field type is incompatible unless it is a known widening
      (FIELD_TYPE_CHANGED)
    - making an optional field required is incompatible (FIELD_BECAME_REQUIRED)
    - adding a required field is incompatible (REQUIRED_FIELD_ADDED)
    - adding an optional field is compatible
    """
    from app.errors import IncompatibleSchemaError, ReasonCode

    old_fields = {f.name: f for f in old.fields}
    new_fields = {f.name: f for f in new.fields}

    for name in old_fields:
        if name not in new_fields:
            raise IncompatibleSchemaError(
                ReasonCode.FIELD_REMOVED, f"field {name!r} was removed"
            )

    for name, old_f in old_fields.items():
        new_f = new_fields[name]
        if new_f.type != old_f.type and (old_f.type, new_f.type) not in _WIDENING:
            raise IncompatibleSchemaError(
                ReasonCode.FIELD_TYPE_CHANGED,
                f"field {name!r} type changed from {old_f.type!r} to {new_f.type!r}",
            )
        if new_f.required and not old_f.required:
            raise IncompatibleSchemaError(
                ReasonCode.FIELD_BECAME_REQUIRED,
                f"field {name!r} changed from optional to required",
            )

    for name, new_f in new_fields.items():
        if name not in old_fields and new_f.required:
            raise IncompatibleSchemaError(
                ReasonCode.REQUIRED_FIELD_ADDED,
                f"new field {name!r} is required; only optional fields may be added",
            )
