"""`MediaPipeLandmarkProvider` against a fake backend, and the fake provider.

The real MediaPipe graphs need the `.task` model files, which are not in the
repository. The one test that loads them is marked `video` and skips unless
`AG_MODEL_DIR` points at them.
"""

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import ag_workstation
import numpy as np
import numpy.typing as npt
import pytest
from ag_workstation.capture import Frame
from ag_workstation.landmarks import (
    FakeLandmarkProvider,
    LandmarkError,
    LandmarkProvider,
    LandmarkResult,
    Point,
    RawFaceResult,
    RawPoseResult,
    raw_face,
    raw_pose,
    synthetic_face,
    synthetic_pose,
    synthetic_result,
)
from ag_workstation.landmarks.mediapipe_provider import (
    FACE_MODEL_FILE,
    MODEL_DIR_ENV,
    POSE_MODEL_FILE,
    MediaPipeLandmarkProvider,
    ModelPaths,
    model_paths,
    model_paths_from_env,
)


def _frame(ts: float = 1.0, value: int = 120) -> Frame:
    image = np.full((60, 80, 3), value, dtype=np.uint8)
    image[:, :, 0] = 10  # blue channel, to see the BGR -> RGB swap
    return Frame(
        camera_slot=1, seq=1, ts=ts, captured_at=datetime(2026, 1, 1, tzinfo=UTC), image=image
    )


@dataclass
class FakeLandmarker:
    result: object
    calls: list[tuple[npt.NDArray[np.uint8], int]] = field(default_factory=list)
    closed: int = 0
    error: Exception | None = None

    def detect_for_video(self, image: npt.NDArray[np.uint8], timestamp_ms: int) -> object:
        if self.error is not None:
            raise self.error
        self.calls.append((image, timestamp_ms))
        return self.result

    def close(self) -> None:
        self.closed += 1


