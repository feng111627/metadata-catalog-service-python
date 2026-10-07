"""Thread-safe in-memory catalog store with optimistic concurrency."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from app.errors import NotFoundError, RevisionConflictError, ValidationError
from app.lineage import assert_acyclic
from app.models import ColumnMapping, DataEntity, LineageEdge


@dataclass(frozen=True)
class LineageMutation:
    """Result of one atomic incremental lineage registration/revocation.

    `changed` tells the caller whether any state actually moved; `new_edge`
    is False when the edge itself was created by the operation and True when
    it already existed (or no longer exists after a full revoke).
    """

    changed: bool
    new_edge: bool
    edge: LineageEdge | None


class CatalogStore:
    """Holds entities and lineage edges.

    All mutating operations take the internal lock and are atomic:
    a failed operation leaves no partial writes. Entity updates use
    revision-based optimistic concurrency.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, DataEntity] = {}
        # edge identity is the endpoint pair; column mappings are a mutable set
        self._edges: dict[tuple[str, str], set[ColumnMapping]] = {}

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
    def merge_edge_mappings(
        self,
        upstream_id: str,
        downstream_id: str,
        mappings: tuple[ColumnMapping, ...],
        guard: Callable[[list[LineageEdge]], None],
    ) -> LineageMutation:
        """Incrementally merge column mappings into one edge, atomically.

        The edge between an endpoint pair always exists at most once: a
        missing edge is created; an existing one gains only the genuinely
        new mappings. The guard (cycle check) runs inside the lock against
        the current graph; if it raises, or the resulting graph is invalid,
        nothing is committed. When nothing is new the guard is not re-run
        (idempotent replay).
        """
        with self._lock:
            key = (upstream_id, downstream_id)
            existing = self._edges.get(key)
            new_edge = existing is None
            current = set(existing) if existing is not None else set()
            additions = set(mappings) - current
            if not additions and not new_edge:
                return LineageMutation(
                    changed=False, new_edge=False, edge=self._snapshot(key, current)
                )
            guard(self.edges())
            merged = current | additions
            assert_acyclic(self._snapshot_with(key, merged))
            self._edges[key] = merged
            return LineageMutation(
                changed=True, new_edge=new_edge, edge=self._snapshot(key, merged)
            )

    def remove_edge_mappings(
        self,
        upstream_id: str,
        downstream_id: str,
        mappings: frozenset[ColumnMapping] | None,
    ) -> LineageMutation:
        """Atomically revoke a whole edge (mappings=None) or some mappings.

        Idempotent: missing edges and already-absent mappings are a no-op
        that still reports success. When the last mapping is removed the
        edge itself disappears. The remaining graph is validated as a
        post-condition, so a revoke can never leave an invalid graph.
        """
        with self._lock:
            key = (upstream_id, downstream_id)
            existing = self._edges.get(key)
            if existing is None:
                return LineageMutation(changed=False, new_edge=False, edge=None)
            if mappings is None:
                # whole-edge revoke: a table-level edge (no column mappings)
                # is still a real edge and must be removed
                assert_acyclic(self._snapshot_with(key, set()))
                del self._edges[key]
                return LineageMutation(changed=True, new_edge=False, edge=None)
            current = set(existing)
            remaining = current - set(mappings)
            removed = current - remaining
            if not removed:
                return LineageMutation(
                    changed=False, new_edge=False, edge=self._snapshot(key, current)
                )
            assert_acyclic(self._snapshot_with(key, remaining))
            if remaining:
                self._edges[key] = remaining
                edge = self._snapshot(key, remaining)
            else:
                del self._edges[key]
                edge = None
            return LineageMutation(changed=True, new_edge=False, edge=edge)

    def edges(self) -> list[LineageEdge]:
        """All edges, in a deterministic (sorted) order."""
        with self._lock:
            return [
                self._snapshot(key, self._edges[key]) for key in sorted(self._edges)
            ]

    def _snapshot(
        self, key: tuple[str, str], columns: set[ColumnMapping]
    ) -> LineageEdge:
        return LineageEdge(
            key[0],
            key[1],
            tuple(sorted(columns, key=lambda m: (m.upstream, m.downstream))),
        )

    def _snapshot_with(
        self, key: tuple[str, str], columns: set[ColumnMapping]
    ) -> list[LineageEdge]:
        """A graph snapshot where `key` carries hypothetical `columns`."""
        result: list[LineageEdge] = []
        for ek in sorted(self._edges):
            if ek == key:
                cols = columns
            else:
                cols = self._edges[ek]
            if cols:
                result.append(self._snapshot(ek, cols))
        if key not in self._edges and columns:
            result.append(self._snapshot(key, columns))
        return result
