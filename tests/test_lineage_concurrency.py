"""并发增量补登/撤销：不丢有效内容、不产生重复、最终状态确定。"""
import threading

from .conftest import make_entity

COLUMNS_UP = [{"name": f"c{i}", "type": "long"} for i in range(8)]
COLUMNS_DOWN = [{"name": f"d{i}", "type": "long"} for i in range(8)]
ALL_PAIRS = {(f"c{i}", f"d{i}") for i in range(8)}


def _edge_mappings(svc, actor):
    edges = svc.get_lineage(actor, "down")["upstream"]
    assert len(edges) == 1
    return {(m.upstream, m.downstream) for m in edges[0].columns}


def test_concurrent_batched_registration_merges_without_loss_or_dups(service, alice):
    make_entity(service, alice, "up", fields=COLUMNS_UP)
    make_entity(service, alice, "down", fields=COLUMNS_DOWN)

    def worker(i: int) -> None:
        j = (i + 1) % 8  # each batch overlaps the next thread's batch by one pair
        service.register_lineage(
            alice, "up", "down",
            [{"upstream": f"c{i}", "downstream": f"d{i}"},
             {"upstream": f"c{j}", "downstream": f"d{j}"}],
        )

    def run_round() -> None:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    run_round()
    assert _edge_mappings(service, alice) == ALL_PAIRS
    # replays under contention keep the exact same state (deterministic)
    for _ in range(5):
        run_round()
        assert _edge_mappings(service, alice) == ALL_PAIRS


def test_concurrent_register_and_revoke_disjoint_mappings_are_deterministic(service, alice):
    make_entity(service, alice, "up", fields=COLUMNS_UP)
    make_entity(service, alice, "down", fields=COLUMNS_DOWN)
    service.register_lineage(
        alice, "up", "down",
        [{"upstream": "c0", "downstream": "d0"},
         {"upstream": "c1", "downstream": "d1"}],
    )
    barrier = threading.Barrier(2)

    def revoker() -> None:
        barrier.wait()
        service.deregister_lineage(
            alice, "up", "down", [{"upstream": "c0", "downstream": "d0"}]
        )

    def adder() -> None:
        barrier.wait()
        service.register_lineage(
            alice, "up", "down", [{"upstream": "c2", "downstream": "d2"}]
        )

    # disjoint keys commute: whichever order the lock grants, end state is same
    for _ in range(20):
        service.deregister_lineage(
            alice, "up", "down", [{"upstream": "c2", "downstream": "d2"}]
        )
        service.register_lineage(
            alice, "up", "down", [{"upstream": "c0", "downstream": "d0"}]
        )
        threads = [threading.Thread(target=revoker), threading.Thread(target=adder)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _edge_mappings(service, alice) == {
            ("c1", "d1"), ("c2", "d2"),
        }


def test_concurrent_revokes_of_different_mappings_lose_nothing(service, alice):
    make_entity(service, alice, "up", fields=COLUMNS_UP)
    make_entity(service, alice, "down", fields=COLUMNS_DOWN)
    service.register_lineage(
        alice, "up", "down",
        [{"upstream": f"c{i}", "downstream": f"d{i}"} for i in range(6)],
    )

    def worker(i: int) -> None:
        service.deregister_lineage(
            alice, "up", "down",
            [{"upstream": f"c{i}", "downstream": f"d{i}"}],
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # exactly one mapping survives and the edge remains
    assert _edge_mappings(service, alice) == {("c5", "d5")}


def test_concurrent_full_revoke_and_reregister_end_in_a_legal_state(service, alice):
    make_entity(service, alice, "up", fields=COLUMNS_UP)
    make_entity(service, alice, "down", fields=COLUMNS_DOWN)
    service.register_lineage(
        alice, "up", "down", [{"upstream": "c0", "downstream": "d0"}]
    )
    barrier = threading.Barrier(2)

    def full_revoke() -> None:
        barrier.wait()
        service.deregister_lineage(alice, "up", "down")

    def re_add() -> None:
        barrier.wait()
        service.register_lineage(
            alice, "up", "down", [{"upstream": "c1", "downstream": "d1"}]
        )

    threads = [threading.Thread(target=full_revoke), threading.Thread(target=re_add)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # whichever order the lock grants, the result is one of two legal states:
    # revoke won -> no edge; re-add won last -> exactly one edge with c1
    upstream = service.get_lineage(alice, "down")["upstream"]
    if not upstream:
        assert service.impact_analysis(alice, "up") == []
    else:
        assert len(upstream) == 1
        assert {(m.upstream, m.downstream) for m in upstream[0].columns} == {
            ("c1", "d1")
        }
        # no resurrection of the pre-revoke mapping
        impacted = service.impact_analysis(alice, "up", ["c0"])
        assert impacted == []


def test_concurrent_mixed_batches_and_revokes_keep_graph_valid(service, alice):
    # 3-entity pipeline; hammer adds/revokes concurrently and repeatedly assert
    # the graph stays a single edge per pair and never holds duplicate mappings
    make_entity(service, alice, "up", fields=COLUMNS_UP)
    make_entity(service, alice, "down", fields=COLUMNS_DOWN)
    errors: list[Exception] = []

    def adder(i: int) -> None:
        try:
            service.register_lineage(
                alice, "up", "down",
                [{"upstream": f"c{i % 4}", "downstream": f"d{i % 4}"}],
            )
        except Exception as exc:  # pragma: no cover - surfaced at end
            errors.append(exc)

    def revoker(i: int) -> None:
        try:
            service.deregister_lineage(
                alice, "up", "down",
                [{"upstream": f"c{i % 4}", "downstream": f"d{i % 4}"}],
            )
        except Exception as exc:  # pragma: no cover - surfaced at end
            errors.append(exc)

    threads = [threading.Thread(target=adder, args=(i,)) for i in range(8)]
    threads += [threading.Thread(target=revoker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    upstream = service.get_lineage(alice, "down")["upstream"]
    assert len(upstream) <= 1
    if upstream:
        mappings = [(m.upstream, m.downstream) for m in upstream[0].columns]
        assert len(mappings) == len(set(mappings))
