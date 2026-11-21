"""Sync building blocks (PLAN 2.9): sealed files, credentials, config store,
clock offset, command inbox and pump, transport, runner, consumer."""

import json
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from ag_shared.api import AssignWorkerCommand, ConfigBundle, EndSessionCommand
from ag_shared.ids import new_uuid7
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_workstation.clock import FakeClock
from ag_workstation.queue import InMemoryKeyProvider, PayloadIntegrityError
from ag_workstation.sync import (
    BundleFileWatcher,
    ClockOffsetEstimator,
    CommandInbox,
    CommandPump,
    ConfigStore,
    CorrectedClock,
    DeviceCredentials,
    HttpxTransport,
    InMemoryCredentialStore,
    SealedFileCredentialStore,
    SyncRunner,
    TransportError,
    bundle_path,
    load_bundle,
    problem_code,
    tls_context,
)
from ag_workstation.sync.consumer import SyncConsumer
from ag_workstation.sync.sealed_file import SealedFile
from ag_workstation.sync.transport import HttpResponse
from pydantic import SecretStr

Json = dict[str, Any]  # Any: free-form JSON fixtures

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
T0 = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
STATION = UUID("0193a0b0-0000-7000-8000-00000000d001")


def bundle_json(version: int = 7, uri: str = "rtsp://user:pw@cam/1") -> Json:
    return {
        "version": version,
        "generated_at": "2026-11-21T07:00:00Z",
        "site": {"timezone": "Asia/Kolkata"},
        "workstation": {
            "id": str(STATION),
            "name": "WS",
            "line_id": str(STATION),
            "camera_count": 1,
        },
        "cameras": [{"slot": 1, "role": "primary", "kind": "rtsp", "uri": uri}],
        "ppe_profile": builtin_profile(PpeProfileKind.OPEN_FACE).model_dump(mode="json"),
        "thresholds": {},
    }


def raw(version: int = 7, **kw: Any) -> bytes:  # Any: overrides for the fixture
    return json.dumps(bundle_json(version, **kw)).encode()


# -- sealed files and credentials ------------------------------------------------------


def test_sealed_file_round_trip_and_tamper(tmp_path: Path) -> None:
    f = SealedFile(tmp_path / "x.bin", InMemoryKeyProvider(KEY), "a")
    assert f.read() is None and not f.exists()
    f.write(b"hello")
    assert f.read() == b"hello"
    assert b"hello" not in (tmp_path / "x.bin").read_bytes()
    assert not list(tmp_path.glob(".tmp-*"))
    with pytest.raises(PayloadIntegrityError):
        SealedFile(tmp_path / "x.bin", InMemoryKeyProvider(KEY), "b").read()  # other purpose
    with pytest.raises(PayloadIntegrityError):
        SealedFile(tmp_path / "x.bin", InMemoryKeyProvider(OTHER_KEY), "a").read()
    (tmp_path / "x.bin").write_bytes(b"junk")
    with pytest.raises(PayloadIntegrityError):
        f.read()


