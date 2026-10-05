"""血缘图：登记（幂等）、循环检测、稳定可复现的上下游查询。"""
from __future__ import annotations

import threading

from .errors import LineageError, RejectReason
from .models import LineageEdge


class LineageGraph:
    """有向血缘图，边为 upstream -> downstream。

    语义：
    - 登记幂等：同一条 (upstream, downstream) 边重复登记不产生重复边；
      列级映射按并集合并，结果与登记次数无关；
    - 循环拒绝：若新边会在图中形成环（含自环），整体拒绝并报告
      CYCLE_DETECTED，不做静默去重或截断；
    - 查询稳定：所有查询结果按 (upstream, downstream) 排序返回，
      同一份血缘多次查询结论一致。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._edges: dict[tuple[str, str], LineageEdge] = {}

    def register(self, edge: LineageEdge) -> LineageEdge:
        with self._lock:
            if edge.upstream == edge.downstream:
                raise LineageError(
                    RejectReason.CYCLE_DETECTED,
                    f"self-loop on '{edge.upstream}' is not allowed",
                )
            key = (edge.upstream, edge.downstream)
            existing = self._edges.get(key)
            merged_columns = existing.column_lineage if existing else ()
            if edge.column_lineage:
                merged = set(merged_columns) | set(edge.column_lineage)
                merged_columns = tuple(sorted(
                    merged, key=lambda c: (c.upstream_entity, c.upstream_column,
                                           c.downstream_column),
                ))
            candidate = LineageEdge(
                upstream=edge.upstream,
                downstream=edge.downstream,
                column_lineage=merged_columns,
                created_by=(existing.created_by if existing else edge.created_by),
                created_at=(existing.created_at if existing else edge.created_at),
            )
            if existing is None and self._creates_cycle(edge.upstream, edge.downstream):
                raise LineageError(
                    RejectReason.CYCLE_DETECTED,
                    f"edge {edge.upstream} -> {edge.downstream} would create a cycle",
                )
            self._edges[key] = candidate
            return candidate

    def _creates_cycle(self, upstream: str, downstream: str) -> bool:
        """从 downstream 沿出边可达 upstream，则新边 upstream->downstream 成环。"""
        stack = [downstream]
        visited: set[str] = set()
        while stack:
            node = stack.pop()
            if node == upstream:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(d for (u, d) in self._edges if u == node)
        return False

    def upstreams(self, entity_id: str) -> list[LineageEdge]:
        """直接上游边，按 (upstream, downstream) 排序，结果稳定。"""
        with self._lock:
            return [self._edges[k] for k in sorted(self._edges) if k[1] == entity_id]

    def downstreams(self, entity_id: str) -> list[LineageEdge]:
        """直接下游边，按 (upstream, downstream) 排序，结果稳定。"""
        with self._lock:
            return [self._edges[k] for k in sorted(self._edges) if k[0] == entity_id]

    def all_edges(self) -> list[LineageEdge]:
        with self._lock:
            return [self._edges[k] for k in sorted(self._edges)]
