"""基于血缘的变更影响分析：受影响下游实体、字段与影响层级。"""
from __future__ import annotations

from .lineage import LineageGraph
from .models import ImpactItem

_WHOLE_TABLE = None  # affected 映射中 None 表示整表受影响


class ImpactAnalyzer:
    """影响分析器。

    语义：
    - 从变更实体出发沿血缘向下游传播；
    - 影响层级 = 从源实体到该实体的最长路径长度（DAG 上确定）；
    - 指定了变更列时，沿列级血缘精确传播；边缺少列级映射时
      保守地将下游整表视为受影响（columns 为空元组表示整表）；
    - 结果按 (level, entity_id) 排序，同一血缘多次分析结论一致。
    """

    def __init__(self, graph: LineageGraph) -> None:
        self._graph = graph

    def analyze(self, entity_id: str, columns: tuple[str, ...] = ()) -> list[ImpactItem]:
        edges = self._graph.all_edges()  # 一致性快照

        # 第一遍：最长路径层级（DAG 上不动点迭代，必然终止）
        level: dict[str, int] = {entity_id: 0}
        changed = True
        while changed:
            changed = False
            for e in edges:
                if e.upstream in level:
                    cand = level[e.upstream] + 1
                    if cand > level.get(e.downstream, -1):
                        level[e.downstream] = cand
                        changed = True

        # 第二遍：列级影响传播（不动点迭代）
        affected: dict[str, set[str] | None] = {
            entity_id: (set(columns) if columns else _WHOLE_TABLE)
        }
        changed = True
        while changed:
            changed = False
            for e in edges:
                if e.upstream not in affected:
                    continue
                src = affected[e.upstream]
                if src is _WHOLE_TABLE or not e.column_lineage:
                    prop: set[str] | None = _WHOLE_TABLE
                else:
                    prop = {cl.downstream_column for cl in e.column_lineage
                            if cl.upstream_column in src}
                    if not prop:
                        continue
                if e.downstream in affected and affected[e.downstream] is _WHOLE_TABLE:
                    continue  # 已整表受影响，无需再传播
                if prop is _WHOLE_TABLE:
                    affected[e.downstream] = _WHOLE_TABLE
                    changed = True
                else:
                    cur = affected.get(e.downstream)
                    merged = (cur if cur else set()) | prop
                    if e.downstream not in affected or merged != cur:
                        affected[e.downstream] = merged
                        changed = True

        items = []
        for ent, lv in level.items():
            if ent == entity_id:
                continue
            if columns and ent not in affected:
                continue  # 列级分析下未被列血缘波及的实体不算受影响
            cols = affected.get(ent, _WHOLE_TABLE)
            items.append(ImpactItem(
                entity_id=ent, level=lv,
                columns=tuple(sorted(cols)) if cols is not _WHOLE_TABLE else (),
            ))
        return sorted(items, key=lambda i: (i.level, i.entity_id))