def test_sealed_file_failed_write_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = SealedFile(tmp_path / "x.bin", InMemoryKeyProvider(KEY), "a")
    f.write(b"old")

    def boom(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ag_workstation.sync.sealed_file.os.replace", boom)
    with pytest.raises(OSError):
        f.write(b"new")
    assert f.read() == b"old"
    assert not list(tmp_path.glob(".tmp-*"))


def test_sealed_credentials_round_trip_without_plaintext(tmp_path: Path) -> None:
    store = SealedFileCredentialStore(tmp_path / "device.agk", InMemoryKeyProvider(KEY))
    assert store.load() is None
    store.save(DeviceCredentials(STATION, SecretStr("topsecret")))
    loaded = store.load()
    assert loaded is not None and loaded.workstation_id == STATION
    assert loaded.device_secret.get_secret_value() == "topsecret"
    assert b"topsecret" not in (tmp_path / "device.agk").read_bytes()
    assert "topsecret" not in repr(loaded)


def test_in_memory_credentials_fault_injection() -> None:
    store = InMemoryCredentialStore()
    store.fail_saves = 1
    with pytest.raises(OSError):
        store.save(DeviceCredentials(STATION, SecretStr("x")))
    store.save(DeviceCredentials(STATION, SecretStr("x")))
    assert store.load() is not None


# -- config store -----------------------------------------------------------------------


def test_config_store_applies_persists_and_reloads(tmp_path: Path) -> None:
    keys = InMemoryKeyProvider(KEY)
    store = ConfigStore(bundle_path(tmp_path), keys, workstation_id=STATION)
    assert store.current() is None and store.applied_version is None
    result = store.apply(raw(7))
    assert result.applied and result.rejected is None and store.applied_version == 7
    assert b"user:pw" not in bundle_path(tmp_path).read_bytes()  # sealed: URIs carry credentials
    assert ConfigStore(bundle_path(tmp_path), keys).applied_version == 7
    loaded = load_bundle(bundle_path(tmp_path), keys)
    assert loaded is not None and loaded.version == 7


def test_invalid_bundle_keeps_the_previous_and_names_no_values(tmp_path: Path) -> None:
    store = ConfigStore(bundle_path(tmp_path), InMemoryKeyProvider(KEY))
    store.apply(raw(7))
    bad = bundle_json(8, uri="rtsp://admin:hunter2@cam/1")
    bad["cameras"][0]["kind"] = "laser"
    result = store.apply(json.dumps(bad).encode())
    assert not result.applied
    assert result.bundle is not None and result.bundle.version == 7
    assert result.rejected is not None and result.rejected.version == 8
    assert "hunter2" not in result.rejected.reason
    assert store.applied_version == 7
    assert load_bundle(bundle_path(tmp_path), InMemoryKeyProvider(KEY)).version == 7  # type: ignore[union-attr]  # checked above


@pytest.mark.parametrize(
    ("body", "advertised", "version"),
    [(b"{", 9, 9), (b"[]", None, 0), (b'{"version": 12}', 3, 12), (b'{"version": -1}', 4, 4)],
)
def test_rejected_version_falls_back_to_the_advertised_one(
    tmp_path: Path, body: bytes, advertised: int | None, version: int
) -> None:
    store = ConfigStore(bundle_path(tmp_path), InMemoryKeyProvider(KEY))
    result = store.apply(body, advertised_version=advertised)
    assert result.rejected is not None and result.rejected.version == version


def test_bundle_that_cannot_be_stored_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConfigStore(bundle_path(tmp_path), InMemoryKeyProvider(KEY))
    store.apply(raw(7))

    def boom(self: SealedFile, data: bytes) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(SealedFile, "write", boom)
    result = store.apply(raw(8))
    assert not result.applied and store.applied_version == 7
    assert result.rejected is not None and "not stored" in result.rejected.reason


def test_unreadable_stored_bundle_loads_as_none(tmp_path: Path) -> None:
    path = bundle_path(tmp_path)
    ConfigStore(path, InMemoryKeyProvider(KEY)).apply(raw(7))
    assert load_bundle(path, InMemoryKeyProvider(OTHER_KEY)) is None
    assert ConfigStore(path, InMemoryKeyProvider(OTHER_KEY)).current() is None


def test_bundle_watcher_reports_each_new_file_once(tmp_path: Path) -> None:
    keys = InMemoryKeyProvider(KEY)
    path = bundle_path(tmp_path)
    watcher = BundleFileWatcher(path, keys)
    assert watcher.poll() is None  # no file yet
    store = ConfigStore(path, keys)
    store.apply(raw(7))
    first = watcher.poll()
    assert first is not None and first.version == 7
    assert watcher.poll() is None
    time.sleep(0.01)
    store.apply(raw(8))
    second = watcher.poll()
    assert second is not None and second.version == 8


# -- clock offset -----------------------------------------------------------------------


def test_offset_sample_uses_the_round_trip_midpoint() -> None:
    est = ClockOffsetEstimator()
    sent = T0
    received = T0 + timedelta(milliseconds=200)
    server = T0 + timedelta(milliseconds=100 + 3000)
    assert est.add_sample(server, sent, received)
    assert est.raw_ms == 3000 and est.smoothed_ms == 3000 and est.drift


def test_offset_smooths_ignores_slow_samples_and_resets_on_a_step() -> None:
    est = ClockOffsetEstimator(alpha=0.5)
    assert est.smoothed_ms == 0 and est.raw_ms is None
    est.add_sample(T0 + timedelta(milliseconds=100), T0, T0)
    est.add_sample(T0 + timedelta(milliseconds=300), T0, T0)
    assert est.smoothed_ms == 200
    assert not est.add_sample(T0 + timedelta(seconds=9), T0, T0 + timedelta(seconds=3))
    assert not est.add_sample(T0, T0, T0 - timedelta(seconds=1))
    est.add_sample(T0 + timedelta(seconds=60), T0, T0)  # device clock stepped
    assert est.smoothed_ms == 60_000
    with pytest.raises(ValueError):
        ClockOffsetEstimator(alpha=0)


def test_drift_has_hysteresis() -> None:
    est = ClockOffsetEstimator()
    for ms, drift in [(2100, True), (1800, True), (1400, False), (1999, False), (-2500, True)]:
        est.add_sample(T0 + timedelta(milliseconds=ms), T0, T0)
        assert est.drift is drift, ms


def test_corrected_clock_shifts_wall_time_only() -> None:
    base = FakeClock(start_monotonic=5.0, start_utc=T0)
    est = ClockOffsetEstimator(initial_ms=1500)
    clock = CorrectedClock(base, est)
    assert clock.monotonic() == 5.0
    assert clock.now() == T0 + timedelta(milliseconds=1500)


# -- command inbox and pump ------------------------------------------------------------


def _assign() -> AssignWorkerCommand:
    return AssignWorkerCommand(
        id=new_uuid7(),
        assignment_id=new_uuid7(),
        user_id=new_uuid7(),
        valid_until=T0 + timedelta(hours=1),
    )


class Handler:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[UUID] = []

    def handle_assign_worker(self, command: AssignWorkerCommand) -> str:
        self.calls.append(command.id)
        if self.fail:
            raise RuntimeError("bug")
        return "started"

    def handle_end_session(self, command: EndSessionCommand) -> str:
        self.calls.append(command.id)
        return "not_current"


def test_inbox_lifecycle_across_two_connections(tmp_path: Path) -> None:
    clock = FakeClock(start_utc=T0)
    service = CommandInbox.open(tmp_path / "commands.db", clock)
    ui = CommandInbox.open(tmp_path / "commands.db", clock)
    cmd = _assign()
    end = EndSessionCommand(id=new_uuid7(), session_id=new_uuid7())
    assert service.put(cmd) and not service.put(cmd)
    service.put(end)
    handler = Handler()
    assert CommandPump(ui, handler).poll() == 2
    assert handler.calls == [cmd.id, end.id]
    assert ui.outcome(cmd.id) == "started" and ui.outcome(end.id) == "not_current"
    assert ui.outcome(new_uuid7()) is None
    assert sorted(service.handled_unacked()) == sorted([cmd.id, end.id])
    service.mark_acked([cmd.id])
    assert service.handled_unacked() == [end.id]
    assert service.purge() == 0
    clock.advance(8 * 86400)
    assert service.purge() == 1
    service.close()
    ui.close()


def test_pump_marks_a_failing_command_final(tmp_path: Path) -> None:
    inbox = CommandInbox.open(tmp_path / "c.db", FakeClock(start_utc=T0))
    cmd = _assign()
    inbox.put(cmd)
    handler = Handler(fail=True)
    assert CommandPump(inbox, handler).poll() == 1
    assert inbox.outcome(cmd.id) == "error"
    assert CommandPump(inbox, handler).poll() == 0
    inbox.close()


def test_unreadable_inbox_row_is_dropped(tmp_path: Path) -> None:
    inbox = CommandInbox.open(tmp_path / "c.db", FakeClock(start_utc=T0))
    rid = new_uuid7()
    inbox._conn.execute(
        "INSERT INTO command (id, type, body, received_at) VALUES (?, 'x', '{', 0)", (str(rid),)
    )
    assert inbox.pending() == []
    assert inbox.outcome(rid) == "error"
    inbox.close()


def test_pump_survives_a_closed_inbox(tmp_path: Path) -> None:
    inbox = CommandInbox.open(tmp_path / "c.db", FakeClock(start_utc=T0))
    inbox.close()
    assert CommandPump(inbox, Handler()).poll() == 0


# -- transport ---------------------------------------------------------------------------


def test_http_is_refused_unless_allowed() -> None:
    with pytest.raises(ValueError, match="https"):
        HttpxTransport("http://ag.test")
    HttpxTransport("http://localhost:8000", allow_insecure=True).close()


def test_tls_context_requires_tls12_and_verification() -> None:
    ctx = tls_context()
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode is ssl.CERT_REQUIRED and ctx.check_hostname


def test_transport_errors_and_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/down":
            raise httpx.ConnectTimeout("nope", request=request)
        return httpx.Response(
            418,
            content=b'{"status":418,"code":"teapot"}',
            headers={"Content-Type": "application/problem+json"},
        )

    t = HttpxTransport("https://ag.test", transport=httpx.MockTransport(handler))
    with pytest.raises(TransportError):
        t.request("GET", "/down", headers={})
    response = t.request("GET", "/x", headers={"A": "b"})
    assert response.status == 418 and response.headers["content-type"].startswith("application")
    assert problem_code(response) == "teapot"
    t.close()


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(200, {"content-type": "application/json"}, b'{"code":"x"}'),
        HttpResponse(500, {"content-type": "text/html"}, b"<h1>"),
        HttpResponse(500, {"content-type": "application/json"}, b"[1]"),
        HttpResponse(500, {}, b""),
    ],
)
def test_problem_code_only_from_problem_bodies(response: HttpResponse) -> None:
    assert problem_code(response) is None


