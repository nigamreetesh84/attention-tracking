"""Builds the right `CameraSource` for a `CameraConfig`."""

import re

from ag_shared.config import CameraConfig, CameraKind

from ag_workstation.capture.onvif import OnvifResolver, OnvifSource, StubOnvifResolver
from ag_workstation.capture.types import CameraSource, CaptureError

_DEVICE = re.compile(r"(?:(?:usb|builtin):)?(\d+)")


def parse_device_index(uri: str) -> int:
    """The OpenCV device index of a USB or built-in camera URI.

    Accepts `"0"`, `"usb:1"`, `"builtin:0"` and `"builtin"` (index 0).
    """
    text = uri.strip().lower()
    if text == "builtin":
        return 0
    match = _DEVICE.fullmatch(text)
    if match is None:
        raise CaptureError("not a device index")
    return int(match.group(1))


def make_source(
    config: CameraConfig, *, onvif_resolver: OnvifResolver | None = None
) -> CameraSource:
    """A new, unopened source for `config`. Raises `CaptureError` on a bad URI."""
    # Imported here so that code which only uses fakes never loads OpenCV.
    from ag_workstation.capture.opencv_source import OpenCvSource

    match config.kind:
        case CameraKind.USB | CameraKind.BUILTIN:
            return OpenCvSource(parse_device_index(config.uri))
        case CameraKind.RTSP:
            return OpenCvSource(config.uri)
        case CameraKind.ONVIF:
            return OnvifSource(
                config.uri,
                resolver=onvif_resolver if onvif_resolver is not None else StubOnvifResolver(),
                rtsp_source_factory=OpenCvSource,
            )
