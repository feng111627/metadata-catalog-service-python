"""线程安全的实体存储：乐观锁 + 写时复制，保证并发更新结果确定、无部分写入。"""
from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from .errors import ConcurrencyError, RejectReason, ValidationError
from .models import Entity


class EntityStore:
    """实体的内存存储。

    并发语义：
    - 所有读写在同一把 RLock 下完成，单个操作原子；
    - get 返回深拷贝，调用方无法在锁外篡改存储内状态；
    - update 采用乐观锁 + 写时复制：在深拷贝上执行 mutate，
      全部成功才替换存储中的实体并推进 lock_version；
      mutate 抛异常则整体放弃，存储内状态不变（无部分写入）；
    - 冲突结果确定：先获得锁的一方成功，另一方收到 VERSION_CONFLICT。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, Entity] = {}

    def get(self, entity_id: str) -> Entity | None:
        with self._lock:
            entity = self._entities.get(entity_id)
            return copy.deepcopy(entity) if entity is not None else None

    def create(self, entity: Entity) -> Entity:
        with self._lock:
            if entity.entity_id in self._entities:
                raise ValidationError(
                    RejectReason.INVALID_STRUCTURE,
                    f"entity '{entity.entity_id}' already exists",
                )
            now = datetime.now(timezone.utc)
            stored = copy.deepcopy(entity)
            stored.lock_version = 1
            stored.created_at = stored.created_at or now
            stored.updated_at = now
            self._entities[entity.entity_id] = stored
            return copy.deepcopy(stored)

    def update(
        self,
        entity_id: str,
        expected_lock_version: int,
        mutate: Callable[[Entity], None],
    ) -> Entity:
        """原子地校验版本并应用 mutate；版本不符或 mutate 失败则整体拒绝。"""
        with self._lock:
            current = self._entities.get(entity_id)
            if current is None:
                raise ValidationError(
                    RejectReason.ENTITY_NOT_FOUND, f"entity '{entity_id}' not found"
                )
            if current.lock_version != expected_lock_version:
                raise ConcurrencyError(
                    RejectReason.VERSION_CONFLICT,
                    f"entity '{entity_id}' lock_version mismatch: "
                    f"expected {expected_lock_version}, actual {current.lock_version}",
                )
            candidate = copy.deepcopy(current)
            mutate(candidate)  # 抛异常则放弃，存储内状态不变
            candidate.lock_version += 1
            candidate.updated_at = datetime.now(timezone.utc)
            self._entities[entity_id] = candidate
            return copy.deepcopy(candidate)
