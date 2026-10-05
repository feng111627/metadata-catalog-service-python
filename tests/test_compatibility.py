"""兼容性规则测试：兼容变更被接受，不兼容变更被带原因拒绝。"""
from catalog.compatibility import check_compatibility
from catalog.errors import RejectReason
from catalog.models import Field, FieldType, Schema


def schema(*fields: Field) -> Schema:
    return Schema(fields=tuple(fields))


def test_add_nullable_field_is_compatible():
    old = schema(Field("id", FieldType.INT, nullable=False))
    new = schema(Field("id", FieldType.INT, nullable=False),
                 Field("note", FieldType.STRING))
    assert check_compatibility(old, new).compatible


def test_remove_field_rejected():
    old = schema(Field("a", FieldType.INT), Field("b", FieldType.STRING))
    new = schema(Field("a", FieldType.INT))
    r = check_compatibility(old, new)
    assert not r.compatible and r.reason is RejectReason.INCOMPATIBLE_FIELD_REMOVED


def test_type_change_rejected():
    old = schema(Field("a", FieldType.STRING))
    new = schema(Field("a", FieldType.INT))
    r = check_compatibility(old, new)
    assert not r.compatible and r.reason is RejectReason.INCOMPATIBLE_TYPE_CHANGED


def test_safe_widening_accepted():
    old = schema(Field("a", FieldType.INT))
    new = schema(Field("a", FieldType.BIGINT))
    assert check_compatibility(old, new).compatible


def test_add_required_field_rejected():
    old = schema(Field("a", FieldType.INT))
    new = schema(Field("a", FieldType.INT), Field("b", FieldType.STRING, nullable=False))
    r = check_compatibility(old, new)
    assert not r.compatible and r.reason is RejectReason.INCOMPATIBLE_REQUIRED_ADDED


def test_nullability_tightening_rejected():
    old = schema(Field("a", FieldType.INT, nullable=True))
    new = schema(Field("a", FieldType.INT, nullable=False))
    r = check_compatibility(old, new)
    assert not r.compatible and r.reason is RejectReason.INCOMPATIBLE_NULLABILITY_TIGHTENED
