"""并发更新测试：冲突结果确定、无部分写入、重试后收敛。"""
import threading

import pytest

from catalog.errors import ConcurrencyError, RejectReason
from catalog.models import Entity
from catalog.store import EntityStore


def make_store() -> tuple[EntityStore, Entity]:
    store = EntityStore()
    entity = Entity(entity_id="e1", namespace="ns", name="t1", owner="alice")
    entity.__dict__["counter"] = 0  # 测试用动态属性
    return store, store.create(entity)


def test_concurrent_updates_exactly_one_wins():
    store, entity = make_store()
    results: list[str] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i: int):
        barrier.wait()  # 同时起跑，最大化竞争
        try:
            store.update("e1", expected_lock_version=1,
                         mutate=lambda e: e.__dict__.__setitem__("counter", e.counter + 1))
            outcome = "ok"
        except ConcurrencyError as exc:
            assert exc.reason is RejectReason.VERSION_CONFLICT
            outcome = "conflict"
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 确定的结果：恰好一个成功，其余全部冲突
    assert results.count("ok") == 1
    assert results.count("conflict") == 7
    # 无部分写入：counter 只被加了一次
    assert store.get("e1").counter == 1
    assert store.get("e1").lock_version == 2


def test_retry_loop_converges_without_lost_updates():
    store, entity = make_store()

    def worker():
        for _ in range(25):
            while True:
                current = store.get("e1")
                try:
                    store.update(
                        "e1",
                        expected_lock_version=current.lock_version,
                        mutate=lambda e: e.__dict__.__setitem__("counter", e.counter + 1),
                    )
                    break
                except ConcurrencyError:
                    continue  # 冲突后重读重试

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 线程 * 25 次 = 100 次递增，一次不丢
    assert store.get("e1").counter == 100
    assert store.get("e1").lock_version == 101


def test_failed_mutation_leaves_state_unchanged():
    store, entity = make_store()

    def bad_mutate(e: Entity):
        e.__dict__["counter"] = 999
        raise ValueError("boom")

    with pytest.raises(ValueError):
        store.update("e1", expected_lock_version=1, mutate=bad_mutate)

    assert store.get("e1").counter == 0  # 部分写入被回滚（版本号也未推进）
    assert store.get("e1").lock_version == 1
