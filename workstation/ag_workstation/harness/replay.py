"""Replay recordings through the full pipeline with frame time as the clock.

Video files go through the landmark provider, extraction, estimation, fusion
and the state machine; signal traces skip the model and start at the
estimator. Recordings that share a `session` key are replayed together as
the cameras of one station, so fusion is tested on real footage.

Replays are deterministic and run faster than real time: nothing sleeps and
the only clock is the frame timestamp.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from ag_shared.config import CameraConfig, CameraKind, CameraRole, EngineConfig
from ag_shared.ppe import builtin_profile
from ag_shared.signals import SignalFrame
from ag_shared.states import AttentionState
from ag_shared.thresholds import Thresholds

from ag_workstation.capture.types import Frame, Image
from ag_workstation.engine.pipeline import StationPipeline
from ag_workstation.harness.labels import LabelFile
from ag_workstation.harness.metrics import Segment, timeline_from_samples
from ag_workstation.harness.traces import iter_trace
from ag_workstation.landmarks.types import LandmarkError, LandmarkProvider

REPLAY_EPOCH = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
"""Wall time of replay second 0 (no shifts are configured, so no breaks)."""

SignalListener = Callable[[SignalFrame], None]

TICK_S = 0.1
"""Fusion tick in frame time, the same as the live engine's tick interval."""


class VideoReader(Protocol):
    """Reads the frames of one video file in order."""

    def __call__(self, path: Path) -> Iterator[Image]:
        """Yield every frame of `path`."""
        ...


def opencv_reader(path: Path) -> Iterator[Image]:
    """Read a video file with OpenCV (the capture layer owns OpenCV)."""
    from ag_workstation.capture.opencv_source import FileSource
    from ag_workstation.capture.types import EndOfStream

    source = FileSource(path, realtime=False)
    source.open()
    try:
        while True:
            try:
                image = source.read()
            except EndOfStream:
                return
            if image is not None:
                yield image
    finally:
        source.close()


@dataclass(frozen=True, slots=True)
class Recording:
    """One recording of a session: its label and media file."""

    label: LabelFile
    path: Path


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """The predicted state timeline of one session."""

    name: str
    predicted: list[Segment]
    duration_s: float


def session_config(
    recordings: Sequence[Recording], thresholds: Thresholds | None = None
) -> EngineConfig:
    """An `EngineConfig` for the cameras of one session."""
    labels = [r.label for r in recordings]
    kinds = {lab.ppe_profile for lab in labels}
    if len(kinds) != 1:
        raise ValueError("recordings of one session must share a PPE profile")
    slots = [lab.slot for lab in labels]
    if len(set(slots)) != len(slots):
        raise ValueError("recordings of one session must use different camera slots")
    primary = min(slots) if CameraRole.PRIMARY not in {lab.camera_role for lab in labels} else None
    cameras = tuple(
        CameraConfig(
            slot=lab.slot,
            role=(
                CameraRole.PRIMARY
                if lab.camera_role is CameraRole.PRIMARY or lab.slot == primary
                else CameraRole.SECONDARY
            ),
            kind=CameraKind.USB,
            uri=f"replay:{lab.slot}",
        )
        for lab in labels
    )
    return EngineConfig(
        cameras=cameras,
        profile=builtin_profile(kinds.pop()),
        thresholds=thresholds or Thresholds(),
    )


def _run(
    pipeline: StationPipeline,
    feed: Iterator[tuple[float, int, Callable[[], None]]],
    end_s: float,
) -> list[tuple[float, AttentionState]]:
    """Process (ts, slot, step) items in time order and tick every `TICK_S`.

    Ticks run on their own schedule, as in the live engine, so a camera that
    drops out goes stale and the other cameras or Unknown take over. Frames
    stamped at or before a tick time are processed before that tick.
    """
    samples: list[tuple[float, AttentionState]] = []
    index = 0

    def tick_until(limit: float) -> None:
        """Tick at every tick time strictly before `limit`."""
        nonlocal index
        while True:
            t = round(index * TICK_S, 6)
            if t >= limit - 1e-9:
                return
            out = pipeline.tick(t, REPLAY_EPOCH + timedelta(seconds=t))
            samples.append((t, out.status.state))
            index += 1

    for ts, _slot, step in feed:
        tick_until(ts)
        step()
    tick_until(end_s)
    return samples


def _merge_by_time(
    streams: Sequence[Iterator[tuple[float, int, Callable[[], None]]]],
) -> Iterator[tuple[float, int, Callable[[], None]]]:
    import heapq

    return heapq.merge(*streams, key=lambda item: (item[0], item[1]))


def replay_traces(
    name: str,
    recordings: Sequence[Recording],
    *,
    thresholds: Thresholds | None = None,
) -> ReplayResult:
    """Replay signal traces (one per camera) through estimator, fusion and state machine."""
    config = session_config(recordings, thresholds)
    pipeline = StationPipeline(config)

    def stream(rec: Recording) -> Iterator[tuple[float, int, Callable[[], None]]]:
        slot = rec.label.slot
        for frame in iter_trace(rec.path):
            relabelled = frame.model_copy(update={"camera_slot": slot})

            def step(f: SignalFrame = relabelled) -> None:
                pipeline.process_signals(f)

            yield relabelled.ts, slot, step

    duration = max(r.label.duration_s for r in recordings)
    samples = _run(pipeline, _merge_by_time([stream(r) for r in recordings]), duration)
    return ReplayResult(name, timeline_from_samples(samples, duration), duration)


def replay_videos(
    name: str,
    recordings: Sequence[Recording],
    provider_factory: Callable[[CameraConfig], LandmarkProvider],
    *,
    reader: VideoReader = opencv_reader,
    thresholds: Thresholds | None = None,
    signal_listener: SignalListener | None = None,
) -> ReplayResult:
    """Replay video files through the landmark model and the whole pipeline.

    Frame `n` of a recording is at `n / fps` seconds, from the label's fps.
    """
    config = session_config(recordings, thresholds)
    pipeline = StationPipeline(config, signal_listener=signal_listener)
    providers = {cam.slot: provider_factory(cam) for cam in config.cameras}
    with_pose = pipeline.with_pose

    def stream(rec: Recording) -> Iterator[tuple[float, int, Callable[[], None]]]:
        slot = rec.label.slot
        provider = providers[slot]
        for index, image in enumerate(reader(rec.path)):
            ts = round(index / rec.label.fps, 6)
            frame = Frame(
                camera_slot=slot,
                seq=index + 1,
                ts=ts,
                captured_at=REPLAY_EPOCH + timedelta(seconds=ts),
                image=image,
            )

            def step(f: Frame = frame) -> None:
                try:
                    result = provider.detect(f, with_pose=with_pose)
                except LandmarkError:
                    return  # an unreadable frame, as in the live engine
                pipeline.process_landmarks(result)

            yield ts, slot, step

    duration = max(r.label.duration_s for r in recordings)
    try:
        samples = _run(pipeline, _merge_by_time([stream(r) for r in recordings]), duration)
    finally:
        for provider in providers.values():
            provider.close()
    return ReplayResult(name, timeline_from_samples(samples, duration), duration)


def truth_timeline(label: LabelFile) -> list[Segment]:
    """The labelled intervals as a timeline."""
    return [Segment(i.start_s, i.end_s, i.state) for i in label.intervals]
