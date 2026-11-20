"""Per-camera state estimator (`docs/detection-engine.md` sections 3 and 5).

Turns one camera's `SignalFrame` stream into a `CameraCandidate`: a state,
a confidence and whether the camera is usable. Pure Python, driven only by
frame timestamps, so replays are deterministic.

Candidate rules, first match wins:

1. Unknown: the frame quality has been below `quality_min` for longer than
   `unknown_seconds` (or there are no recent frames at all).
2. Absent: good-quality frames without a person for longer than
   `absent_seconds`. A visible upper body (profiles with posture) counts as a
   person, so a worker slumped over the bench is not Absent.
3. Sleeping: any of the profile's sleeping rules holds (see `SleepingRule`).
   When the eye signals are unreadable (glare, angle), a profile that uses
   them falls back to the head-drop rule with lower confidence.
4. Drowsy: PERCLOS above its threshold, enough yawns in the yawn window, or
   repeated head nods.
5. Distracted: head yaw or pitch off-axis for longer than `distracted_seconds`.
6. Attentive.

Between a fault and its timer (a few bad frames, a face that has just left)
the previous candidate is held rather than reporting Attentive; the timers
then settle it. All timers bridge gaps up to `gap_tolerance_s`.
"""

from dataclasses import dataclass

from ag_shared.ppe import PpeProfile, SleepingRule
from ag_shared.signals import SignalFrame, SignalName
from ag_shared.states import AttentionState
from ag_shared.thresholds import Thresholds

from ag_workstation.engine.timers import ConditionTimer, EdgeCounter, ShareWindow
from ag_workstation.engine.types import CameraCandidate

S = SignalName
A = AttentionState


@dataclass(frozen=True, slots=True)
class EstimatorParams:
    """Tuning constants below the scope thresholds; to be tuned in the pilot."""

    nods_for_drowsy: int = 3
    nod_window_s: float = 60.0
    """Repeated head nods: this many nods inside this window mean Drowsy."""
    motion_still_max: float = 0.05
    """Motion energy (body scales per second) at or below which the worker is still."""
    posture_slumped_min: float = 0.7
    """Posture slump (0 upright, 1 head at shoulder level) from which the head
    counts as dropped when the face is not readable."""
    eye_valid_window_s: float = 2.0
    eye_valid_min_share: float = 0.5
    """Below this share of measurable eye frames the eye rules are not trusted."""
    fallback_confidence: float = 0.6
    """Confidence factor when Sleeping comes from the head-pose fallback."""
    no_eye_confidence: float = 0.8
    """Confidence factor for Sleeping in profiles without eye signals."""


def _any_true(*values: bool | None) -> bool | None:
    """True if any value is True, None if all are None, else False."""
    if any(v is True for v in values):
        return True
    if all(v is None for v in values):
        return None
    return False


def _both(a: bool | None, b: bool | None) -> bool | None:
    if a is None or b is None:
        return None
    return a and b


