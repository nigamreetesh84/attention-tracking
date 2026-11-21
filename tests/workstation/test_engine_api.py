"""Engine-facing value types: validation, wire conversion and burst invariants."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from ag_shared.api import CameraHeartbeat, CameraStateSnapshot
from ag_shared.health import CameraStatus, EngineStatus
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.engine_api import (
    BurstKind,
    CameraHealthSummary,
    CameraState,
    EngineHealth,
    EvidenceBurst,
    EvidenceFrame,
    FrameRole,
    StateBucket,
    StateEpisode,
    StationState,
)
from ag_workstation.fakes import fake_jpeg

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
EVENT = UUID("0193a0b0-0000-7000-8000-0000000000e1")
A = AttentionState


def _state(**kw: object) -> StationState:
    args: dict[str, object] = {
        "state": A.DROWSY,
        "confidence": 0.8,
        "low_confidence": False,
        "at": T0,
        "ts": 10.0,
        "engine_version": "1",
        "config_version": 3,
    }
    args.update(kw)
    return StationState(**args)  # type: ignore[arg-type]  # test helper over keyword args


def test_station_state_outcome_and_unknown() -> None:
    assert _state().outcome is ScanOutcome.WARNED
    assert _state(state=A.ATTENTIVE).outcome is ScanOutcome.ATTENTIVE
    unknown = StationState.unknown(T0, 1.0, engine_version="1", config_version=2)
    assert unknown.state is A.UNKNOWN
    assert unknown.confidence is None
    assert unknown.outcome is ScanOutcome.UNMONITORED


def test_station_state_signals_are_frozen_copies() -> None:
    signals = {"perclos": 0.4}
    state = _state(signals=signals)
    signals["perclos"] = 0.0
    assert state.signals["perclos"] == 0.4
    with pytest.raises(TypeError):
        state.signals["perclos"] = 1.0  # type: ignore[index]  # proving immutability


@pytest.mark.parametrize(
    "bad",
    [
        {"confidence": 1.2},
        {"at": datetime(2026, 11, 20)},
        {"at": T0.astimezone(timezone(timedelta(hours=5)))},
        {"config_version": -1},
    ],
)
def test_station_state_validation(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"UTC|0-1|negative"):
        _state(**bad)


def test_camera_state_to_wire() -> None:
    cam = CameraState(2, CameraStatus.ONLINE, A.DISTRACTED, 0.7, True)
    assert cam.to_snapshot() == CameraStateSnapshot(
        slot=2, status=CameraStatus.ONLINE, state=A.DISTRACTED, quality=0.7, usable=True
    )
    with pytest.raises(ValueError, match="slot"):
        CameraState(5, CameraStatus.ONLINE, A.ATTENTIVE)
    with pytest.raises(ValueError, match="quality"):
        CameraState(1, CameraStatus.ONLINE, A.ATTENTIVE, quality=-0.1)


def test_health_summary_to_wire_and_monitoring_available() -> None:
    online = CameraHealthSummary(1, CameraStatus.ONLINE, 12.0, 0.9)
    offline = CameraHealthSummary(2, CameraStatus.OFFLINE)
    assert online.to_heartbeat() == CameraHeartbeat(
        slot=1, status=CameraStatus.ONLINE, fps=12.0, quality=0.9
    )
    assert EngineHealth(EngineStatus.DEGRADED, (online, offline)).monitoring_available
    assert not EngineHealth(EngineStatus.RUNNING, (offline,)).monitoring_available
    assert not EngineHealth(EngineStatus.DOWN, (online,)).monitoring_available
    with pytest.raises(ValueError, match="fps"):
        CameraHealthSummary(1, CameraStatus.ONLINE, fps=-1)
    with pytest.raises(ValueError, match="slot"):
        CameraHealthSummary(0, CameraStatus.ONLINE)
    with pytest.raises(ValueError, match="quality"):
        CameraHealthSummary(1, CameraStatus.ONLINE, quality=2)


def test_episode_and_bucket_validation() -> None:
    ep = StateEpisode(EVENT, A.SLEEPING, T0, 0.9, False, "1", 3, ended_at=T0)
    assert ep.ended_at == T0
    with pytest.raises(ValueError, match="before"):
        StateEpisode(EVENT, A.SLEEPING, T0, 0.9, False, "1", 3, ended_at=T0 - timedelta(1))
    bucket = StateBucket(T0, {A.ATTENTIVE: 45.0, A.DROWSY: 15.0})
    assert sum(bucket.seconds.values()) == 60
    with pytest.raises(ValueError, match="minute"):
        StateBucket(T0 + timedelta(seconds=1), {})
    with pytest.raises(ValueError, match="0-60"):
        StateBucket(T0, {A.ATTENTIVE: 61.0})
    with pytest.raises(ValueError, match="0-60"):
        StateBucket(T0, {A.ATTENTIVE: -1.0})


def _frame(
    slot: int = 1, offset: int = 0, seq: int = 1, role: FrameRole = FrameRole.KEY
) -> EvidenceFrame:
    return EvidenceFrame(
        slot, T0 + timedelta(milliseconds=offset), offset, seq, role, fake_jpeg("x")
    )


def test_evidence_frame_validation_and_hash() -> None:
    frame = _frame()
    assert len(frame.sha256) == 64
    assert frame.size_bytes == len(fake_jpeg("x"))
    assert "jpeg" not in repr(frame)
    with pytest.raises(ValueError, match="pre-frame"):
        _frame(offset=0, role=FrameRole.PRE)
    with pytest.raises(ValueError, match="post-frame"):
        _frame(offset=-500, role=FrameRole.POST)
    with pytest.raises(ValueError, match="JPEG"):
        EvidenceFrame(1, T0, 0, 1, FrameRole.KEY, b"PNG")
    with pytest.raises(ValueError, match="slot"):
        _frame(slot=9)
    with pytest.raises(ValueError, match="seq"):
        _frame(seq=-1)


def test_burst_orders_frames_and_counts_missing() -> None:
    frames = (
        _frame(1, 500, 3, FrameRole.POST),
        _frame(2, 0, 7),
        _frame(1, -500, 1, FrameRole.PRE),
        _frame(1, 0, 2),
    )
    burst = EvidenceBurst(EVENT, BurstKind.SCAN_WARNING, T0, 10.0, frames, expected_frames=6)
    assert [(f.camera_slot, f.offset_ms) for f in burst.frames] == [
        (1, -500),
        (1, 0),
        (1, 500),
        (2, 0),
    ]
    assert burst.missing_frames == 2
    assert not burst.complete
    with pytest.raises(ValueError, match="twice"):
        EvidenceBurst(EVENT, BurstKind.SCAN_WARNING, T0, 0.0, (_frame(), _frame()), 2)
    with pytest.raises(ValueError, match="more frames"):
        EvidenceBurst(EVENT, BurstKind.EPISODE_KEYFRAME, T0, 0.0, frames, 1)


def test_burst_missing_labels_match_the_missing_count() -> None:
    # 2.9-pre: the single burst type also carries the assembler's labels.
    key = (_frame(1, 0, 2),)
    burst = EvidenceBurst(
        EVENT, BurstKind.SCAN_WARNING, T0, 1.0, key, 3, missing=("pre", "secondary_key:2")
    )
    assert burst.missing == ("pre", "secondary_key:2")
    assert burst.missing_frames == 2
    with pytest.raises(ValueError, match="missing labels"):
        EvidenceBurst(EVENT, BurstKind.SCAN_WARNING, T0, 1.0, key, 3, missing=("pre",))
    assert EvidenceBurst(EVENT, BurstKind.SCAN_WARNING, T0, 1.0, key, 3).missing == ()
