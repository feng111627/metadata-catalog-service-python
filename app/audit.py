"""Append-only audit log."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.models import AuditRecord


class AuditLog:
    """In-memory, append-only audit trail.

    Every mutation attempt (success or failure) is recorded with actor,
    timestamp, target, outcome and a human-readable detail, so one can
    reconstruct who changed what, when, and with what result.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[AuditRecord] = []

    def record(
        self,
        actor: str,
        action: str,
        target: str,
        outcome: str,
        detail: str = "",
    ) -> AuditRecord:
        with self._lock:
            rec = AuditRecord(
                seq=len(self._records) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                actor=actor,
                action=action,
                target=target,
                outcome=outcome,
                detail=detail,
            )
            self._records.append(rec)
            return rec

    def list(self) -> list[AuditRecord]:
        with self._lock:
            return list(self._records)
