"""Server-corrected UTC shared across the workstation's processes (contract 6, "Clock"; 2.11 fix).

The service parent publishes the sync client's smoothed offset to a plain
file; the UI and the engine child stamp records with it. Every way the file
can be wrong (missing, corrupt, out of bounds, stale, from before a clock
step or another boot) falls back to the raw clock.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from ag_workstation.clock import FakeClock
from ag_workstation.corrected_time import (
    HOLD_ON_READ_ERROR_S,
    MAX_APPLIED_OFFSET_MS,
    MAX_OFFSET_AGE_S,
    OFFSET_FILE,
    PUBLISH_REFRESH_S,
    READ_INTERVAL_S,
    STEP_TOLERANCE_S,
    OffsetPublisher,
    SharedClockOffset,
    StampingClock,
    offset_path,
    stamping_clock,
)
from ag_workstation.fakes.device_server import FakeDeviceServer, Fault
from ag_workstation.queue import EventQueue, InMemoryKeyProvider
from ag_workstation.sync import DeviceCredentials, HttpxTransport, InMemoryCredentialStore
from ag_workstation.sync.clock_offset import ClockOffsetEstimator
from ag_workstation.sync.wiring import SyncService, build_sync_service
from pydantic import SecretStr

T0 = datetime(2026, 11, 21, 4, 30, tzinfo=UTC)
STATION = UUID("0193a0b0-0000-7000-8000-00000000e001")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_monotonic=1000.0, start_utc=T0)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return offset_path(tmp_path)


def _write(path: Path, body: object) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def _body(clock: FakeClock, offset_ms: int) -> dict[str, object]:
    return {
        "v": 1,
        "offset_ms": offset_ms,
        "wall": clock.now().isoformat(),
        "mono": clock.monotonic(),
    }


def test_path_is_in_the_data_directory(tmp_path: Path) -> None:
    assert offset_path(tmp_path) == tmp_path / OFFSET_FILE


def test_published_offset_corrects_now_and_stamps(path: Path, clock: FakeClock) -> None:
    assert OffsetPublisher(path, clock).publish(5000)
    utc = StampingClock(clock, SharedClockOffset(path, clock))
    assert utc.now() == clock.now() + timedelta(seconds=5)
    assert utc.monotonic() == clock.monotonic()  # intervals are never shifted
    device = T0 - timedelta(minutes=3)
    assert utc.stamp(device) == device + timedelta(seconds=5)
    assert utc.raw is clock
    clock.advance(30)
    assert utc.now() == clock.now() + timedelta(seconds=5)


def test_no_file_or_no_data_dir_is_the_raw_clock(path: Path, clock: FakeClock) -> None:
    assert SharedClockOffset(path, clock).offset_ms() == 0
    assert SharedClockOffset(None, clock).offset_ms() == 0
    assert stamping_clock(clock, None).now() == clock.now()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"v": 2, "offset_ms": 5000, "wall": "2026-11-21T04:30:00+00:00", "mono": 1000}',
        '{"v": 1, "offset_ms": "5000", "wall": "2026-11-21T04:30:00+00:00", "mono": 1000}',
        '{"v": 1, "offset_ms": 5000.5, "wall": "2026-11-21T04:30:00+00:00", "mono": 1000}',
        '{"v": 1, "offset_ms": true, "wall": "2026-11-21T04:30:00+00:00", "mono": 1000}',
        '{"v": 1, "offset_ms": 5000, "wall": "yesterday", "mono": 1000}',
        '{"v": 1, "offset_ms": 5000, "wall": "2026-11-21T04:30:00", "mono": 1000}',
        '{"v": 1, "offset_ms": 5000, "wall": "2026-11-21T04:30:00+00:00", "mono": NaN}',
        '{"v": 1, "offset_ms": 5000, "wall": "2026-11-21T04:30:00+00:00"}',
        "x" * 10_000,
    ],
)
def test_a_corrupt_file_is_the_raw_clock(path: Path, clock: FakeClock, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    assert SharedClockOffset(path, clock).offset_ms() == 0


def test_the_offset_applied_is_bounded(path: Path, clock: FakeClock) -> None:
    _write(path, _body(clock, MAX_APPLIED_OFFSET_MS))
    assert SharedClockOffset(path, clock).offset_ms() == MAX_APPLIED_OFFSET_MS
    _write(path, _body(clock, -MAX_APPLIED_OFFSET_MS))
    assert SharedClockOffset(path, clock).offset_ms() == -MAX_APPLIED_OFFSET_MS
    _write(path, _body(clock, MAX_APPLIED_OFFSET_MS + 1))
    assert SharedClockOffset(path, clock).offset_ms() == 0
    _write(path, _body(clock, -(MAX_APPLIED_OFFSET_MS + 1)))
    assert SharedClockOffset(path, clock).offset_ms() == 0


def test_a_stale_offset_is_not_applied(path: Path, clock: FakeClock) -> None:
    _write(path, _body(clock, 5000))
    reader = SharedClockOffset(path, clock)
    assert reader.offset_ms() == 5000
    clock.advance(MAX_OFFSET_AGE_S - 1)
    assert reader.offset_ms() == 5000
    clock.advance(2)
    assert reader.offset_ms() == 0


def test_an_offset_from_before_a_clock_step_is_not_applied(path: Path) -> None:
    """Windows time sync stepped the PC clock after the offset was measured:
    applying it would correct twice."""
    before = FakeClock(start_monotonic=1000.0, start_utc=T0)
    _write(path, _body(before, 5000))
    small = FakeClock(start_monotonic=1060.0, start_utc=T0 + timedelta(seconds=61))
    assert SharedClockOffset(path, small).offset_ms() == 5000  # 1 s slew: tolerated
    stepped = FakeClock(
        start_monotonic=1060.0, start_utc=T0 + timedelta(seconds=60 + STEP_TOLERANCE_S + 3)
    )
    assert SharedClockOffset(path, stepped).offset_ms() == 0
    back = FakeClock(start_monotonic=1060.0, start_utc=T0 + timedelta(seconds=55))
    assert SharedClockOffset(path, back).offset_ms() == 0


def test_an_offset_from_another_boot_is_not_applied(path: Path) -> None:
    before = FakeClock(start_monotonic=90_000.0, start_utc=T0)
    _write(path, _body(before, 5000))
    rebooted = FakeClock(start_monotonic=30.0, start_utc=T0 + timedelta(minutes=5))
    assert SharedClockOffset(path, rebooted).offset_ms() == 0


def test_the_file_is_read_at_most_once_per_interval(path: Path, clock: FakeClock) -> None:
    reads: list[Path] = []

    def read(p: Path) -> str:
        reads.append(p)
        return p.read_text(encoding="utf-8")

    OffsetPublisher(path, clock).publish(5000)
    reader = SharedClockOffset(path, clock, read_text=read)
    for _ in range(50):
        assert reader.offset_ms() == 5000
    assert len(reads) == 1
    OffsetPublisher(path, clock).publish(-700)
    clock.advance(READ_INTERVAL_S / 2)
    assert reader.offset_ms() == 5000
    clock.advance(READ_INTERVAL_S / 2)
    assert reader.offset_ms() == -700
    assert len(reads) == 2
    path.unlink()
    clock.advance(READ_INTERVAL_S)
    assert reader.offset_ms() == 0  # removed: raw clock


def test_a_transient_read_error_keeps_the_last_offset_briefly(path: Path, clock: FakeClock) -> None:
    """On Windows a reader can collide with the writer's atomic replace
    (sharing violation): hold the last good value instead of flapping."""
    failing = False

    def read(p: Path) -> str:
        if failing:
            raise PermissionError("sharing violation")
        return p.read_text(encoding="utf-8")

    OffsetPublisher(path, clock).publish(5000)
    reader = SharedClockOffset(path, clock, read_text=read)
    assert reader.offset_ms() == 5000
    failing = True
    clock.advance(READ_INTERVAL_S)
    assert reader.offset_ms() == 5000
    clock.advance(HOLD_ON_READ_ERROR_S)
    assert reader.offset_ms() == 0
    failing = False
    clock.advance(READ_INTERVAL_S)
    assert reader.offset_ms() == 5000


def test_a_read_error_with_nothing_held_is_the_raw_clock(path: Path, clock: FakeClock) -> None:
    def read(p: Path) -> str:
        raise OSError("device not ready")

    assert SharedClockOffset(path, clock, read_text=read).offset_ms() == 0


def test_the_publisher_writes_changes_and_refreshes(path: Path, clock: FakeClock) -> None:
    publisher = OffsetPublisher(path, clock)
    assert publisher.publish(5000)
    first = path.read_text(encoding="utf-8")
    clock.advance(2)
    assert not publisher.publish(5005)  # below the change threshold, recently written
    assert path.read_text(encoding="utf-8") == first
    assert publisher.publish(5100)
    clock.advance(PUBLISH_REFRESH_S)
    assert publisher.publish(5100)  # unchanged but old: anchors refreshed
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["offset_ms"] == 5100
    assert body["mono"] == clock.monotonic()
    assert not list(path.parent.glob("*.tmp*"))  # atomic replace leaves no temporaries


def test_the_publisher_never_raises(tmp_path: Path, clock: FakeClock) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    publisher = OffsetPublisher(blocker / "sub" / OFFSET_FILE, clock)
    assert not publisher.publish(5000)


def _sync(
    tmp_path: Path, clock: FakeClock, device: FakeDeviceServer
) -> tuple[EventQueue, SyncService]:
    keys = InMemoryKeyProvider(bytes(range(32)))
    queue = EventQueue.open(tmp_path / "queue.db", keys, clock)
    service = build_sync_service(
        data_dir=tmp_path,
        keys=keys,
        queue=queue,
        clock=clock,
        transport=HttpxTransport("https://ag.test", transport=httpx.MockTransport(device)),
        credentials=InMemoryCredentialStore(DeviceCredentials(STATION, SecretStr("s"))),
        offset=ClockOffsetEstimator(),
    )
    return queue, service


def test_the_sync_client_publishes_its_offset_after_a_sample(
    tmp_path: Path, clock: FakeClock
) -> None:
    device = FakeDeviceServer(clock=clock, workstation_id=STATION, secret="s")  # noqa: S106 - fake
    device.skew = timedelta(seconds=-4)
    queue, service = _sync(tmp_path, clock, device)
    try:
        reader = SharedClockOffset(offset_path(tmp_path), clock)
        service.client.tick()
        assert abs(reader.offset_ms() + 4000) < 50
        # The sync client's own records and the shared clock agree.
        utc = stamping_clock(clock, tmp_path)
        assert abs((utc.now() - service.client.utc.now()).total_seconds()) < 0.05
    finally:
        service.stop(0.5)
        queue.close()


def test_no_sample_publishes_nothing(tmp_path: Path, clock: FakeClock) -> None:
    """Offline from the start: nothing measured, nothing published, raw clock."""
    device = FakeDeviceServer(clock=clock, workstation_id=STATION, secret="s")  # noqa: S106 - fake
    device.inject("/", Fault.DROP_REQUEST, times=1_000)
    queue, service = _sync(tmp_path, clock, device)
    try:
        for _ in range(3):
            service.client.tick()
            clock.advance(5)
        assert not offset_path(tmp_path).exists()
        assert stamping_clock(clock, tmp_path).now() == clock.now()
        assert service.client.health().clock_offset_ms == 0
    finally:
        service.stop(0.5)
        queue.close()
