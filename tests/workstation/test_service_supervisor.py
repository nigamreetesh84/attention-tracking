"""Engine supervisor restart logic with a fake child and an injected clock."""

import threading

import pytest
from ag_shared.api import LocalAlertKind, LocalAlertPayload
from ag_shared.health import EngineStatus
from ag_workstation.clock import FakeClock
from ag_workstation.service import (
    RESTART_DEADLINE_S,
    EngineSupervisor,
    FailureReason,
    SupervisorConfig,
)

POLL = 0.25


class FakeChild:
    def __init__(self, pid: int) -> None:
        self._pid = pid
        self.alive = True
        self.beats = 0
        self.beating = True
        self.killed = False
        self._exitcode: int | None = None

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def exitcode(self) -> int | None:
        return self._exitcode

    def is_alive(self) -> bool:
        return self.alive

    def heartbeat(self) -> int:
        return self.beats

    def kill(self) -> None:
        self.killed = True
        self.die(-9)

    def die(self, code: int = 1) -> None:
        self.alive = False
        self._exitcode = code


class FakeLauncher:
    def __init__(self) -> None:
        self.children: list[FakeChild] = []
        self.fail_next = 0
        self.launched_at: list[float] = []
        self.clock: FakeClock | None = None

    def launch(self) -> FakeChild:
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("cannot start")
        child = FakeChild(1000 + len(self.children))
        self.children.append(child)
        if self.clock is not None:
            self.launched_at.append(self.clock.monotonic())
        return child

    @property
    def current(self) -> FakeChild:
        return self.children[-1]


class Rig:
    def __init__(self, config: SupervisorConfig | None = None, alert_raises: bool = False) -> None:
        self.clock = FakeClock()
        self.launcher = FakeLauncher()
        self.launcher.clock = self.clock
        self.alerts: list[LocalAlertPayload] = []
        self.alert_raises = alert_raises
        self.sup = EngineSupervisor(self.launcher, self.clock, on_alert=self._alert, config=config)

    def _alert(self, payload: LocalAlertPayload) -> None:
        self.alerts.append(payload)
        if self.alert_raises:
            raise RuntimeError("queue closed")

    def step(self, seconds: float) -> None:
        """Advance in poll steps; live, beating children beat each step."""
        steps = round(seconds / POLL)
        for _ in range(steps):
            self.clock.advance(POLL)
            for child in self.launcher.children:
                if child.alive and child.beating:
                    child.beats += 1
            self.sup.poll()

    def until_relaunch(self, limit: float = 30.0) -> float:
        """Seconds until a new child is launched."""
        start, count = self.clock.monotonic(), len(self.launcher.children)
        while len(self.launcher.children) == count:
            self.step(POLL)
            assert self.clock.monotonic() - start <= limit, "no relaunch"
        return self.clock.monotonic() - start


def test_start_runs_after_first_heartbeat() -> None:
    rig = Rig()
    rig.sup.start()
    rig.sup.start()  # idempotent
    assert rig.sup.health().status is EngineStatus.STARTING
    assert rig.sup.health().pid == 1000
    rig.step(POLL)
    health = rig.sup.health()
    assert health.status is EngineStatus.RUNNING
    assert (health.restarts, health.last_error, health.consecutive_failures) == (0, None, 0)
    assert rig.alerts == []


def test_dead_child_is_restarted_within_deadline() -> None:
    rig = Rig()
    rig.sup.start()
    rig.step(1.0)
    rig.launcher.current.die(3)
    waited = rig.until_relaunch()
    assert waited <= RESTART_DEADLINE_S
    assert waited <= POLL  # first failure: no backoff
    health = rig.sup.health()
    assert health.restarts == 1
    assert health.last_error == FailureReason.EXITED.value
    assert health.status is EngineStatus.STARTING
    rig.step(POLL)
    assert rig.sup.health().status is EngineStatus.RUNNING
    [alert] = rig.alerts
    assert alert.kind is LocalAlertKind.ENGINE_RESTART
    assert alert.detail["reason"] == "exited"
    assert alert.detail["restarts"] == 1


def test_hung_child_is_killed_and_restarted_within_deadline() -> None:
    rig = Rig()
    rig.sup.start()
    rig.step(1.0)
    hung = rig.launcher.current
    hung.beating = False
    waited = rig.until_relaunch()
    assert hung.killed
    assert 2.0 < waited <= RESTART_DEADLINE_S
    assert rig.sup.health().last_error == FailureReason.HUNG.value
    assert rig.alerts[0].detail["reason"] == "hung"


