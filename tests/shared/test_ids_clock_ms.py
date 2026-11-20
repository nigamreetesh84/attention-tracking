"""UUIDv7 tests for the millisecond-clock API used by the engine (from Phase 1)."""

import threading
from uuid import UUID

import pytest
from ag_shared.ids import Uuid7Generator, uuid7, uuid7_ms


def test_version_variant_and_timestamp() -> None:
    gen = Uuid7Generator(clock_ms=lambda: 1_700_000_000_123)
    value = gen()
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"
    assert uuid7_ms(value) == 1_700_000_000_123


def test_ids_are_strictly_increasing_within_one_millisecond() -> None:
    gen = Uuid7Generator(clock_ms=lambda: 5)
    ids = [gen() for _ in range(5000)]  # more than the 12-bit counter holds
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_clock_going_backwards_keeps_order() -> None:
    times = iter([100, 50, 50, 101])
    gen = Uuid7Generator(clock_ms=lambda: next(times))
    ids = [gen() for _ in range(4)]
    assert ids == sorted(ids)


def test_process_generator_is_thread_safe_and_unique() -> None:
    out: list[UUID] = []
    lock = threading.Lock()

    def work() -> None:
        local = [uuid7() for _ in range(500)]
        with lock:
            out.extend(local)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 4000


def test_uuid7_ms_rejects_other_versions() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        uuid7_ms(UUID("12345678-1234-4234-8234-123456789abc"))


def test_concurrent_ids_from_one_generator_stay_ordered_per_millisecond() -> None:
    gen = Uuid7Generator(clock_ms=lambda: 42)
    out: list[UUID] = []
    lock = threading.Lock()

    def work() -> None:
        for _ in range(300):
            value = gen()
            with lock:
                out.append(value)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    counters = [(v.int >> 64) & 0xFFFF for v in out]
    assert len(set(counters)) == len(counters)  # no two ids share a counter value
