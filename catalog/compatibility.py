"""schema 演进兼容性判断（向后兼容：旧消费者仍能读取新 schema 产出的数据）。"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import RejectReason
from .models import FieldType, Schema

# 允许的类型 widening（安全提升），其余类型变更一律不兼容
_WIDENING: dict[FieldType, frozenset[FieldType]] = {
    FieldType.INT: frozenset({FieldType.BIGINT, FieldType.DOUBLE}),
    FieldType.BIGINT: frozenset({FieldType.DOUBLE}),
}


@dataclass(frozen=True)
class CompatibilityResult:
    compatible: bool
    reason: RejectReason | None = None
    detail: str = ""


def check_compatibility(old: Schema, new: Schema) -> CompatibilityResult:
    """判断 new 相对 old 是否向后兼容。

    规则（任一违反即不兼容，按字段名确定性顺序报告首个违例）：
    1. 删除已有字段 -> INCOMPATIBLE_FIELD_REMOVED
    2. 已有字段类型变更（除白名单 widening）-> INCOMPATIBLE_TYPE_CHANGED
    3. 已有字段 nullable -> not-null -> INCOMPATIBLE_NULLABILITY_TIGHTENED
    4. 新增 not-null 字段 -> INCOMPATIBLE_REQUIRED_ADDED
    5. 新增 nullable 字段 -> 兼容
    """
    old_map = old.field_map()
    new_map = new.field_map()

    for name in sorted(old_map):
        if name not in new_map:
            return CompatibilityResult(
                False, RejectReason.INCOMPATIBLE_FIELD_REMOVED,
                f"field '{name}' removed",
            )

    for name in sorted(old_map):
        old_f, new_f = old_map[name], new_map[name]
        if old_f.type != new_f.type:
            allowed = _WIDENING.get(old_f.type, frozenset())
            if new_f.type not in allowed:
                return CompatibilityResult(
                    False, RejectReason.INCOMPATIBLE_TYPE_CHANGED,
                    f"field '{name}' type changed {old_f.type.value} -> {new_f.type.value}",
                )
        if old_f.nullable and not new_f.nullable:
            return CompatibilityResult(
                False, RejectReason.INCOMPATIBLE_NULLABILITY_TIGHTENED,
                f"field '{name}' tightened to not-null",
            )

    for name in sorted(new_map):
        if name not in old_map and not new_map[name].nullable:
            return CompatibilityResult(
                False, RejectReason.INCOMPATIBLE_REQUIRED_ADDED,
                f"new field '{name}' is not nullable",
            )

    return CompatibilityResult(True)
