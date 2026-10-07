"""Concurrent incremental lineage maintenance: no lost mappings, no
duplicates, deterministic end state, graph stays valid throughout."""
import threading

from app.lineage import assert_acyclic

from .conftest import make_entity

N_PER_THREAD = 10


def _setup(service, alice, count=N_PER_THREAD * 8):
    up = [{"name": f"u{i}", "type": "long"} for i in range(count)]
    down = [{"name": f"d{i}", "type": "long"} for i in range(count)]
    make_entity(service, alice, "up", fields=up)
    make_entity(service, alice, "down", fields=down)


def _edge_mappings(service, alice):
    upstream = service.get_lineage(alice, "down")["upstream"]
    assert len(upstream) == 1, "endpoint pair must stay a single edge"
    return [(m.upstream, m.downstream) for m in upstream[0].columns]


def test_concurrent_incremental_batches_lose_nothing_and_never_duplicate(service, alice):
    _setup(service, alice)
    n_threads = 8
    true_count = 0
    false_count = 0
    counter_lock = threading.Lock()

    def worker(t: int) -> None:
        nonlocal true_count, false_count
        # every thread replays the whole range; each mapping is new exactly
        # once across the whole run, the rest must be idempotent no-ops
        for i in range(n_threads * N_PER_THREAD):
            changed = service.register_lineage(
                alice, "up", "down",
                [{"upstream": f"u{i}", "downstream": f"d{i}"}],
            )
            with counter_lock:
                if changed:
                    true_count += 1
                else:
                    false_count += 1

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = n_threads * n_threads * N_PER_THREAD
    assert true_count == n_threads * N_PER_THREAD  # each mapping added once
    assert false_count == total - true_count       # every replay was a no-op
    mappings = _edge_mappings(service, alice)
    assert len(mappings) == n_threads * N_PER_THREAD
    assert len(set(mappings)) == len(mappings)     # no duplicates ever


def test_concurrent_register_and_revoke_ends_in_deterministic_state(service, alice):
    _setup(service, alice)
    stop = threading.Event()
    violations: list[str] = []
    vlock = threading.Lock()

    # checker thread: the graph must be acyclic and the pair must remain a
    # single edge at every observable instant
    def checker() -> None:
        while not stop.is_set():
            edges = service.store.edges()
            try:
                assert_acyclic(edges)
            except Exception as exc:  # pragma: no cover - invariant failure
                with vlock:
                    violations.append(f"cycle: {exc}")
            pair_edges = [e for e in edges
                          if e.upstream_id == "up" and e.downstream_id == "down"]
            if len(pair_edges) > 1:
                with vlock:
                    violations.append("duplicate endpoint-pair edge observed")
            for e in pair_edges:
                cols = [(m.upstream, m.downstream) for m in e.columns]
                if len(cols) != len(set(cols)):
                    with vlock:
                        violations.append("duplicate mapping inside edge")

    total = 4
    per = N_PER_THREAD

    def toggler(t: int) -> None:
        base = t * per
        for round_ in range(6):
            for i in range(base, base + per):
                mapping = [{"upstream": f"u{i}", "downstream": f"d{i}"}]
                service.register_lineage(alice, "up", "down", mapping)
            for i in range(base, base + per):
                mapping = [{"upstream": f"u{i}", "downstream": f"d{i}"}]
                service.revoke_lineage(alice, "up", "down", mapping)

    watcher = threading.Thread(target=checker)
    watcher.start()
    threads = [threading.Thread(target=toggler, args=(t,)) for t in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watcher.join()

    assert violations == []
    # every thread revoked exactly its own slice -> empty is the only end state
    assert service.get_lineage(alice, "down")["upstream"] == []
    assert service.impact_analysis(alice, "up", ["u0"]) == []


def test_concurrent_full_revoke_is_idempotent(service, alice):
    _setup(service, alice)
    mappings = [{"upstream": f"u{i}", "downstream": f"d{i}"}
                for i in range(N_PER_THREAD)]
    service.register_lineage(alice, "up", "down", mappings)

    results = {"removed": 0, "noop": 0}
    rlock = threading.Lock()

    def worker() -> None:
        changed = service.revoke_lineage(alice, "up", "down")
        with rlock:
            results["removed" if changed else "noop"] += 1

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {"removed": 1, "noop": 15}
    assert service.get_lineage(alice, "down")["upstream"] == []
