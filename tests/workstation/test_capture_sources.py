"""OpenCV and file sources, ONVIF stub, and the source factory.

Real cameras are mocked: `cv2.VideoCapture` is replaced for the live-camera
tests. `FileSource` reads a tiny video generated with OpenCV in a tmp dir.
"""

from pathlib import Path
from typing import Any, ClassVar

import cv2
import numpy as np
import pytest
from ag_shared.config import CameraConfig, CameraKind, CameraRole
from ag_workstation.capture import (
    CameraSource,
    CameraStatus,
    CameraWorker,
    CaptureError,
    EndOfStream,
    FakeCamera,
    OnvifSource,
    OnvifUnavailableError,
    StaticOnvifResolver,
    StubOnvifResolver,
    make_source,
    parse_device_index,
)
from ag_workstation.capture.opencv_source import FileSource, OpenCvSource
from ag_workstation.clock import FakeClock

# --- tiny generated video ---------------------------------------------------


def _write_video(path: Path, frames: int = 5, fps: float = 10.0) -> Path | None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), fps, (32, 24))
    if not writer.isOpened():
        return None
    for i in range(frames):
        writer.write(np.full((24, 32, 3), i * 40, dtype=np.uint8))
    writer.release()
    return path


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = _write_video(tmp_path / "clip.avi")
    if path is None:
        pytest.skip("this OpenCV build cannot write MJPG video")
    return path


def test_file_source_reads_all_frames_then_ends(video: Path) -> None:
    source = FileSource(video, realtime=False)
    source.open()
    assert source.fps == pytest.approx(10.0)
    images = [source.read() for _ in range(5)]
    assert all(img is not None and img.shape == (24, 32, 3) for img in images)
    with pytest.raises(EndOfStream):
        source.read()
    source.close()
    source.close()  # idempotent


def test_file_source_loops(video: Path) -> None:
    source = FileSource(video, loop=True, realtime=False)
    source.open()
    for _ in range(12):
        assert source.read() is not None
    source.close()


def test_file_source_paces_on_the_injected_clock(video: Path) -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    source = FileSource(video, realtime=True, clock=clock, sleep=fake_sleep)
    source.open()
    for _ in range(3):
        source.read()
    # Frame 0 is due at once, frames 1 and 2 at 0.1 s steps.
    assert sum(sleeps) == pytest.approx(0.2)
    assert clock.monotonic() == pytest.approx(0.2)
    source.close()


def test_file_source_missing_file(tmp_path: Path) -> None:
    source = FileSource(tmp_path / "nope.avi")
    with pytest.raises(CaptureError):
        source.open()
    with pytest.raises(CaptureError):
        source.read()


def test_file_source_in_a_worker_stops_at_the_end(video: Path) -> None:
    clock = FakeClock()
    worker = CameraWorker(
        slot=1, source_factory=lambda: FileSource(video, realtime=False), clock=clock
    )
    delays = [worker.step() for _ in range(6)]
    assert delays == [0.0] * 5 + [None]
    assert worker.health().status is CameraStatus.STOPPED
    assert worker.health().frames_captured == 5


# --- OpenCvSource with a mocked VideoCapture --------------------------------


class _FakeCapture:
    instances: ClassVar[list["_FakeCapture"]] = []
    opened = True
    reads_ok = True

    def __init__(self, locator: Any, api: int = 0, params: Any = None) -> None:
        # Any: mirrors cv2.VideoCapture's overloaded constructor.
        self.locator = locator
        self.api = api
        self.params = params
        self.released = False
        _FakeCapture.instances.append(self)

    def isOpened(self) -> bool:  # noqa: N802 - cv2 API name
        return _FakeCapture.opened

    def read(self) -> tuple[bool, Any]:
        if _FakeCapture.reads_ok:
            return True, np.zeros((4, 4, 3), dtype=np.uint8)
        return False, None

    def release(self) -> None:
        self.released = True


@pytest.fixture
def fake_cv(monkeypatch: pytest.MonkeyPatch) -> type[_FakeCapture]:
    _FakeCapture.instances = []
    _FakeCapture.opened = True
    _FakeCapture.reads_ok = True
    monkeypatch.setattr(cv2, "VideoCapture", _FakeCapture)
    return _FakeCapture


def test_opencv_source_usb_index(fake_cv: type[_FakeCapture]) -> None:
    source = OpenCvSource(0)
    source.open()
    img = source.read()
    assert img is not None and img.shape == (4, 4, 3)
    source.close()
    cap = fake_cv.instances[0]
    assert cap.locator == 0
    assert cap.released


def test_opencv_source_rtsp_sets_timeouts(fake_cv: type[_FakeCapture]) -> None:
    source = OpenCvSource("rtsp://u:p@cam/s", open_timeout_ms=1234, read_timeout_ms=987)
    source.open()
    cap = fake_cv.instances[0]
    assert cap.api == cv2.CAP_FFMPEG
    assert list(cap.params) == [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
        1234,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
        987,
    ]


