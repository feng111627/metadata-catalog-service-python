"""服务层测试：schema 演进、输入校验、权限边界、审计还原。"""
import pytest

from catalog.errors import (
    CompatibilityError,
    PermissionDeniedError,
    RejectReason,
    ValidationError,
)
from catalog.models import Caller, Field, FieldType, LineageEdge, Schema
from catalog.service import MetadataCatalogService

ADMIN = Caller("admin", frozenset(), is_admin=True)
ALICE = Caller("alice", frozenset({"finance"}))
BOB = Caller("bob", frozenset({"marketing"}))

SCHEMA_V1 = Schema((Field("id", FieldType.INT, nullable=False),
                    Field("amt", FieldType.DOUBLE)))
SCHEMA_V2_COMPAT = Schema((Field("id", FieldType.INT, nullable=False),
                           Field("amt", FieldType.DOUBLE),
                           Field("note", FieldType.STRING)))
SCHEMA_V2_BAD = Schema((Field("id", FieldType.INT, nullable=False),))


@pytest.fixture
def service() -> MetadataCatalogService:
    svc = MetadataCatalogService()
    svc.register_entity(ALICE, "orders", "finance", "orders", "alice", SCHEMA_V1)
    svc.register_entity(ALICE, "orders_stg", "finance", "orders_stg", "alice", SCHEMA_V1)
    svc.register_entity(BOB, "campaigns", "marketing", "campaigns", "bob", SCHEMA_V1)
    return svc


# ---- schema 演进 ----

def test_compatible_schema_update_accepted(service):
    updated = service.update_schema(ALICE, "orders", SCHEMA_V2_COMPAT, expected_lock_version=1)
    assert len(updated.schema_versions) == 2
    assert updated.current_schema.field_map()["note"].type is FieldType.STRING


def test_incompatible_schema_update_rejected_with_reason(service):
    with pytest.raises(CompatibilityError) as exc:
        service.update_schema(ALICE, "orders", SCHEMA_V2_BAD, expected_lock_version=1)
    assert exc.value.reason is RejectReason.INCOMPATIBLE_FIELD_REMOVED
    # 拒绝后实体保持原版本
    assert len(service.get_entity(ALICE, "orders").schema_versions) == 1


# ---- 输入校验：缺失/非法输入不能静默写入 ----

def test_empty_schema_rejected(service):
    with pytest.raises(ValidationError) as exc:
        service.register_entity(ALICE, "bad", "finance", "bad", "alice", Schema(()))
    assert exc.value.reason is RejectReason.MISSING_FIELD
    with pytest.raises(PermissionDeniedError):  # 未写入（对同范围调用方也不可见）
        service.get_entity(BOB, "bad")


def test_duplicate_field_rejected(service):
    dup = Schema((Field("a", FieldType.INT), Field("a", FieldType.STRING)))
    with pytest.raises(ValidationError) as exc:
        service.register_entity(ALICE, "dup", "finance", "dup", "alice", dup)
    assert exc.value.reason is RejectReason.INVALID_STRUCTURE


def test_missing_name_rejected(service):
    with pytest.raises(ValidationError) as exc:
        service.register_entity(ALICE, "x", "finance", "", "alice", SCHEMA_V1)
    assert exc.value.reason is RejectReason.MISSING_FIELD


# ---- 权限边界：不暴露实体是否存在 ----

def test_cross_namespace_read_denied_same_error_as_not_found(service):
    with pytest.raises(PermissionDeniedError) as exc_hidden:
        service.get_entity(BOB, "orders")          # 存在但无权
    with pytest.raises(PermissionDeniedError) as exc_missing:
        service.get_entity(BOB, "no_such_entity")  # 根本不存在
    # 两种情况的错误类型与文案完全一致，无法区分实体是否存在
    assert exc_hidden.value.reason is exc_missing.value.reason is RejectReason.PERMISSION_DENIED
    assert str(exc_hidden.value) == str(exc_missing.value)


def test_cross_namespace_write_denied(service):
    with pytest.raises(PermissionDeniedError):
        service.update_schema(BOB, "orders", SCHEMA_V2_COMPAT, expected_lock_version=1)
    with pytest.raises(PermissionDeniedError):
        service.register_lineage(BOB, LineageEdge("orders", "campaigns"))


def test_admin_sees_everything(service):
    assert service.get_entity(ADMIN, "orders").name == "orders"
    assert service.get_entity(ADMIN, "campaigns").name == "campaigns"


# ---- 血缘经服务登记（含列校验）----

def test_lineage_with_invalid_column_rejected(service):
    from catalog.models import ColumnLineage
    edge = LineageEdge("orders", "orders_stg",
                       column_lineage=(ColumnLineage("orders", "ghost_col", "id"),))
    with pytest.raises(ValidationError) as exc:
        service.register_lineage(ALICE, edge)
    assert exc.value.reason is RejectReason.COLUMN_NOT_FOUND


def test_lineage_visibility_filtered(service):
    service.register_lineage(ALICE, LineageEdge("orders", "orders_stg"))
    service.register_lineage(ADMIN, LineageEdge("campaigns", "orders_stg"))
    alice_view = service.get_lineage(ALICE, "orders_stg")
    # alice 看不到 marketing 命名空间的 campaigns 边
    assert [e.upstream for e in alice_view["upstreams"]] == ["orders"]
    admin_view = service.get_lineage(ADMIN, "orders_stg")
    assert {e.upstream for e in admin_view["upstreams"]} == {"orders", "campaigns"}


# ---- 审计：谁在什么时间改了什么、结论是什么 ----

def test_audit_trail_records_success_and_rejection(service):
    service.update_schema(ALICE, "orders", SCHEMA_V2_COMPAT, expected_lock_version=1)
    with pytest.raises(CompatibilityError):
        service.update_schema(ALICE, "orders", SCHEMA_V2_BAD, expected_lock_version=2)

    records = service.query_audit(ADMIN, target="orders")
    updates = [r for r in records if r.action == "update_schema"]
    assert len(updates) == 2
    assert updates[0].caller_id == "alice" and updates[0].outcome == "SUCCESS"
    assert updates[1].outcome == "REJECTED:INCOMPATIBLE_FIELD_REMOVED"
    assert updates[0].timestamp <= updates[1].timestamp


def test_audit_visibility_scoped(service):
    service.update_schema(ALICE, "orders", SCHEMA_V2_COMPAT, expected_lock_version=1)
    # 普通调用方只能看到自己的记录
    assert service.query_audit(BOB, target="orders") == []
    assert len(service.query_audit(ALICE, target="orders")) >= 1
    assert len(service.query_audit(ADMIN, target="orders")) >= 1
