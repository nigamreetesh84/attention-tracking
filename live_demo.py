"""Live demo of the Phase 1 detection engine on a local webcam.

Usage (from the repo root, inside the venv):

    python live_demo.py --models D:\\ag-models            # webcam 0
    python live_demo.py --models D:\\ag-models --camera 1 --seconds 120
    python live_demo.py --fake                             # no camera, no models

Prints the station state once a second. Nothing is saved to disk.
"""

import argparse
import time
from pathlib import Path

from ag_shared.config import CameraConfig, CameraKind, CameraRole, EngineConfig
from ag_shared.ppe import PpeProfileKind, builtin_profile
from ag_workstation.capture import CaptureManager, FakeCamera
from ag_workstation.engine import Engine
from ag_workstation.landmarks import FakeLandmarkProvider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, help="folder with face_landmarker.task")
    parser.add_argument("--camera", default="0", help="webcam index or rtsp:// URL")
    parser.add_argument("--profile", default="open_face", choices=[k.value for k in PpeProfileKind])
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--fake", action="store_true", help="fake camera and model")
    args = parser.parse_args()

    kind = CameraKind.RTSP if args.camera.startswith("rtsp://") else CameraKind.USB
    camera = CameraConfig(slot=1, role=CameraRole.PRIMARY, kind=kind, uri=args.camera)
    config = EngineConfig(cameras=(camera,), profile=builtin_profile(PpeProfileKind(args.profile)))

    if args.fake:
        fake_cam = FakeCamera(width=64, height=48, fps=15)
        provider_factory = lambda cam: FakeLandmarkProvider()  # noqa: E731
        feed_factory = lambda cams: CaptureManager(  # noqa: E731
            cams, source_factory=lambda c: fake_cam.make_source()
        )
        engine = Engine(config, provider_factory=provider_factory, feed_factory=feed_factory)
    else:
        if args.models is None:
            parser.error("--models is required unless --fake is used")
        from ag_workstation.landmarks.mediapipe_provider import (
            MediaPipeLandmarkProvider,
            model_paths,
        )

        models = model_paths(args.models)
        engine = Engine(config, provider_factory=lambda cam: MediaPipeLandmarkProvider(models))

    engine.start()
    print(
        "Running. Look at the camera, close your eyes for 5 s, look away, walk off. Ctrl+C stops."
    )
    try:
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            time.sleep(1.0)
            status = engine.status()
            cam = engine.health().cameras[1]
            print(
                f"state={status.state.value:<10} confidence={status.confidence:.2f} "
                f"camera={cam.capture.status.value if cam.capture else '?':<8} "
                f"fps={cam.processed_fps:4.1f} quality={cam.last_quality or 0:.2f} "
                f"scan_would_be={engine.scan_decision().outcome.value}"
            )
            for event in engine.drain_events():
                print(
                    "  event:",
                    type(event).__name__,
                    getattr(event, "episode", None) and event.episode.state.value,
                )
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
