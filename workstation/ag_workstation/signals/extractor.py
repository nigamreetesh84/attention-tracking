"""`SignalExtractor`: one camera's landmarks to one `SignalFrame` per frame.

Implements `docs/detection-engine.md` section 2. The extractor is driven by
the frame timestamps alone (no clock), reads landmark data only (no pixels)
and produces numbers and flags only: nothing in a `SignalFrame` can identify
a person.

Validity rules:

- `face_present` is always valid. `quality` is valid whenever something
  could be measured (the face, or the frame when there is no face).
- A face whose quality is below `quality_min` is present but unusable: every
  face signal of that frame is invalid.
- Signals the PPE profile disables are not computed and are absent.
- Posture and motion come from the pose model and do not need a face (a
  worker slumped over the bench has none), only a usable frame.
"""

from ag_shared.config import CameraConfig
from ag_shared.ppe import PpeProfile
from ag_shared.signals import SignalFrame, SignalName, SignalReading
from ag_shared.thresholds import Thresholds

from ag_workstation.landmarks.types import (
    FaceObservation,
    LandmarkResult,
    Point,
    PoseObservation,
)
from ag_workstation.signals.geometry import (
    eye_aspect_ratio,
    mouth_aspect_ratio,
    pose_visible,
    posture_slump,
    shoulder_width,
)
from ag_workstation.signals.params import ExtractionParams
from ag_workstation.signals.quality import face_quality, frame_quality, stability_score
from ag_workstation.signals.roi import select_face, select_pose
from ag_workstation.signals.windows import (
    BlinkRateCounter,
    EarBaseline,
    MotionTracker,
    NodDetector,
    PerclosWindow,
    YawnDetector,
)

_EPS = 1e-9


def _flag(value: bool | None) -> SignalReading:
    return SignalReading.invalid() if value is None else SignalReading.flag(value)


def _number(value: float | None) -> SignalReading:
    return SignalReading.invalid() if value is None else SignalReading(value=value)


