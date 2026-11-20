"""Tuning constants of the signal extractors.

These sit below the detection thresholds of `ag_shared.thresholds` (which
the scope defines and the server distributes): they say how a raw landmark
measurement becomes a signal. The defaults are starting points to be tuned
on the labeled video set and in the pilot.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExtractionParams:
    """How landmarks become signals. All lengths are relative, angles in degrees."""

    # --- eyes ---
    ear_closed_ratio: float = 0.7
    """Eyes are closed when EAR falls below this share of the open-eye baseline."""
    ear_baseline_min: float = 0.15
    ear_baseline_max: float = 0.45
    """The baseline is kept inside this range of raw EAR values."""
    ear_warmup_s: float = 5.0
    ear_warmup_min_samples: int = 30
    """A session's baseline is first taken from this much valid eye data."""
    ear_adapt_up: float = 0.1
    ear_adapt_down: float = 0.01
    """After warm-up the baseline follows open-eye frames: quickly upwards,
    slowly downwards. Closed-eye frames never move it."""
    use_blink_blendshape: bool = True
    blink_closed_score: float = 0.6
    """Mean blink blendshape score at or above which the eyes count as closed."""
    glare_max_fraction: float = 0.05
    """Share of an eye region covered by highlights from which the eye is
    unreadable (profiles with glare tolerance only)."""

    # --- PERCLOS and blink rate ---
    perclos_min_valid_share: float = 0.5
    """PERCLOS needs at least this share of valid frames in its window."""
    perclos_min_fill: float = 0.5
    """PERCLOS needs frames spanning at least this share of its window."""
    blink_window_s: float = 60.0
    blink_min_span_s: float = 20.0
    blink_min_valid_share: float = 0.5

    # --- mouth ---
    mar_yawn: float = 0.6
    """Mouth aspect ratio above which the mouth is open as in a yawn."""
    yawn_min_s: float = 1.5
    """The mouth must stay open for more than this to count as a yawn."""

    # --- head ---
    head_drop_deg: float = 25.0
    """The head is dropped when pitch is below minus this angle."""
    nod_drop_deg: float = 15.0
    nod_recover_deg: float = 5.0
    nod_max_s: float = 3.0
    nod_ref_window_s: float = 4.0
    """A nod: pitch falls `nod_drop_deg` below its recent level and returns to
    within `nod_recover_deg` of it inside `nod_max_s`."""

    # --- body ---
    posture_upright_ratio: float = 0.6
    """Nose height above the shoulder line, in shoulder widths, when upright."""
    pose_min_visibility: float = 0.5
    motion_window_s: float = 2.0
    motion_min_fill: float = 0.5
    max_frame_gap_s: float = 1.0
    """Frames further apart than this are not compared (motion, stability)."""

    # --- quality ---
    face_width_min: float = 0.04
    face_width_good: float = 0.10
    """Face width as a share of the image width: unusable below the first,
    full score from the second."""
    brightness_dark_lo: float = 0.08
    brightness_dark_hi: float = 0.20
    brightness_bright_lo: float = 0.90
    brightness_bright_hi: float = 0.98
    sharpness_lo: float = 5.0
    sharpness_hi: float = 30.0
    stability_max_change: float = 0.5
    """Relative change of the face width between two frames that scores zero."""
