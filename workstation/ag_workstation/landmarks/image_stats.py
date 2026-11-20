"""Image-derived numbers for quality and glare (NumPy only, no OpenCV).

These are the only places where pixels are read after capture. Each returns
one or two plain numbers; no pixels leave this module.
"""

import numpy as np
import numpy.typing as npt

from ag_workstation.capture.types import Image
from ag_workstation.landmarks.types import Box, EyePoints, RegionStats

_MAX_SIDE = 160
"""Regions are subsampled to about this many pixels per side: enough for a
brightness and blur estimate and cheap at 15 fps."""
_MIN_SIDE = 3
HIGHLIGHT_LEVEL = 240
"""A pixel is a specular highlight when all three channels reach this."""


def _crop(image: Image, box: Box | None) -> npt.NDArray[np.uint8] | None:
    """The (subsampled) pixels of `box`, or None when the region is too small."""
    if image.ndim != 3 or image.shape[2] != 3:
        return None
    height, width = image.shape[:2]
    if box is None:
        region = image
    else:
        x0 = max(0, min(width, round(box.x_min * width)))
        x1 = max(0, min(width, round(box.x_max * width)))
        y0 = max(0, min(height, round(box.y_min * height)))
        y1 = max(0, min(height, round(box.y_max * height)))
        region = image[y0:y1, x0:x1]
    if region.shape[0] < _MIN_SIDE or region.shape[1] < _MIN_SIDE:
        return None
    step_y = max(1, region.shape[0] // _MAX_SIDE)
    step_x = max(1, region.shape[1] // _MAX_SIDE)
    return region[::step_y, ::step_x]


def region_stats(image: Image, box: Box | None) -> RegionStats | None:
    """Brightness and sharpness of `box` (the whole image when None)."""
    region = _crop(image, box)
    if region is None:
        return None
    pixels = region.astype(np.float32)
    # BGR order, ITU-R BT.601 weights.
    luma = 0.114 * pixels[:, :, 0] + 0.587 * pixels[:, :, 1] + 0.299 * pixels[:, :, 2]
    laplacian = (
        4.0 * luma[1:-1, 1:-1] - luma[:-2, 1:-1] - luma[2:, 1:-1] - luma[1:-1, :-2] - luma[1:-1, 2:]
    )
    return RegionStats(
        brightness=float(luma.mean()) / 255.0,
        sharpness=float(laplacian.var()),
    )


def highlight_fraction(image: Image, box: Box) -> float | None:
    """Share of `box` covered by near-white (specular) pixels, 0-1."""
    region = _crop(image, box)
    if region is None:
        return None
    return float((region.min(axis=2) >= HIGHLIGHT_LEVEL).mean())


def eye_region(eye: EyePoints, aspect: float) -> Box:
    """The area around one eye where a lens reflection hides it.

    Wider than the eye contour (1.2 eye lengths) and 0.8 eye lengths high, so
    a reflection on the lens just above or below the lids is seen as well.
    """
    xs = [p.x for p in eye]
    ys = [p.y for p in eye]
    length = max(xs) - min(xs)
    cx = (max(xs) + min(xs)) / 2
    cy = (max(ys) + min(ys)) / 2
    half_w = 0.6 * length
    half_h = 0.4 * length * aspect
    return Box(
        x_min=max(0.0, cx - half_w),
        y_min=max(0.0, cy - half_h),
        x_max=min(1.0, cx + half_w),
        y_max=min(1.0, cy + half_h),
    )