class SignalExtractor:
    """Per-camera signal extraction with the windowed state it needs.

    One instance per camera; call `extract` once per frame in timestamp
    order. If a timestamp is earlier than the previous one (a replay that
    starts over) all state is reset first.
    """

    def __init__(
        self,
        camera: CameraConfig,
        profile: PpeProfile,
        thresholds: Thresholds,
        params: ExtractionParams | None = None,
    ) -> None:
        self._camera = camera
        self._profile = profile
        self._thresholds = thresholds
        self._p = params if params is not None else ExtractionParams()
        p = self._p
        self._baseline = EarBaseline(p)
        self._perclos = PerclosWindow(
            min_valid_share=p.perclos_min_valid_share, min_fill=p.perclos_min_fill
        )
        self._blinks = BlinkRateCounter(
            window_s=p.blink_window_s,
            min_span_s=p.blink_min_span_s,
            min_valid_share=p.blink_min_valid_share,
        )
        self._yawn = YawnDetector(min_s=p.yawn_min_s)
        self._nod = NodDetector(
            drop_deg=p.nod_drop_deg,
            recover_deg=p.nod_recover_deg,
            max_s=p.nod_max_s,
            ref_window_s=p.nod_ref_window_s,
        )
        self._motion = MotionTracker(
            window_s=p.motion_window_s, max_gap_s=p.max_frame_gap_s, min_fill=p.motion_min_fill
        )
        self._last_ts: float | None = None
        self._last_width: tuple[float, float] | None = None

    @property
    def camera_slot(self) -> int:
        """The camera this extractor belongs to."""
        return self._camera.slot

    def configure(
        self,
        *,
        camera: CameraConfig | None = None,
        profile: PpeProfile | None = None,
        thresholds: Thresholds | None = None,
    ) -> None:
        """Apply a config change between frames; the windows are kept."""
        if camera is not None:
            if camera.slot != self._camera.slot:
                raise ValueError("an extractor cannot move to another camera slot")
            self._camera = camera
        if profile is not None:
            self._profile = profile
        if thresholds is not None:
            self._thresholds = thresholds

    def reset_session(self) -> None:
        """Forget everything measured so far (a new worker session starts).

        The open-eye baseline and all windows belong to one person.
        """
        self._baseline.reset()
        self._perclos.reset()
        self._blinks.reset()
        self._yawn.reset()
        self._nod.reset()
        self._motion.reset()
        self._last_ts = None
        self._last_width = None

    def extract(self, result: LandmarkResult) -> SignalFrame:
        """The signals of one frame."""
        if result.camera_slot != self._camera.slot:
            raise ValueError("landmark result belongs to another camera")
        ts = result.ts
        if self._last_ts is not None and ts < self._last_ts:
            self.reset_session()
        self._last_ts = ts

        enabled = self._profile.enabled_signals
        aspect = result.aspect
        roi = self._camera.roi
        quality_min = self._thresholds.quality_min

        face = select_face(result.faces, roi)
        frame_q = frame_quality(result.frame_stats, self._p)
        quality = frame_q if face is None else self._face_quality(ts, face, result)
        usable = (
            face if face is not None and quality is not None and quality >= quality_min else None
        )
        frame_usable = frame_q is None or frame_q >= quality_min

        signals: dict[SignalName, SignalReading] = {
            SignalName.FACE_PRESENT: SignalReading.flag(face is not None),
            SignalName.QUALITY: _number(quality),
        }
        if SignalName.EAR in enabled:
            self._eye_signals(signals, ts, usable, aspect)
        if SignalName.MAR in enabled:
            self._mouth_signals(signals, ts, usable, aspect)
        self._head_signals(signals, ts, usable)
        if SignalName.POSTURE in enabled or SignalName.MOTION in enabled:
            pose = None
            if frame_usable and result.poses:
                pose = select_pose(result.poses, roi, face)
            self._body_signals(signals, ts, usable, pose, aspect)

        return SignalFrame(
            camera_slot=self._camera.slot,
            ts=ts,
            signals={name: reading for name, reading in signals.items() if name in enabled},
        )

    # --- quality ---

    def _face_quality(self, ts: float, face: FaceObservation, result: LandmarkResult) -> float:
        stability = None
        if self._last_width is not None:
            previous_ts, previous_width = self._last_width
            if ts - previous_ts <= self._p.max_frame_gap_s + _EPS:
                stability = stability_score(previous_width, face.box.width, self._p)
        self._last_width = (ts, face.box.width)
        return face_quality(face, result.frame_stats, stability, self._p)

    # --- eyes ---

    def _eye_measurement(self, face: FaceObservation, aspect: float) -> tuple[float, bool] | None:
        """(mean raw EAR, blink blendshape says closed) over the readable eyes."""
        p = self._p
        ears: list[float] = []
        blinks: list[float] = []
        for eye, glare, blink in (
            (face.right_eye, face.glare_right, face.blink_right),
            (face.left_eye, face.glare_left, face.blink_left),
        ):
            if (
                self._profile.glare_tolerance
                and glare is not None
                and glare >= p.glare_max_fraction
            ):
                continue  # a reflection hides this eye: neither open nor closed
            ear = eye_aspect_ratio(eye, aspect)
            if ear is None:
                continue
            ears.append(ear)
            if blink is not None:
                blinks.append(blink)
        if not ears:
            return None
        blink_closed = (
            p.use_blink_blendshape
            and len(blinks) == len(ears)
            and sum(blinks) / len(blinks) >= p.blink_closed_score
        )
        return sum(ears) / len(ears), blink_closed

    def _eye_signals(
        self,
        signals: dict[SignalName, SignalReading],
        ts: float,
        face: FaceObservation | None,
        aspect: float,
    ) -> None:
        enabled = self._profile.enabled_signals
        measurement = None if face is None else self._eye_measurement(face, aspect)
        closed: bool | None = None
        if measurement is None:
            signals[SignalName.EAR] = SignalReading.invalid()
        else:
            raw_ear, blink_closed = measurement
            ratio = self._baseline.normalize(ts, raw_ear)
            closed = ratio < self._p.ear_closed_ratio or blink_closed
            signals[SignalName.EAR] = SignalReading(value=ratio)
        signals[SignalName.EYES_CLOSED] = _flag(closed)
        gap = self._thresholds.gap_tolerance_s
        if SignalName.PERCLOS in enabled:
            signals[SignalName.PERCLOS] = self._perclos.update(
                ts, closed, self._thresholds.perclos_window_s
            )
        if SignalName.BLINK_RATE in enabled:
            signals[SignalName.BLINK_RATE] = self._blinks.update(ts, closed, gap)

    # --- mouth ---

    def _mouth_signals(
        self,
        signals: dict[SignalName, SignalReading],
        ts: float,
        face: FaceObservation | None,
        aspect: float,
    ) -> None:
        mar = None if face is None else mouth_aspect_ratio(face.mouth, aspect)
        signals[SignalName.MAR] = _number(mar)
        if SignalName.YAWN in self._profile.enabled_signals:
            mouth_open = None if mar is None else mar > self._p.mar_yawn
            signals[SignalName.YAWN] = _flag(
                self._yawn.update(ts, mouth_open, self._thresholds.gap_tolerance_s)
            )

    # --- head ---

    def _head_signals(
        self, signals: dict[SignalName, SignalReading], ts: float, face: FaceObservation | None
    ) -> None:
        enabled = self._profile.enabled_signals
        pose = None if face is None else face.head_pose
        neutral = self._camera.calibration
        yaw = None if pose is None else pose.yaw_deg - neutral.neutral_yaw_deg
        pitch = None if pose is None else pose.pitch_deg - neutral.neutral_pitch_deg
        roll = None if pose is None else pose.roll_deg - neutral.neutral_roll_deg
        signals[SignalName.HEAD_YAW] = _number(yaw)
        signals[SignalName.HEAD_PITCH] = _number(pitch)
        signals[SignalName.HEAD_ROLL] = _number(roll)
        if SignalName.HEAD_NOD in enabled:
            signals[SignalName.HEAD_NOD] = _flag(
                self._nod.update(ts, pitch, self._thresholds.gap_tolerance_s)
            )
        signals[SignalName.HEAD_DROPPED] = _flag(
            None if pitch is None else pitch < -self._p.head_drop_deg
        )

    # --- body ---

    def _body_signals(
        self,
        signals: dict[SignalName, SignalReading],
        ts: float,
        face: FaceObservation | None,
        pose: PoseObservation | None,
        aspect: float,
    ) -> None:
        p = self._p
        enabled = self._profile.enabled_signals
        if SignalName.POSTURE in enabled:
            slump = None
            if pose is not None:
                slump = posture_slump(
                    pose,
                    aspect,
                    upright_ratio=p.posture_upright_ratio,
                    min_visibility=p.pose_min_visibility,
                )
            signals[SignalName.POSTURE] = _number(slump)
        if SignalName.MOTION in enabled:
            tracked: dict[str, tuple[Point, float]] = {}
            if face is not None:
                scale = face.box.width * aspect
                for index, anchor in enumerate(face.anchors):
                    tracked[f"face{index}"] = (Point(anchor.x * aspect, anchor.y), scale)
            if pose is not None and pose_visible(pose, p.pose_min_visibility):
                scale = shoulder_width(pose, aspect)
                for name, part in (
                    ("nose", pose.nose),
                    ("left_shoulder", pose.left_shoulder),
                    ("right_shoulder", pose.right_shoulder),
                ):
                    tracked[name] = (Point(part.x * aspect, part.y), scale)
            signals[SignalName.MOTION] = self._motion.update(ts, tracked)
