"""Thread-safe in-memory catalog store with optimistic concurrency."""
from __future__ import annotations

import threading
from collections.abc import Callable

from app.errors import NotFoundError, RevisionConflictError, ValidationError
from app.models import ColumnMapping, DataEntity, LineageEdge


class CatalogStore:
    """Holds entities and lineage edges.

    All mutating operations take the internal lock and are atomic:
    a failed operation leaves no partial writes. Entity updates use
    revision-based optimistic concurrency.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, DataEntity] = {}
        # one record per (upstream, downstream) table pair; mappings are a set
        # so column lineage can be registered in batches and merged/deduplicated
        self._mappings: dict[tuple[str, str], set[ColumnMapping]] = {}

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
    def merge_lineage(
        self,
        upstream_id: str,
        downstream_id: str,
        mappings: frozenset[ColumnMapping],
        guard: Callable[[list[LineageEdge]], None] | None = None,
    ) -> tuple[bool, frozenset[ColumnMapping]]:
        """Atomically merge column mappings for one table pair.

        The pair is always represented by a single edge: mappings of
        previously registered batches and the new batch are unioned and
        deduplicated. Only a genuinely new table pair runs the optional
        `guard(current_edges)` (e.g. cycle detection) -- adding more column
        mappings to an existing pair cannot create a cycle.

        Returns ``(added, new_mappings)`` where ``added`` is True iff the
        pair is new or the batch contained previously unknown mappings, and
        ``new_mappings`` is the full mapping set after the merge. All work
        happens under the lock, so there are no partial writes.
        """
        key = (upstream_id, downstream_id)
        with self._lock:
            current = self._mappings.get(key)
            if current is None and guard is not None:
                guard(self.edges())
            if current is None:
                self._mappings[key] = set(mappings)
                current = self._mappings[key]
                added = True  # the pair/edge itself is new
            else:
                before = len(current)
                current.update(mappings)
                added = len(current) != before
            return added, frozenset(current)

    def remove_lineage(
        self,
        upstream_id: str,
        downstream_id: str,
        mappings: frozenset[ColumnMapping] | None = None,
    ) -> tuple[bool, bool]:
        """Atomically remove a whole edge or specific column mappings.

        ``mappings=None`` removes the whole edge. Otherwise only the given
        mappings are removed; a column-level edge whose last mapping is gone
        disappears as well, so it cannot linger with empty mappings. A
        table-level edge (no mappings) survives partial removals.

        Idempotent: removing an already-absent edge or mappings changes
        nothing and returns ``(False, edge_still_exists)``.
        """
        key = (upstream_id, downstream_id)
        with self._lock:
            current = self._mappings.get(key)
            if current is None:
                return False, False
            if mappings is None:
                del self._mappings[key]
                return True, False
            if not current:
                # table-level edge: dropping individual mappings is a no-op
                return False, True
            before = len(current)
            current.difference_update(mappings)
            removed = len(current) != before
            if not current:
                del self._mappings[key]
                return removed, False
            return removed, True

    def edges(self) -> list[LineageEdge]:
        """All edges, in a deterministic (sorted) order.

        Column mappings inside an edge are sorted too.
        """
        with self._lock:
            result = [
                LineageEdge(
                    up,
                    down,
                    tuple(sorted(cols, key=lambda m: (m.upstream, m.downstream))),
                )
                for (up, down), cols in self._mappings.items()
            ]
        return sorted(result, key=lambda e: (e.upstream_id, e.downstream_id))