def test_opencv_source_not_opened(fake_cv: type[_FakeCapture]) -> None:
    fake_cv.opened = False
    source = OpenCvSource("rtsp://u:secret@cam/s")
    with pytest.raises(CaptureError) as info:
        source.open()
    assert "secret" not in str(info.value)
    assert fake_cv.instances[0].released
    with pytest.raises(CaptureError):
        source.read()


def test_opencv_source_failed_read_returns_none(fake_cv: type[_FakeCapture]) -> None:
    fake_cv.reads_ok = False
    source = OpenCvSource(1)
    source.open()
    assert source.read() is None


def test_opencv_source_repr_hides_the_uri() -> None:
    assert "secret" not in repr(OpenCvSource("rtsp://u:secret@cam/s"))


# --- ONVIF stub ---------------------------------------------------------------


def test_stub_resolver_passes_rtsp_through() -> None:
    resolver = StubOnvifResolver()
    assert resolver.resolve("rtsp://cam/s") == "rtsp://cam/s"
    assert resolver.resolve("RTSPS://cam/s") == "RTSPS://cam/s"
    with pytest.raises(OnvifUnavailableError):
        resolver.resolve("http://cam/onvif/device_service")


def test_static_resolver() -> None:
    resolver = StaticOnvifResolver({"http://cam/onvif": "rtsp://cam/main"})
    assert resolver.resolve("http://cam/onvif") == "rtsp://cam/main"
    with pytest.raises(OnvifUnavailableError):
        resolver.resolve("http://other/onvif")


def test_onvif_source_resolves_on_open() -> None:
    camera = FakeCamera()
    seen: list[str] = []

    def make_rtsp(uri: str) -> CameraSource:
        seen.append(uri)
        return camera.make_source()

    source = OnvifSource(
        "http://cam/onvif",
        resolver=StaticOnvifResolver({"http://cam/onvif": "rtsp://cam/main"}),
        rtsp_source_factory=make_rtsp,
    )
    with pytest.raises(CaptureError):
        source.read()
    source.open()
    assert seen == ["rtsp://cam/main"]
    assert source.read() is not None
    source.close()
    assert camera.closes == 1
    source.close()


def test_onvif_resolution_failure_makes_the_worker_offline() -> None:
    def source_factory() -> CameraSource:
        return OnvifSource(
            "http://cam/onvif",
            resolver=StubOnvifResolver(),
            rtsp_source_factory=lambda _uri: FakeCamera().make_source(),
        )

    worker = CameraWorker(slot=2, source_factory=source_factory, clock=FakeClock())
    worker.step()
    assert worker.health().status is CameraStatus.OFFLINE
    assert worker.health().last_error == "OnvifUnavailableError"


# --- factory ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("uri", "index"),
    [("0", 0), ("2", 2), ("usb:1", 1), ("builtin:0", 0), ("builtin", 0), (" usb:3 ", 3)],
)
def test_parse_device_index(uri: str, index: int) -> None:
    assert parse_device_index(uri) == index


@pytest.mark.parametrize("uri", ["", "usb:", "usb:-1", "cam", "rtsp://x", "usb:1.5"])
def test_parse_device_index_rejects(uri: str) -> None:
    with pytest.raises(CaptureError):
        parse_device_index(uri)


def _cfg(kind: CameraKind, uri: str) -> CameraConfig:
    return CameraConfig(slot=1, role=CameraRole.PRIMARY, kind=kind, uri=uri)


def test_make_source_by_kind() -> None:
    usb = make_source(_cfg(CameraKind.USB, "usb:1"))
    assert isinstance(usb, OpenCvSource) and usb.locator == 1
    builtin = make_source(_cfg(CameraKind.BUILTIN, "builtin"))
    assert isinstance(builtin, OpenCvSource) and builtin.locator == 0
    rtsp = make_source(_cfg(CameraKind.RTSP, "rtsp://cam/s"))
    assert isinstance(rtsp, OpenCvSource) and rtsp.locator == "rtsp://cam/s"
    onvif = make_source(_cfg(CameraKind.ONVIF, "http://cam/onvif"))
    assert isinstance(onvif, OnvifSource)


def test_make_source_onvif_uses_given_resolver(fake_cv: type[_FakeCapture]) -> None:
    source = make_source(
        _cfg(CameraKind.ONVIF, "http://cam/onvif"),
        onvif_resolver=StaticOnvifResolver({"http://cam/onvif": "rtsp://cam/main"}),
    )
    source.open()
    assert fake_cv.instances[0].locator == "rtsp://cam/main"
    source.close()


def test_make_source_rejects_bad_usb_uri() -> None:
    with pytest.raises(CaptureError):
        make_source(_cfg(CameraKind.USB, "front-door"))