class FakeBackend:
    def __init__(self) -> None:
        self.face = FakeLandmarker(RawFaceResult([raw_face(synthetic_face())]))
        self.pose = FakeLandmarker(RawPoseResult([raw_pose(synthetic_pose())]))
        self.face_requests: list[tuple[Path, int]] = []
        self.pose_requests: list[tuple[Path, int]] = []

    def make_image(self, rgb: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
        return rgb

    def face_landmarker(self, model: Path, max_faces: int) -> FakeLandmarker:
        self.face_requests.append((model, max_faces))
        return self.face

    def pose_landmarker(self, model: Path, max_poses: int) -> FakeLandmarker:
        self.pose_requests.append((model, max_poses))
        return self.pose


@pytest.fixture
def models(tmp_path: Path) -> ModelPaths:
    (tmp_path / FACE_MODEL_FILE).write_bytes(b"face")
    (tmp_path / POSE_MODEL_FILE).write_bytes(b"pose")
    return model_paths(tmp_path)


def _provider(models: ModelPaths, backend: FakeBackend) -> MediaPipeLandmarkProvider:
    return MediaPipeLandmarkProvider(models, backend=backend)


# --- MediaPipe provider with a fake backend -----------------------------------------


def test_detect_maps_the_face_result(models: ModelPaths) -> None:
    backend = FakeBackend()
    provider: LandmarkProvider = _provider(models, backend)
    result = provider.detect(_frame(ts=2.5))
    assert result.camera_slot == 1
    assert result.ts == 2.5
    assert (result.width, result.height) == (80, 60)
    assert len(result.faces) == 1
    assert result.faces[0].right_eye == synthetic_face().right_eye
    assert result.poses is None
    assert backend.face_requests == [(models.face, 3)]
    assert backend.pose_requests == []


def test_the_backend_receives_a_contiguous_rgb_image(models: ModelPaths) -> None:
    backend = FakeBackend()
    frame = _frame()
    _provider(models, backend).detect(frame)
    image, _ = backend.face.calls[0]
    assert image.flags["C_CONTIGUOUS"]
    assert image.shape == (60, 80, 3)
    assert image[0, 0, 2] == 10  # blue is last in RGB
    assert image[0, 0, 0] == 120
    assert frame.image[0, 0, 0] == 10  # the frame itself is untouched


def test_the_pose_model_is_loaded_only_when_asked_for(models: ModelPaths) -> None:
    backend = FakeBackend()
    provider = _provider(models, backend)
    provider.detect(_frame(1.0))
    assert backend.pose_requests == []
    with_pose = provider.detect(_frame(1.1), with_pose=True)
    assert with_pose.poses is not None
    assert with_pose.poses[0] == synthetic_pose()
    provider.detect(_frame(1.2), with_pose=True)
    assert backend.pose_requests == [(models.pose, 2)]  # created once
    assert len(backend.face_requests) == 1
    assert provider.detect(_frame(1.3)).poses is None


def test_without_a_pose_model_the_pose_is_not_run(tmp_path: Path) -> None:
    (tmp_path / FACE_MODEL_FILE).write_bytes(b"face")
    backend = FakeBackend()
    provider = _provider(model_paths(tmp_path), backend)
    assert provider.detect(_frame(), with_pose=True).poses is None
    assert backend.pose_requests == []


def test_timestamps_are_milliseconds_and_strictly_increasing(models: ModelPaths) -> None:
    backend = FakeBackend()
    provider = _provider(models, backend)
    for ts in (1.0, 1.0004, 1.0, 0.5, 2.0):
        provider.detect(_frame(ts), with_pose=True)
    assert [ms for _, ms in backend.face.calls] == [1000, 1001, 1002, 1003, 2000]
    assert [ms for _, ms in backend.pose.calls] == [1000, 1001, 1002, 1003, 2000]


def test_a_missing_model_file_is_a_landmark_error(tmp_path: Path) -> None:
    backend = FakeBackend()
    provider = _provider(model_paths(tmp_path), backend)
    with pytest.raises(LandmarkError, match=FACE_MODEL_FILE):
        provider.detect(_frame())
    assert backend.face_requests == []


def test_a_missing_pose_model_file_is_a_landmark_error(tmp_path: Path) -> None:
    (tmp_path / FACE_MODEL_FILE).write_bytes(b"face")
    models = ModelPaths(face=tmp_path / FACE_MODEL_FILE, pose=tmp_path / POSE_MODEL_FILE)
    provider = _provider(models, FakeBackend())
    assert provider.detect(_frame(1.0)).poses is None
    with pytest.raises(LandmarkError, match=POSE_MODEL_FILE):
        provider.detect(_frame(1.1), with_pose=True)


def test_backend_failures_become_landmark_errors(models: ModelPaths) -> None:
    backend = FakeBackend()
    backend.face.error = RuntimeError("graph exploded")
    with pytest.raises(LandmarkError, match="RuntimeError"):
        _provider(models, backend).detect(_frame())


def test_an_unusable_image_is_a_landmark_error(models: ModelPaths) -> None:
    frame = Frame(
        camera_slot=1,
        seq=1,
        ts=0.0,
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        image=np.zeros((4, 4), dtype=np.uint8),
    )
    with pytest.raises(LandmarkError, match="image"):
        _provider(models, FakeBackend()).detect(frame)


def test_close_releases_the_graphs_and_is_final(models: ModelPaths) -> None:
    backend = FakeBackend()
    provider = _provider(models, backend)
    provider.detect(_frame(), with_pose=True)
    provider.close()
    provider.close()
    assert backend.face.closed == 1
    assert backend.pose.closed == 1
    with pytest.raises(LandmarkError, match="closed"):
        provider.detect(_frame(2.0))


def test_close_before_first_use_is_harmless(models: ModelPaths) -> None:
    _provider(models, FakeBackend()).close()


def test_face_and_pose_limits_are_configurable(models: ModelPaths) -> None:
    backend = FakeBackend()
    provider = MediaPipeLandmarkProvider(models, max_faces=5, max_poses=1, backend=backend)
    provider.detect(_frame(), with_pose=True)
    assert backend.face_requests[0][1] == 5
    assert backend.pose_requests[0][1] == 1
    with pytest.raises(ValueError, match="max_faces"):
        MediaPipeLandmarkProvider(models, max_faces=0, backend=backend)


# --- model paths --------------------------------------------------------------------


def test_model_paths_in_a_directory(tmp_path: Path) -> None:
    paths = model_paths(tmp_path)
    assert paths.face == tmp_path / FACE_MODEL_FILE
    assert paths.pose is None  # no pose file there
    (tmp_path / POSE_MODEL_FILE).write_bytes(b"pose")
    assert model_paths(tmp_path).pose == tmp_path / POSE_MODEL_FILE


def test_model_paths_from_the_environment(tmp_path: Path) -> None:
    assert model_paths_from_env({MODEL_DIR_ENV: str(tmp_path)}).face == tmp_path / FACE_MODEL_FILE
    with pytest.raises(LandmarkError, match=MODEL_DIR_ENV):
        model_paths_from_env({})
    with pytest.raises(LandmarkError, match=MODEL_DIR_ENV):
        model_paths_from_env({MODEL_DIR_ENV: "  "})


# --- layering -----------------------------------------------------------------------


def test_importing_the_engine_packages_does_not_load_mediapipe() -> None:
    code = (
        "import sys\n"
        "import ag_workstation.landmarks, ag_workstation.signals\n"
        "import ag_workstation.landmarks.mediapipe_provider\n"
        "assert 'mediapipe' not in sys.modules, 'mediapipe was imported'\n"
        "assert 'cv2' not in sys.modules, 'cv2 was imported'\n"
    )
    done = subprocess.run(  # noqa: S603 - our own interpreter and a fixed script
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_only_the_provider_module_imports_mediapipe() -> None:
    root = Path(ag_workstation.__file__).parent
    importers = {
        source.relative_to(root).as_posix()
        for source in root.rglob("*.py")
        if "import mediapipe" in source.read_text(encoding="utf-8")
    }
    assert importers == {"landmarks/mediapipe_provider.py"}


# --- fake provider ------------------------------------------------------------------


def test_fake_provider_returns_an_attentive_face_by_default() -> None:
    provider: LandmarkProvider = FakeLandmarkProvider()
    result = provider.detect(_frame(ts=3.0))
    assert result.ts == 3.0
    assert result.camera_slot == 1
    assert (result.width, result.height) == (80, 60)
    assert len(result.faces) == 1
    assert result.poses is None
    assert provider.detect(_frame(ts=3.1), with_pose=True).poses is not None


def test_fake_provider_follows_a_script() -> None:
    def script(frame: Frame, with_pose: bool) -> LandmarkResult:
        faces = () if frame.ts >= 1.0 else (synthetic_face(center=Point(0.3, 0.3)),)
        return synthetic_result(frame.ts, faces, camera_slot=frame.camera_slot)

    provider = FakeLandmarkProvider(script)
    assert len(provider.detect(_frame(0.5)).faces) == 1
    assert provider.detect(_frame(1.0)).faces == ()
    assert provider.calls == 2


def test_fake_provider_can_fail_and_close() -> None:
    provider = FakeLandmarkProvider()
    provider.failing = True
    with pytest.raises(LandmarkError):
        provider.detect(_frame())
    provider.failing = False
    provider.close()
    assert provider.closed
    with pytest.raises(LandmarkError, match="closed"):
        provider.detect(_frame())


# --- real models (manual / owner check) ---------------------------------------------


@pytest.mark.video
def test_real_models_load_and_run_on_a_blank_frame() -> None:
    model_dir = os.environ.get(MODEL_DIR_ENV, "")
    if not model_dir or not (Path(model_dir) / FACE_MODEL_FILE).is_file():
        pytest.skip(f"{MODEL_DIR_ENV} does not point at the MediaPipe .task files")
    provider = MediaPipeLandmarkProvider(model_paths(Path(model_dir)))
    try:
        result = provider.detect(_frame(ts=0.0), with_pose=True)
        again = provider.detect(_frame(ts=0.1), with_pose=True)
    finally:
        provider.close()
    assert result.faces == ()
    assert again.faces == ()
    assert result.frame_stats is not None
