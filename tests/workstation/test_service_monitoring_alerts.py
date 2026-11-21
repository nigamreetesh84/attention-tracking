"""Camera and monitoring alerts from the service's health snapshots (scope 4.4, PLAN 2.11)."""

from datetime import UTC, datetime

import pytest
from ag_shared.api import LocalAlertKind, LocalAlertPayload
from ag_shared.health import CameraStatus, EngineStatus
from ag_workstation.clock import FakeClock
from ag_workstation.engine_api import CameraHealthSummary
from ag_workstation.service.health import HealthSnapshot
from ag_workstation.service.monitoring_alerts import (
    ALERT_AFTER_S,
    ALERT_MIN_INTERVAL_S,
    MonitoringAlerter,
)

ON, OFF, STARTING = CameraStatus.ONLINE, CameraStatus.OFFLINE, CameraStatus.STARTING


class Rig:
    def __init__(self, **kwargs: float) -> None:
        self.clock = FakeClock(start_utc=datetime(2026, 11, 21, tzinfo=UTC))
        self.status = EngineStatus.RUNNING
        self.cameras: dict[int, CameraStatus] = {1: ON, 2: ON}
        self.alerts: list[LocalAlertPayload] = []
        self.alerter = MonitoringAlerter(self.clock, self.snapshot, self.alerts.append, **kwargs)

    def snapshot(self) -> HealthSnapshot:
        live = self.status in (EngineStatus.RUNNING, EngineStatus.DEGRADED)
        cameras = tuple(CameraHealthSummary(s, st) for s, st in self.cameras.items())
        return HealthSnapshot(
            taken_at=self.clock.now(),
            engine_status=self.status,
            engine_restarts=0,
            engine_last_error=None,
            cameras=cameras if live else (),
        )

    def step(self, seconds: float, every: float = 5.0) -> None:
        for _ in range(round(seconds / every)):
            self.alerter.check()
            self.clock.advance(every)
        self.alerter.check()

    def kinds(self) -> list[tuple[LocalAlertKind, object]]:
        return [
            (a.kind, a.detail.get("slot", a.detail.get("cameras_offline"))) for a in self.alerts
        ]


def test_a_camera_offline_for_the_limit_alerts_once_per_episode() -> None:
    rig = Rig()
    rig.cameras[2] = OFF
    rig.step(ALERT_AFTER_S - 5.0)
    assert rig.alerts == []
    rig.step(5.0)
    assert rig.kinds() == [(LocalAlertKind.CAMERA_OFFLINE, 2)]
    assert rig.alerts[0].detail["status"] == "offline"
    rig.step(120.0)
    assert len(rig.alerts) == 1  # same episode
    # Monitoring itself is available (camera 1 online).
    assert all(a.kind is LocalAlertKind.CAMERA_OFFLINE for a in rig.alerts)


def test_a_short_blip_does_not_alert_and_recovery_ends_the_episode() -> None:
    rig = Rig(min_interval_s=0.0)
    rig.cameras[1] = STARTING
    rig.step(10.0)
    rig.cameras[1] = ON
    rig.step(5.0)
    rig.cameras[1] = OFF
    rig.step(ALERT_AFTER_S - 5.0)
    assert rig.alerts == []  # the blip did not count towards the limit
    rig.step(5.0)
    rig.cameras[1] = ON
    rig.step(5.0)
    rig.cameras[1] = OFF
    rig.step(ALERT_AFTER_S)
    assert rig.kinds() == [(LocalAlertKind.CAMERA_OFFLINE, 1)] * 2  # a new episode


def test_flapping_is_rate_limited_per_slot() -> None:
    rig = Rig()
    for _ in range(3):
        rig.cameras[2] = OFF
        rig.step(ALERT_AFTER_S)
        rig.cameras[2] = ON
        rig.step(5.0)
    assert len(rig.alerts) == 1
    assert rig.alerter.alerts_suppressed == 2
    rig.clock.advance(ALERT_MIN_INTERVAL_S)
    rig.cameras[2] = OFF
    rig.step(ALERT_AFTER_S)
    assert len(rig.alerts) == 2


def test_all_cameras_offline_alerts_monitoring_unavailable_too() -> None:
    rig = Rig()
    rig.cameras = {1: OFF, 2: OFF}
    rig.step(ALERT_AFTER_S)
    assert sorted(rig.kinds(), key=str) == sorted(
        [
            (LocalAlertKind.CAMERA_OFFLINE, 1),
            (LocalAlertKind.CAMERA_OFFLINE, 2),
            (LocalAlertKind.MONITORING_UNAVAILABLE, [1, 2]),
        ],
        key=str,
    )


def test_engine_down_keeps_camera_episodes_and_alerts_monitoring_unavailable() -> None:
    rig = Rig()
    rig.cameras[2] = OFF
    rig.step(10.0)
    rig.status = EngineStatus.DOWN  # no camera information while the engine is down
    rig.step(ALERT_AFTER_S)
    assert rig.kinds() == [(LocalAlertKind.MONITORING_UNAVAILABLE, [])]
    assert rig.alerts[0].detail["engine_status"] == "down"
    rig.status = EngineStatus.RUNNING
    rig.alerter.check()
    # Camera 2 was offline before, during and after: its episode kept running.
    assert (LocalAlertKind.CAMERA_OFFLINE, 2) in rig.kinds()


def test_failures_are_contained() -> None:
    rig = Rig()

    def broken() -> HealthSnapshot:
        raise RuntimeError("health bug")

    MonitoringAlerter(rig.clock, broken, rig.alerts.append).check()  # does not raise

    def refuse(payload: LocalAlertPayload) -> None:
        raise OSError("queue full")

    failing = MonitoringAlerter(rig.clock, rig.snapshot, refuse, after_s=0.0)
    rig.cameras[1] = OFF
    failing()
    assert failing.alerts_raised == 0
    with pytest.raises(ValueError, match="negative"):
        MonitoringAlerter(rig.clock, rig.snapshot, rig.alerts.append, after_s=-1.0)
