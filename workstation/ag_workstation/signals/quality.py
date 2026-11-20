"""The `quality` signal: can this frame be trusted at all?

Each available factor scores 0-1 and the weakest one is the quality, because
one bad factor (too dark, too small, too blurred) is enough to make the
landmarks unreliable.
"""

from ag_workstation.landmarks.types import FaceObservation, RegionStats
from ag_workstation.signals.geometry import clamp, ramp
from ag_workstation.signals.params import ExtractionParams


def image_score(stats: RegionStats, params: ExtractionParams) -> float:
    """Brightness (not too dark, not washed out) and sharpness of a region."""
    not_dark = ramp(stats.brightness, params.brightness_dark_lo, params.brightness_dark_hi)
    not_bright = 1.0 - ramp(
        stats.brightness, params.brightness_bright_lo, params.brightness_bright_hi
    )
    sharp = ramp(stats.sharpness, params.sharpness_lo, params.sharpness_hi)
    return min(not_dark, not_bright, sharp)


def frame_quality(stats: RegionStats | None, params: ExtractionParams) -> float | None:
    """Quality of a frame as a whole; None when it was not measured."""
    return None if stats is None else image_score(stats, params)


def stability_score(previous_width: float, width: float, params: ExtractionParams) -> float:
    """1 when the face size is steady between two frames, 0 when it jumps."""
    if previous_width <= 0 or params.stability_max_change <= 0:
        return 1.0
    change = abs(width - previous_width) / previous_width
    return 1.0 - clamp(change / params.stability_max_change, 0.0, 1.0)


def face_quality(
    face: FaceObservation,
    frame_stats: RegionStats | None,
    stability: float | None,
    params: ExtractionParams,
) -> float:
    """Quality of a detected face: size, light, blur, confidence, stability.

    Light and blur come from the face region, or from the whole frame when
    the face region was not measured. Factors that are not available are
    left out.
    """
    factors = [ramp(face.box.width, params.face_width_min, params.face_width_good)]
    stats = face.stats if face.stats is not None else frame_stats
    if stats is not None:
        factors.append(image_score(stats, params))
    if face.confidence is not None:
        factors.append(clamp(face.confidence, 0.0, 1.0))
    if stability is not None:
        factors.append(stability)
    return min(factors)
