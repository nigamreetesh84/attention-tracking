"""Landmark data handed from the landmark layer to the signal extractors.

An observation keeps only the few points the signals in
`docs/detection-engine.md` section 2 are computed from (eye contours, inner
mouth, three anchor points, nose and shoulders), plus plain numbers. The face
mesh is not kept, and nothing here is a face template, an embedding or any
other identity feature. Observations live for one frame and are never stored.

Coordinates are normalized image coordinates: x and y in 0-1, origin top
left, x in units of the image width and y in units of the image height.
"""

from dataclasses import dataclass
from typing import NamedTuple, Protocol

from ag_workstation.capture.types import Frame


class LandmarkError(Exception):
    """The landmark models could not be loaded or run.

    The engine turns this into an Unknown state; it never reaches the scan
    path.
    """


class Point(NamedTuple):
    """A point in normalized image coordinates."""

    x: float
    y: float


class Box(NamedTuple):
    """An axis-aligned box in normalized image coordinates."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def width(self) -> float:
        """Width as a share of the image width."""
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        """Height as a share of the image height."""
        return self.y_max - self.y_min

    @property
    def area(self) -> float:
        """Share of the image area."""
        return self.width * self.height

    @property
    def center(self) -> Point:
        """The middle of the box."""
        return Point((self.x_min + self.x_max) / 2, (self.y_min + self.y_max) / 2)


EyePoints = tuple[Point, Point, Point, Point, Point, Point]
"""Eye contour p1..p6: outer corner, two upper lid points, inner corner, then
the two lower lid points under the upper ones in reverse order (p2 is above
p6, p3 above p5)."""


@dataclass(frozen=True, slots=True)
class MouthPoints:
    """Inner lip contour: the corners and three upper/lower pairs."""

    left: Point
    right: Point
    upper: tuple[Point, Point, Point]
    lower: tuple[Point, Point, Point]
    """`lower[i]` is below `upper[i]`."""


@dataclass(frozen=True, slots=True)
class HeadPose:
    """Head rotation relative to the camera, in degrees (not yet calibrated).

    Yaw is positive when the nose points to the right of the image, pitch is
    positive when it points up (negative: head down), roll is rotation in
    the image plane.
    """

    yaw_deg: float
    pitch_deg: float
    roll_deg: float


@dataclass(frozen=True, slots=True)
class RegionStats:
    """Image statistics of a region, reduced to two numbers."""

    brightness: float
    """Mean luma, 0 (black) to 1 (white)."""
    sharpness: float
    """Variance of the Laplacian of the luma (0-255 scale); low means blur."""


@dataclass(frozen=True, slots=True)
class FaceObservation:
    """One detected face, reduced to what the signals need."""

    box: Box
    right_eye: EyePoints
    """The worker's right eye (on the left of an unmirrored image)."""
    left_eye: EyePoints
    mouth: MouthPoints
    anchors: tuple[Point, ...]
    """A few rigid points (nose tip, chin, forehead) used for motion energy."""
    head_pose: HeadPose | None = None
    blink_right: float | None = None
    """Blink blendshape score 0-1 of the right eye, when the model gives one."""
    blink_left: float | None = None
    confidence: float | None = None
    """Detection confidence 0-1, when the provider has one."""
    stats: RegionStats | None = None
    """Brightness and sharpness of the face region."""
    glare_right: float | None = None
    """Share of the right eye region covered by specular highlights, 0-1."""
    glare_left: float | None = None


@dataclass(frozen=True, slots=True)
class PosePoint:
    """A body landmark with the model's visibility estimate (0-1)."""

    x: float
    y: float
    visibility: float = 1.0

    @property
    def point(self) -> Point:
        """The position without the visibility."""
        return Point(self.x, self.y)


@dataclass(frozen=True, slots=True)
class PoseObservation:
    """One detected upper body: nose and shoulders only."""

    nose: PosePoint
    left_shoulder: PosePoint
    right_shoulder: PosePoint


@dataclass(frozen=True, slots=True)
class LandmarkResult:
    """Everything the landmark layer found in one frame."""

    camera_slot: int
    ts: float
    """The frame's `ts` (seconds on the engine clock)."""
    width: int
    height: int
    faces: tuple[FaceObservation, ...]
    """All detected faces; the ROI filter picks the worker's."""
    poses: tuple[PoseObservation, ...] | None = None
    """None when the pose model was not run for this frame."""
    frame_stats: RegionStats | None = None
    """Brightness and sharpness of the whole frame."""

    @property
    def aspect(self) -> float:
        """Image width over height; turns x distances into y units."""
        return self.width / self.height if self.height > 0 else 1.0


class LandmarkProvider(Protocol):
    """Finds face and pose landmarks in a frame.

    One provider serves one camera (implementations may track between
    frames). `detect` may raise `LandmarkError`; the caller treats that as
    an unusable frame. Implementations are the only code allowed to import
    MediaPipe.
    """

    def detect(self, frame: Frame, *, with_pose: bool = False) -> LandmarkResult:
        """Landmarks of `frame`; the pose model runs only when `with_pose`."""
        ...

    def close(self) -> None:
        """Release the models. Safe to call more than once."""
        ...
