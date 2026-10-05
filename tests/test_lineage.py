"""血缘测试：幂等登记、循环拒绝、查询稳定可复现。"""
import pytest

from catalog.errors import LineageError, RejectReason
from catalog.lineage import LineageGraph
from catalog.models import ColumnLineage, LineageEdge


def edge(up: str, down: str, cols: tuple[ColumnLineage, ...] = ()) -> LineageEdge:
    return LineageEdge(upstream=up, downstream=down, column_lineage=cols)


def test_register_is_idempotent():
    g = LineageGraph()
    g.register(edge("a", "b"))
    g.register(edge("a", "b"))
    g.register(edge("a", "b"))
    downs = g.downstreams("a")
    assert len(downs) == 1  # 重复登记不产生重复边


def test_column_lineage_merges_deterministically():
    g = LineageGraph()
    c1 = ColumnLineage("a", "x", "x")
    c2 = ColumnLineage("a", "y", "y")
    g.register(edge("a", "b", (c1,)))
    g.register(edge("a", "b", (c2,)))
    g.register(edge("a", "b", (c1, c2)))  # 再登一次不改变结论
    downs = g.downstreams("a")
    assert len(downs) == 1
    assert set(downs[0].column_lineage) == {c1, c2}


def test_self_loop_rejected():
    g = LineageGraph()
    with pytest.raises(LineageError) as exc:
        g.register(edge("a", "a"))
    assert exc.value.reason is RejectReason.CYCLE_DETECTED


def test_indirect_cycle_rejected():
    g = LineageGraph()
    g.register(edge("a", "b"))
    g.register(edge("b", "c"))
    g.register(edge("c", "d"))
    with pytest.raises(LineageError) as exc:
        g.register(edge("d", "a"))  # d->a 会成环 a->b->c->d->a
    assert exc.value.reason is RejectReason.CYCLE_DETECTED
    # 被拒绝的边没有写入：图保持原状
    assert g.upstreams("a") == []


def test_diamond_is_not_a_cycle():
    g = LineageGraph()
    g.register(edge("a", "b"))
    g.register(edge("a", "c"))
    g.register(edge("b", "d"))
    g.register(edge("c", "d"))  # 菱形合法
    assert len(g.upstreams("d")) == 2


def test_queries_are_stable_and_reproducible():
    g = LineageGraph()
    for up, down in [("c", "z"), ("a", "z"), ("b", "z"), ("a", "y")]:
        g.register(edge(up, down))
    first = g.upstreams("z")
    for _ in range(5):
        again = g.upstreams("z")
        assert again == first  # 多次查询结论一致
    assert [e.upstream for e in first] == ["a", "b", "c"]  # 排序确定