def test_flapping_child_backs_off_capped_and_each_restart_within_deadline() -> None:
    rig = Rig()
    rig.sup.start()
    waits = []
    for _ in range(7):
        rig.step(POLL)
        rig.launcher.current.die()
        waits.append(rig.until_relaunch())
    # Death is seen on the next poll; then 0, 0.5, 1, 2, 2.5, 2.5 ... backoff.
    assert waits[0] <= POLL
    assert waits == sorted(waits)
    assert max(waits) <= RESTART_DEADLINE_S
    assert waits[-1] == pytest.approx(waits[-2])  # capped
    assert rig.sup.health().restarts == 7
    assert rig.sup.health().consecutive_failures == 7
    # Flapping raises one alert per interval; the rest are counted.
    assert len(rig.alerts) == 1
    rig.step(60.0)
    rig.launcher.current.die()
    rig.until_relaunch()
    assert len(rig.alerts) == 2
    assert rig.alerts[1].detail["suppressed"] == 6


def test_backoff_resets_after_stable_run() -> None:
    rig = Rig()
    rig.sup.start()
    for _ in range(3):
        rig.step(POLL)
        rig.launcher.current.die()
        rig.until_relaunch()
    assert rig.sup.health().consecutive_failures == 3
    rig.step(31.0)
    assert rig.sup.health().consecutive_failures == 0
    rig.launcher.current.die()
    assert rig.until_relaunch() <= POLL


def test_child_that_never_beats_hits_startup_timeout() -> None:
    config = SupervisorConfig(startup_timeout_s=3.0)
    rig = Rig(config)
    rig.sup.start()
    rig.launcher.current.beating = False
    rig.step(2.75)
    assert rig.sup.health().status is EngineStatus.STARTING
    rig.step(0.5)
    assert rig.sup.health().last_error == FailureReason.STARTUP_TIMEOUT.value
    assert rig.launcher.children[0].killed


def test_launch_failures_are_retried_with_backoff() -> None:
    rig = Rig()
    rig.launcher.fail_next = 3
    rig.sup.start()  # fails, immediate retry fails, then backoff
    health = rig.sup.health()
    assert health.status is EngineStatus.RESTARTING
    assert health.last_error == FailureReason.LAUNCH_FAILED.value
    assert health.pid is None
    assert rig.launcher.children == []
    rig.step(5.0)
    assert len(rig.launcher.children) == 1
    rig.step(POLL)
    assert rig.sup.health().status is EngineStatus.RUNNING
    assert rig.sup.health().restarts == 3


def test_failing_alert_callback_does_not_stop_restarts() -> None:
    rig = Rig(alert_raises=True)
    rig.sup.start()
    rig.step(POLL)
    rig.launcher.current.die()
    rig.until_relaunch()
    rig.step(POLL)
    rig.launcher.current.die()
    rig.until_relaunch()
    assert rig.sup.health().restarts == 2
    assert len(rig.alerts) == 2  # not rate-limited: the first was never delivered


def test_no_alert_callback_is_fine() -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    sup = EngineSupervisor(launcher, clock)
    sup.start()
    launcher.current.die()
    sup.poll()
    assert sup.health().restarts == 1


def test_stop_kills_child_and_stops_supervising() -> None:
    rig = Rig()
    rig.sup.poll()  # not started: nothing happens
    assert rig.launcher.children == []
    rig.sup.start()
    rig.step(POLL)
    child = rig.launcher.current
    rig.sup.stop()
    assert child.killed
    assert rig.sup.health().status is EngineStatus.DOWN
    rig.step(10.0)
    assert len(rig.launcher.children) == 1


def test_poll_survives_a_broken_child() -> None:
    rig = Rig()
    rig.sup.start()

    def boom() -> bool:
        raise RuntimeError("broken")

    rig.launcher.current.is_alive = boom  # type: ignore[method-assign]  # inject a fault
    rig.sup.poll()  # logged, not raised


def test_kill_that_raises_is_contained() -> None:
    rig = Rig()
    rig.sup.start()
    child = rig.launcher.current

    def bad_kill() -> None:
        raise OSError("gone")

    child.kill = bad_kill  # type: ignore[method-assign]  # inject a fault
    child.die()
    rig.step(POLL)
    assert rig.sup.health().restarts == 1


def test_run_forever_polls_until_stopped() -> None:
    config = SupervisorConfig(poll_interval_s=0.01)
    rig = Rig(config)
    rig.sup.start()
    rig.launcher.current.die()
    stop = threading.Event()
    thread = threading.Thread(target=rig.sup.run_forever, args=(stop,))
    thread.start()
    try:
        for _ in range(200):
            if rig.sup.health().restarts:
                break
            stop.wait(0.005)
    finally:
        stop.set()
        thread.join(1.0)
    assert rig.sup.health().restarts == 1


def test_backoff_delays() -> None:
    sup = EngineSupervisor(FakeLauncher(), FakeClock())
    assert [sup.backoff_delay(n) for n in range(1, 7)] == [0.0, 0.5, 1.0, 2.0, 2.5, 2.5]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"poll_interval_s": 0.0},
        {"heartbeat_timeout_s": -1.0},
        {"backoff_initial_s": -0.1},
        {"backoff_initial_s": 3.0, "backoff_max_s": 2.0},
        {"heartbeat_timeout_s": 3.0, "backoff_max_s": 2.5},
    ],
)
def test_config_rejects_unsafe_timing(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        SupervisorConfig(**kwargs)
