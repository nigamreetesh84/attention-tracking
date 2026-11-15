"""Camera capture layer (detection-engine.md section 1).

`CaptureManager` runs one `CameraWorker` thread per enabled camera; each
publishes timestamped `Frame`s into a latest-frame-wins `LatestFrame` and
reports `CameraHealth`. Sources implement `CameraSource`: `OpenCvSource`
(USB, built-in, RTSP) and `FileSource` live in `opencv_source`, the only
module here that imports OpenCV, and are not re-exported so that importing
this package does not load OpenCV.
"""

from ag_workstation.capture.backoff import Backoff
from ag_workstation.capture.factory import make_source, parse_device_index
from ag_workstation.capture.fake import FakeCamera, FakeSource
from ag_workstation.capture.latest import LatestFrame
from ag_workstation.capture.manager import CaptureManager
from ag_workstation.capture.onvif import (
    OnvifResolver,
    OnvifSource,
    OnvifUnavailableError,
    StaticOnvifResolver,
    StubOnvifResolver,
)
from ag_workstation.capture.types import (
    CameraHealth,
    CameraSource,
    CameraStatus,
    CaptureError,
    EndOfStream,
    Frame,
    Image,
    SourceFactory,
)
from ag_workstation.capture.worker import CameraWorker

__all__ = [
    "Backoff",
    "CameraHealth",
    "CameraSource",
    "CameraStatus",
    "CameraWorker",
    "CaptureError",
    "CaptureManager",
    "EndOfStream",
    "FakeCamera",
    "FakeSource",
    "Frame",
    "Image",
    "LatestFrame",
    "OnvifResolver",
    "OnvifSource",
    "OnvifUnavailableError",
    "SourceFactory",
    "StaticOnvifResolver",
    "StubOnvifResolver",
    "make_source",
    "parse_device_index",
]
