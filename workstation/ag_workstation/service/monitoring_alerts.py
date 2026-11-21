"""Camera and monitoring alerts raised by the service parent (scope 4.4, PLAN 2.11).

Scope 4.4: a camera going offline raises an admin/supervisor alert, and both
cameras offline is the same plus "Monitoring unavailable". The engine only
reports camera status in its health; this turns sustained problems into
`local_alert` records (contract 4.3, kinds `camera_offline` and
`monitoring_unavailable`), which the sync client uploads like any record.

`MonitoringAlerter.check()` runs from the service's housekeeping tick. It
reads one `HealthSnapshot` and:

- per camera slot: a camera reported not `online` for `after_s` (30 s, the
  degraded limit of detection-engine section 6 rule 7) queues one
  `camera_offline` alert with its slot; the episode ends when the camera is
  online again. A slot that is absent from a snapshot (engine down, no
  answer) keeps its episode as it was: no information is not a recovery.
- station: monitoring unavailable (engine not live, or no camera online)
  for `after_s` queues one `monitoring_unavailable` alert; the episode ends
  when monitoring is back.

Each kind (and each slot) alerts at most once per episode and at most once
per `min_interval_s`, so a flapping camera cannot flood the queue. `check()`
never raises: alerting is not allowed to disturb supervision or scanning.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ag_shared.api import LocalAlertKind, LocalAlertPayload
from ag_shared.health import CameraStatus

from ag_workstation.clock import Clock
from ag_workstation.service.health import HealthSnapshot

logger = logging.getLogger(__name__)

ALERT_AFTER_S: Final = 30.0
"""How long a problem must last before it is alerted (detection-engine 6.7)."""
ALERT_MIN_INTERVAL_S: Final = 600.0
"""At most one alert of a kind (per camera slot) in this interval."""
CHECK_INTERVAL_S: Final = 5.0
"""How often the service checks (housekeeping job interval)."""

AlertCallback = Callable[[LocalAlertPayload], None]


@dataclass(slots=True)
class _Episode:
    since: float
    alerted: bool = False


class MonitoringAlerter:
    """Queues `camera_offline` and `monitoring_unavailable` alerts from health snapshots."""

    def __init__(
        self,
        clock: Clock,
        snapshot: Callable[[], HealthSnapshot],
        on_alert: AlertCallback,
        *,
        after_s: float = ALERT_AFTER_S,
        min_interval_s: float = ALERT_MIN_INTERVAL_S,
    ) -> None:
        if after_s < 0 or min_interval_s < 0:
            raise ValueError("intervals must not be negative")
        self._clock = clock
        self._snapshot = snapshot
        self._on_alert = on_alert
        self._after_s = after_s
        self._min_interval_s = min_interval_s
        self._cameras: dict[int, _Episode] = {}
        self._unavailable: _Episode | None = None
        self._last_alert: dict[str, float] = {}
        self.alerts_raised = 0
        self.alerts_suppressed = 0

    def __call__(self) -> None:
        """Housekeeping entry point."""
        self.check()

    def check(self) -> None:
        """Read health once and raise what is due. Never raises."""
        try:
            snap = self._snapshot()
            now = self._clock.monotonic()
            self._check_cameras(snap, now)
            self._check_station(snap, now)
        except Exception as exc:  # alerting must never stop the service tick
            logger.warning("monitoring alert check failed: %s", type(exc).__name__)

    def _check_cameras(self, snap: HealthSnapshot, now: float) -> None:
        for camera in snap.cameras:
            slot = camera.slot
            if camera.status is CameraStatus.ONLINE:
                if self._cameras.pop(slot, None) is not None:
                    logger.info("camera %d back online", slot)
                continue
            episode = self._cameras.setdefault(slot, _Episode(now))
            if not episode.alerted and now - episode.since >= self._after_s:
                episode.alerted = True
                self._raise(
                    f"camera:{slot}",
                    LocalAlertPayload(
                        kind=LocalAlertKind.CAMERA_OFFLINE,
                        detail={
                            "slot": slot,
                            "status": camera.status.value,
                            "offline_s": round(now - episode.since),
                        },
                    ),
                    now,
                )

    def _check_station(self, snap: HealthSnapshot, now: float) -> None:
        if snap.monitoring_available:
            if self._unavailable is not None:
                logger.info("monitoring available again")
            self._unavailable = None
            return
        if self._unavailable is None:
            self._unavailable = _Episode(now)
        episode = self._unavailable
        if episode.alerted or now - episode.since < self._after_s:
            return
        episode.alerted = True
        offline = sorted(c.slot for c in snap.cameras if c.status is not CameraStatus.ONLINE)
        self._raise(
            "monitoring",
            LocalAlertPayload(
                kind=LocalAlertKind.MONITORING_UNAVAILABLE,
                detail={
                    "engine_status": snap.engine_status.value,
                    "cameras_offline": list(offline),
                    "unavailable_s": round(now - episode.since),
                },
            ),
            now,
        )

    def _raise(self, key: str, payload: LocalAlertPayload, now: float) -> None:
        last = self._last_alert.get(key)
        if last is not None and now - last < self._min_interval_s:
            self.alerts_suppressed += 1
            logger.info("%s alert suppressed (rate limit)", payload.kind.value)
            return
        try:
            self._on_alert(payload)
        except Exception as exc:
            logger.error("%s alert not queued (%s)", payload.kind.value, type(exc).__name__)
            return
        self._last_alert[key] = now
        self.alerts_raised += 1
        logger.warning("%s alert queued", payload.kind.value)
