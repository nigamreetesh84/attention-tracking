"""Synthetic signal frames and traces for tests and CI replays.

These stand in for the camera, the landmark model and the extractor: a test
describes what a worker is doing and gets the `SignalFrame`s the extractor
would have produced. No images are involved.
"""

from collections.abc import Iterator
from dataclasses import dataclass, replace

from ag_shared.ppe import PpeProfile
from ag_shared.signals import SignalFrame, SignalName, SignalReading

S = SignalName


@dataclass(frozen=True, slots=True)
class Look:
    """What one camera sees in one frame, in signal terms.

    `None` for a numeric field means "not measured on this frame".
    """

    quality: float | None = 0.9
    face: bool = True
    eyes_closed: bool | None = False
    ear: float | None = 1.0
    perclos: float | None = 0.0
    blink_rate: float | None = 15.0
    mar: float | None = 0.2
    yawn: bool | None = False
    yaw: float | None = 0.0
    pitch: float | None = 0.0
    roll: float | None = 0.0
    nod: bool | None = False
    head_dropped: bool | None = False
    posture: float | None = 0.0
    motion: float | None = 0.3

    def with_(self, **changes: object) -> "Look":
        """A copy with some fields changed."""
        return replace(self, **changes)  # type: ignore[arg-type]  # dataclass kwargs


ATTENTIVE = Look()
EYES_CLOSED = Look(eyes_closed=True, ear=0.3)
LOOKING_AWAY = Look(yaw=45.0)
HEAD_DOWN_STILL = Look(pitch=-35.0, head_dropped=True, posture=0.9, motion=0.0)
NO_FACE = Look(
    face=False,
    eyes_closed=None,
    ear=None,
    perclos=None,
    blink_rate=None,
    mar=None,
    yawn=None,
    yaw=None,
    pitch=None,
    roll=None,
    nod=None,
    head_dropped=None,
    posture=None,
    motion=None,
)
DARK = NO_FACE.with_(quality=0.1)


def _number(value: float | None) -> SignalReading:
    return SignalReading.invalid() if value is None else SignalReading(value=value)


def _flag(value: bool | None) -> SignalReading:
    return SignalReading.invalid() if value is None else SignalReading.flag(value)


def make_frame(
    look: Look, ts: float, *, slot: int = 1, profile: PpeProfile | None = None
) -> SignalFrame:
    """The `SignalFrame` for `look` at `ts`, limited to the profile's signals."""
    signals = {
        S.FACE_PRESENT: SignalReading.flag(look.face),
        S.QUALITY: _number(look.quality),
        S.EAR: _number(look.ear),
        S.EYES_CLOSED: _flag(look.eyes_closed),
        S.PERCLOS: _number(look.perclos),
        S.BLINK_RATE: _number(look.blink_rate),
        S.MAR: _number(look.mar),
        S.YAWN: _flag(look.yawn),
        S.HEAD_YAW: _number(look.yaw),
        S.HEAD_PITCH: _number(look.pitch),
        S.HEAD_ROLL: _number(look.roll),
        S.HEAD_NOD: _flag(look.nod),
        S.HEAD_DROPPED: _flag(look.head_dropped),
        S.POSTURE: _number(look.posture),
        S.MOTION: _number(look.motion),
    }
    if profile is not None:
        signals = {k: v for k, v in signals.items() if profile.is_enabled(k)}
    return SignalFrame(camera_slot=slot, ts=ts, signals=signals)


def trace(
    segments: list[tuple[Look, float]],
    *,
    fps: float = 10.0,
    start: float = 0.0,
    slot: int = 1,
    profile: PpeProfile | None = None,
) -> Iterator[SignalFrame]:
    """Frames for consecutive (look, seconds) segments at `fps`.

    Timestamps are computed from a frame index so they do not accumulate
    float error.
    """
    index = 0
    seg_start = start
    for look, seconds in segments:
        seg_end = seg_start + seconds
        while True:
            ts = round(start + index / fps, 9)
            if ts >= seg_end - 1e-9:
                break
            yield make_frame(look, ts, slot=slot, profile=profile)
            index += 1
        seg_start = seg_end
