"""The MediaPipe Tasks landmark provider (face landmarker and pose landmarker).

This is the only module that imports MediaPipe, and it does so lazily: the
import happens when the first frame is processed, so the package imports
without MediaPipe installed and CI never loads it.

The models are `.task` files that are not part of the repository; see
`workstation/README.md` for where to get them. Their directory comes from
the `AG_MODEL_DIR` environment variable (or an explicit `ModelPaths`).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ag_workstation.capture.types import Frame
from ag_workstation.landmarks.mapping import build_result
from ag_workstation.landmarks.types import LandmarkError, LandmarkResult

MODEL_DIR_ENV = "AG_MODEL_DIR"
FACE_MODEL_FILE = "face_landmarker.task"
POSE_MODEL_FILE = "pose_landmarker_lite.task"


@dataclass(frozen=True, slots=True)
class ModelPaths:
    """Where the MediaPipe `.task` files are."""

    face: Path
    pose: Path | None = None
    """None: posture and motion from the body are not available."""


def model_paths(model_dir: Path) -> ModelPaths:
    """The standard file names inside `model_dir`.

    The pose model is optional: it is used only when its file is present.
    """
    pose = model_dir / POSE_MODEL_FILE
    return ModelPaths(face=model_dir / FACE_MODEL_FILE, pose=pose if pose.is_file() else None)


def model_paths_from_env(env: Mapping[str, str]) -> ModelPaths:
    """`model_paths` for the directory named by `AG_MODEL_DIR` in `env`."""
    model_dir = env.get(MODEL_DIR_ENV, "").strip()
    if not model_dir:
        raise LandmarkError(f"{MODEL_DIR_ENV} is not set; it must name the model directory")
    return model_paths(Path(model_dir))


class Landmarker(Protocol):
    """The part of a MediaPipe landmarker task that is used here."""

    # Any: MediaPipe images and results are untyped (no stubs).
    def detect_for_video(self, image: Any, timestamp_ms: int) -> Any:
        """Run the model on one frame of a video stream."""
        ...

    def close(self) -> None:
        """Release the graph."""
        ...


class MediaPipeBackend(Protocol):
    """Creates MediaPipe objects. The seam that tests replace."""

    # Any: a MediaPipe `Image`, untyped.
    def make_image(self, rgb: np.ndarray[Any, np.dtype[np.uint8]]) -> Any:
        """Wrap a contiguous RGB array for the landmarkers."""
        ...

    def face_landmarker(self, model: Path, max_faces: int) -> Landmarker:
        """A face landmarker in video mode with blendshapes and transforms."""
        ...

    def pose_landmarker(self, model: Path, max_poses: int) -> Landmarker:
        """A pose landmarker in video mode."""
        ...


class TasksBackend:  # pragma: no cover - needs MediaPipe and the model files (manual check)
    """The real backend on `mediapipe.tasks`."""

    def __init__(self) -> None:
        import mediapipe as mp

        self._mp = mp

    def make_image(self, rgb: np.ndarray[Any, np.dtype[np.uint8]]) -> Any:
        """A MediaPipe SRGB image over `rgb`."""
        return self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)

    def face_landmarker(self, model: Path, max_faces: int) -> Landmarker:
        """Face landmarker: video mode, blink blendshapes, head transform."""
        vision = self._mp.tasks.vision
        options = vision.FaceLandmarkerOptions(
            # The file is passed as bytes: MediaPipe's own path handling fails
            # on some Windows paths (spaces, non-ASCII).
            base_options=self._mp.tasks.BaseOptions(model_asset_buffer=model.read_bytes()),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=max_faces,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        landmarker: Landmarker = vision.FaceLandmarker.create_from_options(options)
        return landmarker

    def pose_landmarker(self, model: Path, max_poses: int) -> Landmarker:
        """Pose landmarker: video mode, no segmentation masks."""
        vision = self._mp.tasks.vision
        options = vision.PoseLandmarkerOptions(
            base_options=self._mp.tasks.BaseOptions(model_asset_buffer=model.read_bytes()),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=max_poses,
            output_segmentation_masks=False,
        )
        landmarker: Landmarker = vision.PoseLandmarker.create_from_options(options)
        return landmarker


class MediaPipeLandmarkProvider:
    """`LandmarkProvider` on MediaPipe Tasks. One instance per camera.

    The models are loaded on first use, the pose model only when a frame
    asks for it. Every failure surfaces as `LandmarkError` whose message
    names the exception type only.
    """

    def __init__(
        self,
        models: ModelPaths,
        *,
        max_faces: int = 3,
        max_poses: int = 2,
        backend: MediaPipeBackend | None = None,
    ) -> None:
        if max_faces < 1:
            raise ValueError("max_faces must be at least 1")
        if max_poses < 1:
            raise ValueError("max_poses must be at least 1")
        self._models = models
        self._max_faces = max_faces
        self._max_poses = max_poses
        self._backend = backend
        self._face: Landmarker | None = None
        self._pose: Landmarker | None = None
        self._last_ms: int | None = None
        self._closed = False

    def detect(self, frame: Frame, *, with_pose: bool = False) -> LandmarkResult:
        """Run the face model (and the pose model when asked) on `frame`."""
        if self._closed:
            raise LandmarkError("provider is closed")
        image = frame.image
        if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
            raise LandmarkError("frame image is not a BGR image")
        run_pose = with_pose and self._models.pose is not None
        try:
            backend = self._get_backend()
            face = self._get_face(backend)
            pose = self._get_pose(backend) if run_pose else None
            # MediaPipe wants RGB in one contiguous block; the frame is BGR.
            mp_image = backend.make_image(np.ascontiguousarray(image[:, :, ::-1]))
            timestamp_ms = self._next_timestamp_ms(frame.ts)
            face_result = face.detect_for_video(mp_image, timestamp_ms)
            pose_result = None if pose is None else pose.detect_for_video(mp_image, timestamp_ms)
        except LandmarkError:
            raise
        except Exception as exc:
            # The type name only: backend messages may carry paths or data.
            raise LandmarkError(f"landmark detection failed: {type(exc).__name__}") from exc
        return build_result(frame, face_result, pose_result)

    def close(self) -> None:
        """Release both models. Later `detect` calls fail."""
        self._closed = True
        for landmarker in (self._face, self._pose):
            if landmarker is not None:
                try:
                    landmarker.close()
                except Exception:  # noqa: S110 - closing is best effort
                    pass
        self._face = None
        self._pose = None

    def _get_backend(self) -> MediaPipeBackend:
        if self._backend is None:
            self._backend = TasksBackend()  # pragma: no cover - real MediaPipe
        return self._backend

    def _get_face(self, backend: MediaPipeBackend) -> Landmarker:
        if self._face is None:
            self._face = backend.face_landmarker(_existing(self._models.face), self._max_faces)
        return self._face

    def _get_pose(self, backend: MediaPipeBackend) -> Landmarker:
        if self._pose is None:
            if self._models.pose is None:  # pragma: no cover - guarded by the caller
                raise LandmarkError("no pose model configured")
            self._pose = backend.pose_landmarker(_existing(self._models.pose), self._max_poses)
        return self._pose

    def _next_timestamp_ms(self, ts: float) -> int:
        """Milliseconds for video mode, which must strictly increase."""
        ms = round(ts * 1000)
        if self._last_ms is not None and ms <= self._last_ms:
            ms = self._last_ms + 1
        self._last_ms = ms
        return ms


def _existing(path: Path) -> Path:
    if not path.is_file():
        raise LandmarkError(f"model file not found: {path.name}")
    return path
