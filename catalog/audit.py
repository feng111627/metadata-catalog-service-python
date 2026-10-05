"""审计日志：谁在什么时间改了什么、结论是什么。只追加，可查询还原。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class AuditRecord:
    timestamp: datetime
    caller_id: str
    action: str            # 如 register_entity / update_schema / register_lineage / impact_analysis
    target: str            # 目标实体或边
    outcome: str           # "SUCCESS" 或 "REJECTED:<REASON>"
    detail: str = ""


class AuditLog:
    """线程安全的只追加审计日志。

    查询结果按 (timestamp, 追加序号) 排序返回，稳定可复现。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[AuditRecord] = []

    def record(self, caller_id: str, action: str, target: str,
               outcome: str, detail: str = "") -> AuditRecord:
        rec = AuditRecord(
            timestamp=datetime.now(timezone.utc),
            caller_id=caller_id, action=action, target=target,
            outcome=outcome, detail=detail,
        )
        with self._lock:
            self._records.append(rec)
        return rec

    def query(self, target: str | None = None,
              caller_id: str | None = None,
              action: str | None = None) -> list[AuditRecord]:
        with self._lock:
            records = list(self._records)
        return [
            r for r in records
            if (target is None or r.target == target)
            and (caller_id is None or r.caller_id == caller_id)
            and (action is None or r.action == action)
        ]
