"""`StationPipeline`: landmarks to station state without threads."""

from datetime import time, timedelta
from uuid import UUID

from ag_shared.config import EngineConfig, Shift, ShiftBreak, ShiftThresholds
from ag_shared.ppe import PpeProfileKind
from ag_shared.states import AttentionState
from ag_shared.thresholds import Thresholds
from ag_workstation.engine.fakes import (
    T0,
    camera,
    engine_config,
    face_result,
    frame,
    no_face_result,
)
from ag_workstation.engine.pipeline import StationPipeline, needs_pose
from ag_workstation.engine.synthetic import ATTENTIVE, EYES_CLOSED, make_frame, trace

A = AttentionState
SHIFT = UUID("00000000-0000-7000-8000-0000000000aa")


def _feed_landmarks(pipe: StationPipeline, seconds: float, start: float, *, ear: float) -> float:
    """Feed both cameras at 10 fps from `start`; tick every frame. Returns the end time."""
    n = round(seconds * 10)
    for i in range(n):
        ts = round(start + i / 10, 6)
        for slot in (1, 2):
            pipe.process_landmarks(face_result(frame(slot, i + 1, ts), ear=ear))
        pipe.tick(ts, T0 + timedelta(seconds=ts))
    return round(start + n / 10, 6)


def test_landmarks_to_sleeping_end_to_end() -> None:
    pipe = StationPipeline(engine_config())
    t = _feed_landmarks(pipe, 6.0, 0.0, ear=0.30)  # warm up the open-eye baseline
    assert pipe.status().state is A.ATTENTIVE
    _feed_landmarks(pipe, 5.0, t, ear=0.05)
    assert pipe.status().state is A.SLEEPING
    assert pipe.open_episode is not None
    assert pipe.scan_decision().state is A.SLEEPING
    closed, buckets = pipe.flush()
    assert [e.state for e in closed] == [A.SLEEPING]
    assert sum(sum(b.seconds.values()) for b in buckets) > 0


def test_signal_traces_and_fusion_presence_rule() -> None:
    pipe = StationPipeline(engine_config())
    out = None
    for f in trace([(ATTENTIVE, 12.0)], slot=2):
        pipe.process_signals(f)
        pipe.process_signals(make_frame(ATTENTIVE.with_(face=False), f.ts, slot=1))
        out = pipe.tick(f.ts, T0 + timedelta(seconds=f.ts))
    assert out is not None
    assert out.status.state is A.ATTENTIVE  # camera 2 sees the face


def test_disabled_and_unknown_cameras() -> None:
    config = engine_config((camera(1, primary=True, enabled=False), camera(2)))
    pipe = StationPipeline(config)
    assert pipe.process_landmarks(face_result(frame(1, 1, 0.0))) is None
    assert pipe.process_signals(make_frame(ATTENTIVE, 0.0, slot=1)) is None
    assert pipe.process_signals(make_frame(ATTENTIVE, 0.0, slot=3)) is None
    pipe.process_signals(make_frame(ATTENTIVE, 0.0, slot=2))
    out = pipe.tick(0.0, T0)
    assert out.status.state is A.ATTENTIVE
    assert [c.state for c in pipe.candidates(0.0)] == [A.UNKNOWN, A.ATTENTIVE]


def test_all_cameras_offline_is_monitoring_unavailable() -> None:
    pipe = StationPipeline(engine_config())
    out = pipe.tick(0.0, T0, offline_slots={1, 2})
    assert out.status.state is A.UNKNOWN
    assert out.status.monitoring_unavailable
    assert out.fused.monitoring_unavailable


def test_degraded_camera_is_reported_after_30_s() -> None:
    pipe = StationPipeline(engine_config())
    out = None
    for f in trace([(ATTENTIVE, 31.0)]):
        pipe.process_signals(f)
        out = pipe.tick(f.ts, T0 + timedelta(seconds=f.ts))
    assert out is not None
    assert out.status.degraded_slots == (2,)
    assert out.status.state is A.ATTENTIVE


def _shift_config() -> EngineConfig:
    shift = Shift(
        id=SHIFT,
        name="Day",
        start_time=time(9, 0),
        end_time=time(18, 0),
        days_of_week=(0, 1, 2, 3, 4, 5, 6),
        breaks=(ShiftBreak(name="Tea", start_time=time(10, 0), end_time=time(10, 15)),),
    )
    return engine_config(
        cameras=1,
        shifts=(shift,),
        shift_thresholds=(
            ShiftThresholds(shift_id=SHIFT, thresholds=Thresholds(sleeping_eyes_closed_s=1.0)),
        ),
        timezone="Asia/Kolkata",
    )


def test_shift_thresholds_follow_the_calendar() -> None:
    pipe = StationPipeline(engine_config(cameras=1, timezone="Asia/Kolkata"))
    pipe.apply_config(_shift_config())
    # T0 is 10:00 India time: inside the shift and inside the Tea break.
    out = pipe.tick(0.0, T0)
    assert out.calendar.shift_id == SHIFT
    assert out.calendar.on_break
    assert out.status.state is A.ON_BREAK
    assert pipe.thresholds.sleeping_eyes_closed_s == 1.0
    # After the break the shift thresholds still apply: 1.5 s closed is Sleeping.
    start = 16 * 60.0
    for f in trace([(EYES_CLOSED, 1.5)], start=start, slot=1):
        pipe.process_signals(f)
        out = pipe.tick(f.ts, T0 + timedelta(seconds=f.ts))
    assert out.status.state is A.SLEEPING
    # Outside the shift the station defaults apply again.
    later = T0 + timedelta(hours=9)
    pipe.tick(start + 9 * 3600, later)
    assert pipe.thresholds.sleeping_eyes_closed_s == 4.0


def test_apply_config_keeps_running_cameras_and_drops_removed_ones() -> None:
    pipe = StationPipeline(engine_config())
    for f in trace([(EYES_CLOSED, 3.0)]):
        pipe.process_signals(f)
    pipe.apply_config(engine_config(cameras=1))
    # Camera 1 kept its 3 s of closed eyes.
    for f in trace([(EYES_CLOSED, 1.2)], start=3.0):
        pipe.process_signals(f)
        pipe.tick(f.ts, T0 + timedelta(seconds=f.ts))
    assert pipe.status().state is A.SLEEPING
    assert [c.slot for c in pipe.candidates(4.2)] == [1]


def test_reset_session_forgets_timers() -> None:
    pipe = StationPipeline(engine_config(cameras=1))
    for f in trace([(EYES_CLOSED, 3.0)]):
        pipe.process_signals(f)
    pipe.reset_session()
    for f in trace([(EYES_CLOSED, 1.5)], start=3.0):
        pipe.process_signals(f)
        pipe.tick(f.ts, T0 + timedelta(seconds=f.ts))
    assert pipe.status().state is not A.SLEEPING


def test_no_face_landmarks_lead_to_absent() -> None:
    pipe = StationPipeline(engine_config(cameras=1))
    for i in range(115):
        ts = i / 10
        pipe.process_landmarks(no_face_result(frame(1, i + 1, ts)))
        pipe.tick(ts, T0 + timedelta(seconds=ts))
    assert pipe.status().state is A.ABSENT


def test_needs_pose() -> None:
    assert not needs_pose(engine_config(kind=PpeProfileKind.OPEN_FACE))
    assert needs_pose(engine_config(kind=PpeProfileKind.SUNGLASSES))
    assert StationPipeline(engine_config(kind=PpeProfileKind.HELMET_SHIELD)).with_pose
