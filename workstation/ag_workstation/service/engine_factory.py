"""The production engine factory for the service's engine child (task 2.9-pre).

`real_engine_factory` is the default `ProcessLauncher` factory
(`DEFAULT_ENGINE_FACTORY`). It builds the real `Engine` (PLAN 1.8) behind an
`EngineAdapter`, the evidence rings and `BurstAssembler` (1.9), the capture
gate and `GatedEvidenceSink` (2.5) in front of the `EvidenceStore` (2.2), and
an `EngineEventRecorder` that queues episodes and buckets.

Everything fails open:

- no camera config (`AG_ENGINE_CONFIG` unset or invalid, and no config
  bundle stored by the sync client under `AG_DATA_DIR`): the engine is not
  started, the state is Unknown and health says `not_configured` until a
  config is applied over IPC;
- no camera or a camera that cannot be opened: capture reports it offline,
  the state is Unknown;
- no landmark model (`AG_MODEL_DIR` unset or the `.task` file missing): that
  camera gets no provider, its state is Unknown, health says
  `model_unavailable`; the child keeps running;
- no data directory or no data key: evidence is withheld (`disk_full`) and
  engine events are counted as lost; detection and scan answers still work.

Configuration comes from the environment the service gives the child:
`AG_ENGINE_CONFIG` (path to an `EngineConfig` JSON file), `AG_MODEL_DIR`,
`AG_DATA_DIR` (queue and evidence store) and, outside Windows, `AG_DATA_KEY`
(hex data key, development only; on Windows the DPAPI key file under
`AG_DATA_DIR/keys` is used).
"""

import logging
import os
import sys
import threading
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Final
from uuid import UUID

from ag_shared.config import CameraConfig, CaptureConfig, EngineConfig

from ag_workstation.clock import Clock, SystemClock
from ag_workstation.consent.gate import CaptureGate
from ag_workstation.consent.permit import CapturePermit
from ag_workstation.consent.sink import CaptureOutcome, GatedEvidenceSink, SavedBurst
from ag_workstation.engine.adapter import PUMP_INTERVAL_S, EngineAdapter
from ag_workstation.engine.runtime import Engine, FeedFactory, ProviderFactory
from ag_workstation.engine_api import EngineEventListener, EvidenceBurst
from ag_workstation.evidence.burst import AsyncSink, BurstAssembler
from ag_workstation.evidence.ring import EvidenceRings, JpegEncoder
from ag_workstation.landmarks.types import LandmarkError, LandmarkProvider
from ag_workstation.queue import DpapiKeyProvider, InMemoryKeyProvider, KeyProvider
from ag_workstation.service.station import (
    EngineEventRecorder,
    FanOut,
    OutcomeRegistry,
    PushedSessions,
    StationChild,
)

logger = logging.getLogger(__name__)

DEFAULT_ENGINE_FACTORY: Final = "ag_workstation.service.engine_factory:real_engine_factory"
ENGINE_CONFIG_ENV: Final = "AG_ENGINE_CONFIG"
DATA_DIR_ENV: Final = "AG_DATA_DIR"
DATA_KEY_ENV: Final = "AG_DATA_KEY"
ALL_SLOTS: Final = (1, 2, 3, 4)
"""Rings exist for every slot, so a camera change needs no new rings."""


class FailOpenProviderFactory:
    """Wraps a provider factory and remembers which cameras could not get one.

    The engine already isolates a failing factory (that camera stays
    Unknown); this records it so health can say `model_unavailable`.
    """

    def __init__(self, inner: ProviderFactory) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._failed: set[int] = set()

    def __call__(self, camera: CameraConfig) -> LandmarkProvider:
        """Build a provider; on failure record the slot and re-raise."""
        try:
            provider = self._inner(camera)
        except Exception:
            with self._lock:
                self._failed.add(camera.slot)
            raise
        with self._lock:
            self._failed.discard(camera.slot)
        return provider

    def failed_slots(self) -> Collection[int]:
        """Cameras without a landmark provider."""
        with self._lock:
            return frozenset(self._failed)


def mediapipe_provider_factory(env: Mapping[str, str]) -> ProviderFactory:
    """MediaPipe providers from `AG_MODEL_DIR`; a missing model fails at once."""

    def make(camera: CameraConfig) -> LandmarkProvider:
        from ag_workstation.landmarks.mediapipe_provider import (
            MediaPipeLandmarkProvider,
            model_paths_from_env,
        )

        paths = model_paths_from_env(env)
        if not paths.face.is_file():
            raise LandmarkError("face model file not found")
        return MediaPipeLandmarkProvider(paths)

    return make


def _real_encoder() -> JpegEncoder:
    from ag_workstation.capture.jpeg import encode_jpeg

    return encode_jpeg


def load_engine_config(env: Mapping[str, str]) -> EngineConfig | None:
    """The `EngineConfig` named by `AG_ENGINE_CONFIG`, or None (logged, never raised)."""
    raw = env.get(ENGINE_CONFIG_ENV, "").strip()
    if not raw:
        logger.warning("%s is not set; the engine waits for a config", ENGINE_CONFIG_ENV)
        return None
    try:
        return EngineConfig.model_validate_json(Path(raw).read_bytes())
    except Exception as exc:
        # Type only: the file holds camera URIs that may carry credentials.
        logger.error("engine config not loaded (%s)", type(exc).__name__)
        return None


def station_config_from_bundle(data_dir: Path, keys: KeyProvider) -> EngineConfig | None:
    """The station config of the last bundle the sync client applied (PLAN 2.9).

    None when there is none or it does not build (logged, never raised).
    """
    from ag_workstation.sync.config_store import bundle_path, load_bundle

    bundle = load_bundle(bundle_path(data_dir), keys)
    if bundle is None:
        return None
    try:
        return bundle.engine_config()
    except Exception as exc:
        logger.error("stored bundle gives no engine config (%s)", type(exc).__name__)
        return None


