"""Evidence capture in memory (`docs/detection-engine.md` section 7).

`EvidenceRings` keeps a short ring of JPEG frames per camera; the
`BurstAssembler` selects the frames of an event and hands an `EvidenceBurst`
to an `EvidenceSink`. Nothing in this package writes to disk: the consent
gate and the encrypted local store (tasks 2.2 and 2.5) sit behind the sink.
"""

from ag_workstation.evidence.burst import (
    BurstAssembler,
    BurstImage,
    EvidenceBurst,
    EvidenceSink,
    MemorySink,
)
from ag_workstation.evidence.ring import CameraRing, EvidenceRings, JpegEncoder, RingFrame

__all__ = [
    "BurstAssembler",
    "BurstImage",
    "CameraRing",
    "EvidenceBurst",
    "EvidenceRings",
    "EvidenceSink",
    "JpegEncoder",
    "MemorySink",
    "RingFrame",
]