# -- runner ------------------------------------------------------------------------------


class Ticker:
    def __init__(self, *, raise_every: int = 0, block: threading.Event | None = None) -> None:
        self.calls = 0
        self.raise_every = raise_every
        self.block = block

    def tick(self) -> float:
        self.calls += 1
        if self.block is not None:
            self.block.wait()
        if self.raise_every and self.calls % self.raise_every == 0:
            raise RuntimeError("bug")
        return 0.0


def test_runner_keeps_going_after_a_raising_tick() -> None:
    crashes: list[Exception] = []
    ticker = Ticker(raise_every=2)
    runner = SyncRunner(ticker, min_sleep_s=0.001, on_crash=crashes.append)
    runner.start()
    runner.start()  # idempotent
    deadline = time.monotonic() + 5
    while ticker.calls < 6 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.running
    assert runner.stop()
    assert ticker.calls >= 6 and crashes
    assert not runner.running
    assert runner.stop()  # twice is fine


def test_runner_abandons_a_hung_tick_within_the_timeout() -> None:
    block = threading.Event()
    runner = SyncRunner(Ticker(block=block))
    runner.start()
    started = time.monotonic()
    assert not runner.stop(timeout=0.1)
    assert time.monotonic() - started < 1.0
    block.set()


# -- consumer ----------------------------------------------------------------------------


