"""JPEG encoding for the evidence ring (capture layer: may import OpenCV)."""

from ag_workstation.capture.types import CaptureError, Image


def encode_jpeg(image: Image, quality: int) -> bytes:
    """Encode a BGR image as JPEG at `quality` (1-100)."""
    import cv2  # local import: importing the capture package must not load OpenCV

    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise CaptureError("JPEG encoding failed")
    return bytes(buffer.tobytes())
