"""Find out why the face model processes no frames.

    python diagnose_landmarks.py --models D:\\ag-models [--camera 0]

Checks the Python and package versions, the model files, one webcam frame,
and one run of the face model, printing the full error at the step that fails.
"""

import argparse
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def step(name: str) -> None:
    print(f"\n== {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--camera", default="0")
    args = parser.parse_args()

    step("Python and packages")
    print("python", sys.version.split()[0], "at", sys.executable)
    try:
        import cv2
        import mediapipe as mp
        import numpy as np

        print("mediapipe", mp.__version__, "| opencv", cv2.__version__, "| numpy", np.__version__)
    except Exception:
        traceback.print_exc()
        return 1

    step("Model files")
    face = args.models / "face_landmarker.task"
    pose = args.models / "pose_landmarker_lite.task"
    for path in (face, pose):
        size = path.stat().st_size if path.is_file() else None
        print(f"{path}: {'MISSING' if size is None else f'{size:,} bytes'}")
    if not face.is_file():
        found = sorted(p.name for p in args.models.glob("*")) if args.models.is_dir() else []
        print("The face model must be named exactly face_landmarker.task. Folder has:", found)
        return 1

    step("Load the face model")
    try:
        vision = mp.tasks.vision
        options = vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_buffer=face.read_bytes()),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        landmarker = vision.FaceLandmarker.create_from_options(options)
        print("loaded")
    except Exception:
        traceback.print_exc()
        return 1

    step("Grab frames from the camera")
    index = int(args.camera) if args.camera.isdigit() else args.camera
    cap = (
        cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if isinstance(index, int)
        else cv2.VideoCapture(index)
    )
    frames = []
    for _ in range(30):
        ok, image = cap.read()
        if ok and image is not None:
            frames.append(image)
    cap.release()
    if not frames:
        print("No frame could be read from the camera.")
        return 1
    image = frames[-1]
    print(
        f"{len(frames)} frames, shape {image.shape}, dtype {image.dtype}, "
        f"mean brightness {image.mean():.0f}"
    )

    step("Run the face model (raw MediaPipe)")
    try:
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        result = landmarker.detect_for_video(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), 1
        )
        print("faces found:", len(result.face_landmarks))
    except Exception:
        traceback.print_exc()
        return 1

    step("Run the AttentionGuard provider")
    try:
        from ag_workstation.capture.types import Frame
        from ag_workstation.landmarks.mediapipe_provider import (
            MediaPipeLandmarkProvider,
            model_paths,
        )

        provider = MediaPipeLandmarkProvider(model_paths(args.models))
        for i, img in enumerate(frames[-5:]):
            frame = Frame(
                camera_slot=1, seq=i + 1, ts=i / 15, captured_at=datetime.now(UTC), image=img
            )
            try:
                res = provider.detect(frame)
                print(f"frame {i}: faces={len(res.faces)}")
            except Exception as exc:
                print(f"frame {i}: {exc!r}")
                cause = exc.__cause__
                if cause is not None:
                    traceback.print_exception(type(cause), cause, cause.__traceback__)
                return 1
        provider.close()
    except Exception:
        traceback.print_exc()
        return 1

    print("\nAll steps passed: the model works here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
