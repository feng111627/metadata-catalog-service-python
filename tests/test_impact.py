"""影响分析测试：下游实体、字段与层级，结论稳定可复现。"""
from catalog.impact import ImpactAnalyzer
from catalog.lineage import LineageGraph
from catalog.models import ColumnLineage, LineageEdge


def build_graph() -> LineageGraph:
    # raw -> stg -> mart -> report；另有 stg -> audit
    g = LineageGraph()
    g.register(LineageEdge("raw", "stg", column_lineage=(
        ColumnLineage("raw", "user_id", "uid"),
        ColumnLineage("raw", "amount", "amt"),
    )))
    g.register(LineageEdge("stg", "mart", column_lineage=(
        ColumnLineage("stg", "uid", "user_id"),
    )))
    g.register(LineageEdge("mart", "report"))  # 无列级映射
    g.register(LineageEdge("stg", "audit"))
    return g


def test_table_level_impact_levels():
    analyzer = ImpactAnalyzer(build_graph())
    items = analyzer.analyze("raw")
    by_id = {i.entity_id: i for i in items}
    assert by_id["stg"].level == 1
    assert by_id["mart"].level == 2
    assert by_id["report"].level == 3
    assert by_id["audit"].level == 2  # raw -> stg -> audit 两跳
    # 无列级映射的边 -> 下游整表受影响
    assert by_id["report"].columns == ()
    assert by_id["audit"].columns == ()


def test_column_level_impact_propagates_precisely():
    analyzer = ImpactAnalyzer(build_graph())
    items = analyzer.analyze("raw", columns=("user_id",))
    by_id = {i.entity_id: i for i in items}
    assert by_id["stg"].columns == ("uid",)          # user_id -> uid
    assert by_id["mart"].columns == ("user_id",)     # uid -> user_id
    assert by_id["report"].columns == ()             # 无列映射，整表保守受影响


def test_unrelated_column_affects_nothing_downstream_of_mapping():
    analyzer = ImpactAnalyzer(build_graph())
    items = analyzer.analyze("raw", columns=("other_col",))
    by_id = {i.entity_id: i for i in items}
    # other_col 不在 raw->stg 的列映射里，stg 不受影响，mart 也不受影响；
    # 但 raw 的整表级下游不存在，故仅无映射边... 此处无直接无映射边，全部不受影响
    assert items == [] or all(i.entity_id not in ("stg", "mart") for i in items)


def test_analysis_is_reproducible():
    analyzer = ImpactAnalyzer(build_graph())
    first = analyzer.analyze("raw")
    for _ in range(5):
        assert analyzer.analyze("raw") == first


def test_longest_path_level_in_diamond():
    g = LineageGraph()
    g.register(LineageEdge("a", "d"))           # 直接边：d 距 a 为 1
    g.register(LineageEdge("a", "b"))
    g.register(LineageEdge("b", "d"))           # 间接：d 距 a 为 2
    analyzer = ImpactAnalyzer(g)
    by_id = {i.entity_id: i for i in analyzer.analyze("a")}
    assert by_id["d"].level == 2  # 取最长路径，结论确定