def key_provider_from_env(env: Mapping[str, str], data_dir: Path | None) -> KeyProvider | None:
    """The local data key: `AG_DATA_KEY` (development), else DPAPI on Windows."""
    raw = env.get(DATA_KEY_ENV, "").strip()
    if raw:
        try:
            return InMemoryKeyProvider(bytes.fromhex(raw))
        except ValueError as exc:
            logger.error("%s is not a valid key (%s)", DATA_KEY_ENV, type(exc).__name__)
            return None
    if sys.platform == "win32" and data_dir is not None:
        return DpapiKeyProvider(data_dir / "keys" / "data.key")
    return None


class _NoStore:
    """Stands in for an evidence store that could not be opened: every save fails,
    so the sink reports `disk_full` and nothing is written."""

    def save_burst(
        self, burst: EvidenceBurst, event_id: UUID | None = None, *, permit: CapturePermit
    ) -> SavedBurst:
        """Refuse."""
        raise OSError("evidence store unavailable")


def build_station(
    listener: EngineEventListener | None,
    *,
    env: Mapping[str, str],
    clock: Clock | None = None,
    feed_factory: FeedFactory | None = None,
    provider_factory: ProviderFactory | None = None,
    encoder: JpegEncoder | None = None,
    keys: KeyProvider | None = None,
    start: bool = True,
    tick_interval_s: float = 0.1,
    pump_interval_s: float = PUMP_INTERVAL_S,
) -> StationChild:
    """Assemble the real engine and its evidence path. Never raises for
    missing hardware, models, config or storage (see the module docstring)."""
    from ag_workstation.evidence.alerts import QueueDiskFullAlert
    from ag_workstation.evidence.store import EvidenceStore
    from ag_workstation.queue import EventQueue

    clock = clock or SystemClock()
    config = load_engine_config(env)
    closers: list[Callable[[], None]] = []

    data_raw = env.get(DATA_DIR_ENV, "").strip()
    data_dir = Path(data_raw) if data_raw else None
    keys = keys or key_provider_from_env(env, data_dir)
    if config is None and data_dir is not None and keys is not None:
        config = station_config_from_bundle(data_dir, keys)
    queue: EventQueue | None = None
    store: EvidenceStore | None = None
    if data_dir is not None and keys is not None:
        try:
            # Enqueue only: the service parent is the queue's single sender.
            queue = EventQueue.open(data_dir / "queue.db", keys, clock, recover_sending=False)
            closers.append(queue.close)
            store = EvidenceStore.open(
                data_dir / "evidence", keys, clock, alert_sink=QueueDiskFullAlert(queue, clock)
            )
            closers.append(store.close)
        except Exception as exc:
            logger.error("local storage unavailable (%s); metadata only", type(exc).__name__)
    else:
        logger.warning("no data directory or key: evidence withheld, events not queued")

    sessions = PushedSessions(clock)
    outcomes = OutcomeRegistry(clock)
    recorder = EngineEventRecorder(queue, sessions, outcomes, clock)
    if queue is not None:
        # Before the queue closes (closers run in reverse): queue waiting episodes.
        closers.insert(1, recorder.flush)

    def on_outcome(outcome: CaptureOutcome) -> None:
        outcomes.record(outcome)
        recorder.outcome_arrived(outcome)

    holder: dict[str, EngineAdapter] = {}

    def on_break() -> bool:
        adapter = holder.get("adapter")
        return adapter is not None and adapter.on_break()

    def schedule() -> EngineConfig | None:
        adapter = holder.get("adapter")
        return adapter.config if adapter is not None else None

    gate = CaptureGate(
        sessions=sessions,
        bundles=None,
        clock=clock,
        engine_on_break=on_break,
        schedule=schedule,
    )
    sink = AsyncSink(
        GatedEvidenceSink(gate, store if store is not None else _NoStore(), on_outcome=on_outcome)
    )
    closers.append(sink.close)
    capture = config.capture if config is not None else CaptureConfig()
    rings = EvidenceRings(ALL_SLOTS, capture, encoder or _real_encoder())
    assembler = BurstAssembler(
        rings, capture, sink, wall_clock=clock.now, mono_clock=clock.monotonic
    )
    providers = FailOpenProviderFactory(provider_factory or mediapipe_provider_factory(env))

    def build(cfg: EngineConfig) -> Engine:
        if feed_factory is None:
            return Engine(
                cfg,
                provider_factory=providers,
                clock=clock,
                tick_interval_s=tick_interval_s,
                frame_listener=rings.offer,
            )
        return Engine(
            cfg,
            provider_factory=providers,
            feed_factory=feed_factory,
            clock=clock,
            tick_interval_s=tick_interval_s,
            frame_listener=rings.offer,
        )

    fan_out = FanOut(recorder, listener) if listener is not None else FanOut(recorder)
    adapter = EngineAdapter(
        build,
        clock=clock,
        config=config,
        assembler=assembler,
        listener=fan_out,
        unavailable_slots=providers.failed_slots,
        pump_interval_s=pump_interval_s,
    )
    holder["adapter"] = adapter
    if start:
        adapter.start()
    return StationChild(adapter, sessions, outcomes, recorder=recorder, closers=tuple(closers))


def real_engine_factory(listener: EngineEventListener) -> StationChild:
    """The service child's default engine: the real one, configured from `os.environ`."""
    return build_station(listener, env=dict(os.environ))
