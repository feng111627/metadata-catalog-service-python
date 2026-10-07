"""Lineage graph operations: cycle detection, queries, impact analysis."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from app.errors import CycleDetectedError
from app.models import LineageEdge


@dataclass
class ImpactedEntity:
    entity_id: str
    depth: int
    columns: list[str] = field(default_factory=list)


def _has_path(adj: dict[str, list[str]], start: str, target: str) -> list[str] | None:
    """Return one path start->...->target following adj, or None."""
    if start == target:
        return [start]
    visited = {start}
    queue: deque[list[str]] = deque([[start]])
    while queue:
        path = queue.popleft()
        for nxt in adj.get(path[-1], []):
            if nxt in visited:
                continue
            if nxt == target:
                return path + [nxt]
            visited.add(nxt)
            queue.append(path + [nxt])
    return None


def assert_no_cycle(edges: list[LineageEdge], new_edge: LineageEdge) -> None:
    """Raise CycleDetectedError if adding `new_edge` closes a cycle.

    Adding upstream->downstream creates a cycle iff downstream can already
    reach upstream in the existing graph (or upstream == downstream).
    """
    if new_edge.upstream_id == new_edge.downstream_id:
        raise CycleDetectedError([new_edge.upstream_id, new_edge.downstream_id])
    adj: dict[str, list[str]] = {}
    for e in edges:
        adj.setdefault(e.upstream_id, []).append(e.downstream_id)
    path = _has_path(adj, new_edge.downstream_id, new_edge.upstream_id)
    if path is not None:
        raise CycleDetectedError(path + [new_edge.downstream_id])


def assert_acyclic(edges: list[LineageEdge]) -> None:
    """Validate the whole graph: no self loops and no directed cycles.

    Used as a post-condition of every lineage mutation, so a revoke can never
    leave the graph in an invalid state. Raises CycleDetectedError with the
    offending path.
    """
    adj: dict[str, list[str]] = {}
    for e in edges:
        if e.upstream_id == e.downstream_id:
            raise CycleDetectedError([e.upstream_id, e.downstream_id])
        adj.setdefault(e.upstream_id, []).append(e.downstream_id)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(adj.get(node, [])):
            state = color.get(nxt, WHITE)
            if state == GRAY:
                start = stack.index(nxt)
                raise CycleDetectedError(stack[start:] + [nxt])
            if state == WHITE:
                visit(nxt)
        stack.pop()
        color[node] = BLACK

    for node in sorted(adj):
        if color.get(node, WHITE) == WHITE:
            visit(node)


def downstream_impact(
    edges: list[LineageEdge], source_id: str, source_columns: list[str] | None = None
) -> list[ImpactedEntity]:
    """BFS over downstream edges from `source_id`.

    Returns impacted entities with their depth (1 = direct downstream) and
    the impacted downstream columns. Deterministic ordering: by depth, then id.

    Column propagation: if `source_columns` is given, only column mappings
    whose upstream column is in that set propagate, and only the mapped
    downstream columns are reported; entities reached through such edges with
    no matching mapping are not impacted. If `source_columns` is None, the
    whole entity is impacted and all columns of downstream entities are
    considered impacted.
    """
    adj: dict[str, list[LineageEdge]] = {}
    for e in edges:
        adj.setdefault(e.upstream_id, []).append(e)
    for lst in adj.values():
        lst.sort(key=lambda e: e.downstream_id)

    impacted: dict[str, ImpactedEntity] = {}
    # work items: (entity_id, depth, active_columns or None for "all")
    queue: deque[tuple[str, int, frozenset[str] | None]] = deque(
        [(source_id, 0, frozenset(source_columns) if source_columns else None)]
    )
    best_depth: dict[str, int] = {source_id: 0}

    while queue:
        current, depth, active = queue.popleft()
        for edge in adj.get(current, []):
            nxt = edge.downstream_id
            if active is None:
                next_active: frozenset[str] | None = None
                new_cols = sorted({m.downstream for m in edge.columns})
            else:
                new_cols = sorted(
                    m.downstream for m in edge.columns if m.upstream in active
                )
                if not new_cols:
                    continue
                next_active = frozenset(new_cols)

            nd = depth + 1
            existing = impacted.get(nxt)
            if existing is None:
                impacted[nxt] = ImpactedEntity(nxt, nd, list(new_cols))
                best_depth[nxt] = nd
                queue.append((nxt, nd, next_active))
            else:
                changed = False
                if nd < existing.depth:
                    existing.depth = nd
                    changed = True
                merged = sorted(set(existing.columns) | set(new_cols))
                if merged != existing.columns:
                    existing.columns = merged
                    changed = True
                if changed and best_depth.get(nxt, nd) >= nd:
                    best_depth[nxt] = nd
                    queue.append((nxt, nd, next_active))

    return sorted(impacted.values(), key=lambda i: (i.depth, i.entity_id))
