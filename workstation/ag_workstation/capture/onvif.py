"""ONVIF discovery, as an interface stub that resolves to an RTSP URI.

Release 1 has no ONVIF client. `StubOnvifResolver` accepts a URI that is
already RTSP; anything else is unavailable (the camera then shows offline).
A real resolver (WS-Discovery + GetStreamUri) can be dropped in later.
"""

from collections.abc import Callable, Mapping
from typing import Protocol

from ag_workstation.capture.types import CameraSource, CaptureError, Image

_RTSP_SCHEMES = ("rtsp://", "rtsps://")


class OnvifUnavailableError(CaptureError):
    """An ONVIF device could not be resolved to a stream URI."""


class OnvifResolver(Protocol):
    """Resolves an ONVIF device address to its RTSP stream URI."""

    def resolve(self, device_uri: str) -> str:
        """The RTSP URI; raises `OnvifUnavailableError` when unknown."""
        ...


class StubOnvifResolver:
    """Passes RTSP URIs through; cannot resolve real ONVIF addresses."""

    def resolve(self, device_uri: str) -> str:
        """`device_uri` if it is already RTSP."""
        if device_uri.strip().lower().startswith(_RTSP_SCHEMES):
            return device_uri
        raise OnvifUnavailableError("ONVIF discovery is not available in this release")


class StaticOnvifResolver:
    """Resolves from a fixed device-to-RTSP table (tests, manual setup)."""

    def __init__(self, table: Mapping[str, str]) -> None:
        self._table = dict(table)

    def resolve(self, device_uri: str) -> str:
        """The table entry for `device_uri`."""
        try:
            return self._table[device_uri]
        except KeyError:
            raise OnvifUnavailableError("ONVIF device not in table") from None


class OnvifSource:
    """A source that resolves its RTSP URI at each `open`, then delegates."""

    def __init__(
        self,
        device_uri: str,
        *,
        resolver: OnvifResolver,
        rtsp_source_factory: Callable[[str], CameraSource],
    ) -> None:
        self._device_uri = device_uri
        self._resolver = resolver
        self._rtsp_factory = rtsp_source_factory
        self._inner: CameraSource | None = None

    def __repr__(self) -> str:
        return "OnvifSource(<uri hidden>)"

    def open(self) -> None:
        """Resolve the stream URI and open it."""
        uri = self._resolver.resolve(self._device_uri)
        inner = self._rtsp_factory(uri)
        inner.open()
        self._inner = inner

    def read(self) -> Image | None:
        """Read from the resolved stream."""
        if self._inner is None:
            raise CaptureError("source is not open")
        return self._inner.read()

    def close(self) -> None:
        """Close the resolved stream, if any."""
        inner, self._inner = self._inner, None
        if inner is not None:
            inner.close()
