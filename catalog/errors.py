"""统一的错误类型与可区分的拒绝原因码。"""
from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    """可区分的拒绝原因码。"""

    # 输入校验
    MISSING_FIELD = "MISSING_FIELD"          # 必填字段缺失
    INVALID_STRUCTURE = "INVALID_STRUCTURE"  # 结构非法
    # 兼容性
    INCOMPATIBLE_FIELD_REMOVED = "INCOMPATIBLE_FIELD_REMOVED"
    INCOMPATIBLE_TYPE_CHANGED = "INCOMPATIBLE_TYPE_CHANGED"
    INCOMPATIBLE_REQUIRED_ADDED = "INCOMPATIBLE_REQUIRED_ADDED"
    INCOMPATIBLE_NULLABILITY_TIGHTENED = "INCOMPATIBLE_NULLABILITY_TIGHTENED"
    # 血缘
    CYCLE_DETECTED = "CYCLE_DETECTED"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    COLUMN_NOT_FOUND = "COLUMN_NOT_FOUND"
    # 并发
    VERSION_CONFLICT = "VERSION_CONFLICT"
    # 权限
    PERMISSION_DENIED = "PERMISSION_DENIED"


class CatalogError(Exception):
    """所有业务错误的基类，携带可区分的原因码。"""

    def __init__(self, reason: RejectReason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class ValidationError(CatalogError):
    """输入字段缺失或结构非法。"""


class CompatibilityError(CatalogError):
    """schema 演进不兼容。"""


class LineageError(CatalogError):
    """血缘登记/查询错误（含循环血缘）。"""


class ConcurrencyError(CatalogError):
    """乐观锁冲突。"""


class PermissionDeniedError(CatalogError):
    """越权访问（不暴露实体是否存在）。"""

    def __init__(self, message: str = "access denied") -> None:
        super().__init__(RejectReason.PERMISSION_DENIED, message)
