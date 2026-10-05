"""Thread-safe in-memory catalog store with optimistic concurrency."""
from __future__ import annotations

import threading
from collections.abc import Callable

from app.errors import NotFoundError, RevisionConflictError, ValidationError
from app.models import DataEntity, LineageEdge


class CatalogStore:
    """Holds entities and lineage edges.

    All mutating operations take the internal lock and are atomic:
    a failed operation leaves no partial writes. Entity updates use
    revision-based optimistic concurrency.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, DataEntity] = {}
        self._edges: set[LineageEdge] = set()

    # --- entities -----------------------------------------------------
    def create_entity(self, entity: DataEntity) -> DataEntity:
        with self._lock:
            if entity.entity_id in self._entities:
                raise ValidationError(f"entity {entity.entity_id!r} already exists")
            self._entities[entity.entity_id] = entity
            return entity

    def get_entity(self, entity_id: str) -> DataEntity | None:
        with self._lock:
            return self._entities.get(entity_id)

    def list_entities(self, namespace: str | None = None) -> list[DataEntity]:
        with self._lock:
            items = [
                e
                for e in self._entities.values()
                if namespace is None or e.namespace == namespace
            ]
        return sorted(items, key=lambda e: e.entity_id)

    def update_entity(
        self,
        entity_id: str,
        expected_revision: int,
        mutate: Callable[[DataEntity], None],
    ) -> DataEntity:
        """Apply `mutate(entity)` atomically if the revision matches.

        The mutation runs while holding the lock; if it raises, nothing is
        committed and the revision is not bumped, so there are no partial
        writes. On success the revision is incremented exactly once.
        """
        with self._lock:
            entity = self._entities.get(entity_id)
            if entity is None:
                raise NotFoundError(f"entity {entity_id!r}")
            if entity.revision != expected_revision:
                raise RevisionConflictError(entity_id)
            mutate(entity)
            entity.revision += 1
            return entity

    # --- lineage ------------------------------------------------------
    def add_edge(self, edge: LineageEdge) -> bool:
        """Idempotently add an edge. Returns True if it was new."""
        with self._lock:
            if edge in self._edges:
                return False
            self._edges.add(edge)
            return True

    def add_edge_guarded(
        self, edge: LineageEdge, guard: Callable[[list[LineageEdge]], None]
    ) -> bool:
        """Run `guard(current_edges)` and add the edge, atomically.

        If the guard raises, nothing is written. Idempotent: an already
        existing edge returns False without re-running the guard.
        """
        with self._lock:
            if edge in self._edges:
                return False
            guard(self.edges())
            self._edges.add(edge)
            return True

    def edges(self) -> list[LineageEdge]:
        """All edges, in a deterministic (sorted) order."""
        with self._lock:
            return sorted(self._edges, key=lambda e: (e.upstream_id, e.downstream_id))
