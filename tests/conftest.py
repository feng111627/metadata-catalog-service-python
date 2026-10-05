"""测试会话级日志：打印业务覆盖矩阵，便于从日志看出覆盖了哪些业务。"""

COVERAGE = {
    "test_compatibility.py": "schema 兼容性：兼容接受 / 不兼容拒绝（删字段、改类型、加必填、收紧可空）",
    "test_lineage.py": "血缘：幂等登记、列级合并、自环/间接环拒绝、查询稳定可复现",
    "test_concurrency.py": "并发更新：乐观锁冲突确定、无部分写入、重试收敛不丢更新",
    "test_impact.py": "影响分析：下游实体/字段/层级、列级精确传播、结论可复现",
    "test_service.py": "服务门面：输入校验、权限边界（不暴露存在性）、审计还原",
    "test_api.py": "HTTP API：全生命周期、权限不泄露、非法输入拒绝",
}


def pytest_sessionstart(session):
    print("\n===== 业务覆盖矩阵 =====")
    for fname, desc in COVERAGE.items():
        print(f"  {fname:<26} {desc}")
    print("========================")


def pytest_collection_modifyitems(items):
    counts: dict[str, int] = {}
    for item in items:
        fname = item.fspath.basename
        counts[fname] = counts.get(fname, 0) + 1
    print("\n===== 各业务用例数 =====")
    for fname, n in sorted(counts.items()):
        print(f"  {fname:<26} {n} 个用例")
    print("========================")