class CameraEstimator:
    """Candidate state for one camera. Call `update` once per frame in order."""

    def __init__(
        self,
        slot: int,
        profile: PpeProfile,
        thresholds: Thresholds,
        params: EstimatorParams | None = None,
    ) -> None:
        self._slot = slot
        self._profile = profile
        self._t = thresholds
        self._p = params if params is not None else EstimatorParams()
        self._unknown = ConditionTimer()
        self._absent = ConditionTimer()
        self._eyes_closed = ConditionTimer()
        self._eyes_closed_drop = ConditionTimer()
        self._drop_still = ConditionTimer()
        self._drop_fallback = ConditionTimer()
        self._off_axis = ConditionTimer()
        self._yawns = EdgeCounter()
        self._nods = EdgeCounter()
        self._eye_share = ShareWindow()
        self._last: CameraCandidate | None = None
        self._last_ts: float | None = None

    @property
    def slot(self) -> int:
        """The camera this estimator belongs to."""
        return self._slot

    @property
    def last(self) -> CameraCandidate | None:
        """The latest candidate, if any frame was seen."""
        return self._last

    def configure(
        self, *, profile: PpeProfile | None = None, thresholds: Thresholds | None = None
    ) -> None:
        """Apply a config change between frames; running timers are kept."""
        if profile is not None:
            self._profile = profile
        if thresholds is not None:
            self._t = thresholds

    def reset(self) -> None:
        """Forget all timers (a new worker session, or a replay that restarts)."""
        for timer in (
            self._unknown,
            self._absent,
            self._eyes_closed,
            self._eyes_closed_drop,
            self._drop_still,
            self._drop_fallback,
            self._off_axis,
        ):
            timer.reset()
        self._yawns.reset()
        self._nods.reset()
        self._eye_share.reset()
        self._last = None
        self._last_ts = None

    def candidate_at(self, now: float) -> CameraCandidate:
        """The candidate at `now`: Unknown when no frame arrived recently.

        Used by the runtime for cameras that stopped delivering frames.
        """
        if self._last is None or self._last_ts is None:
            return CameraCandidate.unknown(self._slot, now)
        if now - self._last_ts > self._t.unknown_seconds:
            return CameraCandidate.unknown(self._slot, now)
        return self._last

    def update(self, frame: SignalFrame) -> CameraCandidate:
        """Add one frame and return the candidate after it."""
        if frame.camera_slot != self._slot:
            raise ValueError("signal frame belongs to another camera")
        ts = frame.ts
        if self._last_ts is not None and ts < self._last_ts:
            self.reset()
        self._last_ts = ts
        candidate = self._evaluate(frame)
        self._last = candidate
        return candidate

    # --- rules ---

    def _evaluate(self, f: SignalFrame) -> CameraCandidate:
        t, p, prof = self._t, self._p, self._profile
        gap = t.gap_tolerance_s
        ts = f.ts

        quality = f.value(S.QUALITY)
        good = quality is not None and quality >= t.quality_min
        face = f.flag(S.FACE_PRESENT) is True
        face_seen = face and good

        posture = f.value(S.POSTURE) if prof.is_enabled(S.POSTURE) else None
        body_seen = good and posture is not None
        person: bool | None = (face_seen or body_seen) if good else None

        self._unknown.update(ts, not good, gap)
        self._absent.update(ts, None if person is None else not person, gap)

        # Eyes. Readings are only trusted on a usable face.
        closed = f.flag(S.EYES_CLOSED) if face_seen else None
        self._eye_share.update(ts, closed, p.eye_valid_window_s)

        head_drop = f.flag(S.HEAD_DROPPED) if face_seen else None
        body_drop = None if posture is None or not good else posture >= p.posture_slumped_min
        dropped = _any_true(head_drop, body_drop)
        motion = f.value(S.MOTION) if prof.is_enabled(S.MOTION) and good else None
        still = None if motion is None else motion <= p.motion_still_max

        self._eyes_closed.update(ts, closed, gap)
        self._eyes_closed_drop.update(ts, _both(closed, dropped), gap)
        self._drop_still.update(ts, _both(dropped, still), gap)
        # Fallback head rule: stillness when motion is measured, else drop alone.
        self._drop_fallback.update(ts, dropped if motion is None else _both(dropped, still), gap)

        yaw = f.value(S.HEAD_YAW) if face_seen else None
        pitch = f.value(S.HEAD_PITCH) if face_seen else None
        off_axis: bool | None = None
        if yaw is not None or pitch is not None:
            off_axis = (yaw is not None and abs(yaw) > t.distracted_yaw_deg) or (
                pitch is not None and abs(pitch) > t.distracted_pitch_deg
            )
        self._off_axis.update(ts, off_axis, gap)

        yawn = f.flag(S.YAWN) if face_seen else None
        yawns = self._yawns.update(ts, yawn, t.yawn_window_s)
        nod = f.flag(S.HEAD_NOD) if face_seen else None
        nods = self._nods.update(ts, nod, p.nod_window_s)

        q = quality if quality is not None else 0.0

        def make(
            state: AttentionState, confidence: float, fallback: bool = False
        ) -> CameraCandidate:
            return CameraCandidate(
                slot=self._slot,
                ts=ts,
                state=state,
                confidence=max(0.0, min(1.0, confidence)),
                quality=quality,
                usable=good and state is not A.UNKNOWN,
                face_seen=face_seen,
                fallback=fallback,
                frame=f,
            )

        # 1. Unknown.
        if not good:
            if self._unknown.exceeds(t.unknown_seconds) or self._last is None:
                return make(A.UNKNOWN, 0.0)
            return self._hold(f, quality, good=False, face_seen=False)

        # 2. Absent.
        if self._absent.exceeds(t.absent_seconds):
            return make(A.ABSENT, q)

        # 3. Sleeping.
        sleeping = self._sleeping(q)
        if sleeping is not None:
            confidence, fallback = sleeping
            return make(A.SLEEPING, confidence, fallback)

        # 4. Drowsy.
        drowsy_weights: list[float] = []
        perclos = f.value(S.PERCLOS) if prof.is_enabled(S.PERCLOS) else None
        if perclos is not None and perclos > t.perclos_threshold:
            drowsy_weights.append(prof.weight(S.PERCLOS))
        if prof.is_enabled(S.YAWN) and yawns >= t.yawns_for_drowsy:
            drowsy_weights.append(prof.weight(S.YAWN))
        if prof.is_enabled(S.HEAD_NOD) and nods >= p.nods_for_drowsy:
            drowsy_weights.append(prof.weight(S.HEAD_NOD))
        if drowsy_weights:
            return make(A.DROWSY, q * max(drowsy_weights))

        # 5. Distracted.
        if self._off_axis.exceeds(t.distracted_seconds):
            weight = max(prof.weight(S.HEAD_YAW), prof.weight(S.HEAD_PITCH))
            return make(A.DISTRACTED, q * weight)

        # Nobody visible yet, but not long enough for Absent: keep the last answer.
        if not person and self._last is not None:
            return self._hold(f, quality, good=True, face_seen=False)

        # 6. Attentive.
        return make(A.ATTENTIVE, q)

    def _sleeping(self, q: float) -> tuple[float, bool] | None:
        """(confidence, fallback) when a sleeping rule holds, else None."""
        t, p, prof = self._t, self._p, self._profile
        rules = prof.sleeping_rules
        eye_rules = {SleepingRule.EYES_CLOSED, SleepingRule.EYES_CLOSED_WITH_HEAD_DROP}
        uses_eyes = bool(eye_rules & set(rules))
        eye_weight = prof.weight(S.EYES_CLOSED)
        drop_weight = prof.weight(S.HEAD_DROPPED)

        if SleepingRule.EYES_CLOSED in rules and self._eyes_closed.exceeds(
            t.sleeping_eyes_closed_s
        ):
            return q * eye_weight, False
        if SleepingRule.EYES_CLOSED_WITH_HEAD_DROP in rules and self._eyes_closed_drop.exceeds(
            t.sleeping_eyes_closed_s
        ):
            return q * max(eye_weight, drop_weight), False
        if SleepingRule.HEAD_DROPPED_STILL in rules and self._drop_still.exceeds(
            t.sleeping_head_drop_s
        ):
            factor = 1.0 if uses_eyes else p.no_eye_confidence
            return q * drop_weight * factor, False
        eyes_unreadable = (
            len(self._eye_share) > 0 and self._eye_share.valid_share() < p.eye_valid_min_share
        )
        if (
            uses_eyes
            and eyes_unreadable
            and prof.is_enabled(S.HEAD_DROPPED)
            and self._drop_fallback.exceeds(t.sleeping_head_drop_s)
        ):
            return q * drop_weight * p.fallback_confidence, True
        return None

    def _hold(
        self, f: SignalFrame, quality: float | None, *, good: bool, face_seen: bool
    ) -> CameraCandidate:
        """Repeat the previous candidate's state on a frame that cannot decide."""
        previous = self._last
        assert previous is not None  # noqa: S101 - callers check this
        return CameraCandidate(
            slot=self._slot,
            ts=f.ts,
            state=previous.state,
            confidence=previous.confidence,
            quality=quality,
            usable=good and previous.state is not A.UNKNOWN,
            face_seen=face_seen,
            fallback=previous.fallback,
            frame=f,
        )
