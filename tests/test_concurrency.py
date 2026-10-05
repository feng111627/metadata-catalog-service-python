"""Concurrent updates: deterministic conflicts, no partial writes, no lost updates."""
import threading

from app.errors import RevisionConflictError

from .conftest import make_entity

BASE = [{"name": "id", "type": "long", "required": True}]


def test_stale_revision_gets_deterministic_conflict(service, alice):
    make_entity(service, alice, "t1", fields=BASE)
    service.evolve_schema(alice, "t1", 1, BASE + [{"name": "a", "type": "string"}])
    # replaying with the old revision must fail deterministically
    try:
        service.evolve_schema(alice, "t1", 1, BASE + [{"name": "b", "type": "string"}])
        raise AssertionError("expected conflict")
    except RevisionConflictError:
        pass
    entity = service.get_entity(alice, "t1")
    assert entity.revision == 2
    assert [f.name for f in entity.current_schema.fields] == ["id", "a"]


def test_concurrent_evolutions_exactly_one_wins_per_revision(service, alice):
    make_entity(service, alice, "t1", fields=BASE)
    results = {"ok": 0, "conflict": 0}
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            service.evolve_schema(
                alice, "t1", 1, BASE + [{"name": f"f{i}", "type": "string"}]
            )
            with lock:
                results["ok"] += 1
        except RevisionConflictError:
            with lock:
                results["conflict"] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["ok"] == 1
    assert results["conflict"] == 15
    entity = service.get_entity(alice, "t1")
    assert entity.revision == 2
    assert len(entity.schemas) == 2  # no partial/interleaved writes


def test_concurrent_read_modify_write_with_retry_loses_nothing(service, alice):
    make_entity(service, alice, "t1", fields=BASE)
    n_threads, per_thread = 8, 5

    def worker(i: int) -> None:
        for j in range(per_thread):
            while True:
                entity = service.get_entity(alice, "t1")
                new_fields = [f.__dict__ for f in entity.current_schema.fields]
                new_fields.append({"name": f"f{i}_{j}", "type": "string"})
                try:
                    service.evolve_schema(alice, "t1", entity.revision, new_fields)
                    break
                except RevisionConflictError:
                    continue

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entity = service.get_entity(alice, "t1")
    expected = n_threads * per_thread
    assert entity.revision == 1 + expected
    assert len(entity.schemas) == 1 + expected
    all_names = [f.name for f in entity.current_schema.fields]
    assert len(set(all_names)) == len(all_names)  # no overwrite lost a field


def test_concurrent_lineage_registration_stays_idempotent(service, alice):
    make_entity(service, alice, "up")
    make_entity(service, alice, "down")
    added_count = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal added_count
        if service.register_lineage(alice, "up", "down"):
            with lock:
                added_count += 1

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert added_count == 1
    assert len(service.get_lineage(alice, "down")["upstream"]) == 1
