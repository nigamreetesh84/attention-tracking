"""UUIDv7 generator: layout, ordering, injectable clock."""

import threading
from datetime import UTC, datetime, timedelta, timezone
from uuid import RFC_4122, UUID, uuid4

import pytest
from ag_shared.ids import Uuid7Generator, is_uuid7, new_uuid7, uuid7_time

T0 = datetime(2026, 11, 20, 12, 0, 0, 123456, tzinfo=UTC)


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _zero_random(n: int) -> bytes:
    return bytes(n)


def test_layout_version_variant_and_time() -> None:
    gen = Uuid7Generator(now=_Clock(T0))
    u = gen()
    assert u.version == 7
    assert u.variant == RFC_4122
    assert is_uuid7(u)
    assert uuid7_time(u) == T0.replace(microsecond=123000)


def test_timestamp_is_unix_milliseconds_in_top_48_bits() -> None:
    u = Uuid7Generator(now=_Clock(T0), randbytes=_zero_random)()
    assert u.int >> 80 == int(T0.timestamp() * 1000)


def test_ids_in_the_same_millisecond_are_strictly_increasing() -> None:
    gen = Uuid7Generator(now=_Clock(T0))
    ids = [gen() for _ in range(5000)]  # more than the 12-bit counter range
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_ordering_follows_time_and_survives_a_clock_step_back() -> None:
    clock = _Clock(T0)
    gen = Uuid7Generator(now=clock)
    a = gen()
    clock.t = T0 + timedelta(milliseconds=5)
    b = gen()
    clock.t = T0 - timedelta(seconds=10)  # wall clock corrected backwards
    c = gen()
    assert a < b < c
    assert uuid7_time(c) >= uuid7_time(b)


def test_naive_clock_is_refused() -> None:
    gen = Uuid7Generator(now=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        gen()


def test_non_utc_aware_clock_is_converted() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    gen = Uuid7Generator(now=_Clock(T0.astimezone(ist)))
    assert uuid7_time(gen()) == T0.replace(microsecond=123000)


def test_is_uuid7_rejects_other_versions() -> None:
    assert not is_uuid7(uuid4())
    assert not is_uuid7(UUID(int=0))
    with pytest.raises(ValueError, match="UUIDv7"):
        uuid7_time(uuid4())


def test_default_generator_and_threads() -> None:
    gen = Uuid7Generator()
    out: list[UUID] = []
    lock = threading.Lock()

    def work() -> None:
        local = [gen() for _ in range(200)]
        with lock:
            out.extend(local)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 800
    assert is_uuid7(new_uuid7())