class Roster:
    def __init__(self) -> None:
        self.bundles: list[ConfigBundle | None] = []

    def set_bundle(self, bundle: ConfigBundle | None) -> None:
        self.bundles.append(bundle)


def test_consumer_installs_bundles_and_runs_commands(tmp_path: Path) -> None:
    keys = InMemoryKeyProvider(KEY)
    store = ConfigStore(bundle_path(tmp_path), keys)
    store.apply(raw(7))
    inbox = CommandInbox.open(tmp_path / "c.db", FakeClock(start_utc=T0))
    cmd = _assign()
    inbox.put(cmd)
    roster, seen, handler = Roster(), [], Handler()
    closed: list[bool] = []
    consumer = SyncConsumer(
        watcher=BundleFileWatcher(bundle_path(tmp_path), keys),
        roster=roster,
        pump=CommandPump(inbox, handler),
        on_bundle=lambda b: seen.append(b.version),
        interval_s=0.01,
        on_close=lambda: closed.append(True),
    )
    consumer.start()
    deadline = time.monotonic() + 5
    while (not seen or not handler.calls) and time.monotonic() < deadline:
        time.sleep(0.01)
    consumer.close()
    consumer.close()
    assert [b.version for b in roster.bundles if b] == [7] and seen == [7]
    assert handler.calls == [cmd.id] and closed == [True]
    inbox.close()


def test_consumer_contains_failures(tmp_path: Path) -> None:
    keys = InMemoryKeyProvider(KEY)
    ConfigStore(bundle_path(tmp_path), keys).apply(raw(7))

    class BadPump:
        def poll(self) -> int:
            raise RuntimeError("bug")

    def explode(bundle: ConfigBundle) -> None:
        raise RuntimeError("bug")

    consumer = SyncConsumer(
        watcher=BundleFileWatcher(bundle_path(tmp_path), keys),
        roster=Roster(),
        pump=BadPump(),  # type: ignore[arg-type]  # duck-typed failing pump
        on_bundle=explode,
    )
    consumer.poll_once()  # does not raise
