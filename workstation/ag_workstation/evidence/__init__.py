"""Evidence capture and the local evidence store.

In memory (`docs/detection-engine.md` section 7, PLAN 1.9): `EvidenceRings`
keeps a short ring of JPEG frames per camera and the `BurstAssembler` selects
the frames of an event and hands an `EvidenceBurst` to an `EvidenceSink`
(both re-exported from `ag_workstation.engine_api`, the single definition).

On disk (PLAN 2.2, `api-contract.md` section 6): `EvidenceStore` keeps
encrypted image files under the disk policy. It is reached only through the
consent gate (`ag_workstation.consent`, PLAN 2.5).
"""

from ag_workstation.engine_api import EvidenceBurst, EvidenceFrame, EvidenceSink, FrameRole
from ag_workstation.evidence.alerts import DiskFullAlertSink, QueueDiskFullAlert
from ag_workstation.evidence.burst import AsyncSink, BurstAssembler, MemorySink
from ag_workstation.evidence.files import (
    FILE_OVERHEAD,
    EncryptedImageFiles,
    derive_evidence_key,
    image_associated_data,
)
from ag_workstation.evidence.ring import CameraRing, EvidenceRings, JpegEncoder, RingFrame
from ag_workstation.evidence.store import (
    CLEAR_FRACTION,
    DEFAULT_ALERT_MIN_INTERVAL,
    DEFAULT_MIN_FREE_MB,
    MIB,
    EvidenceCorruptError,
    EvidenceStats,
    EvidenceStore,
    EvidenceUnavailableError,
    HousekeepingSummary,
    RemovalReason,
    SaveResult,
    StoredImage,
    UploadImage,
    WithheldListener,
    WithheldRecord,
)

__all__ = [
    "CLEAR_FRACTION",
    "DEFAULT_ALERT_MIN_INTERVAL",
    "DEFAULT_MIN_FREE_MB",
    "FILE_OVERHEAD",
    "MIB",
    "AsyncSink",
    "BurstAssembler",
    "CameraRing",
    "DiskFullAlertSink",
    "EncryptedImageFiles",
    "EvidenceBurst",
    "EvidenceCorruptError",
    "EvidenceFrame",
    "EvidenceRings",
    "EvidenceSink",
    "EvidenceStats",
    "EvidenceStore",
    "EvidenceUnavailableError",
    "FrameRole",
    "HousekeepingSummary",
    "JpegEncoder",
    "MemorySink",
    "QueueDiskFullAlert",
    "RemovalReason",
    "RingFrame",
    "SaveResult",
    "StoredImage",
    "UploadImage",
    "WithheldListener",
    "WithheldRecord",
    "derive_evidence_key",
    "image_associated_data",
]
