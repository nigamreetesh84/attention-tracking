"""Consent and capture gate (PLAN 2.5): the only path from a frame to disk."""

from ag_workstation.consent.breaks import break_intervals, on_break
from ag_workstation.consent.decision import GateDecision, GateReason, images_withheld_for
from ag_workstation.consent.gate import BundleSource, CaptureGate, SessionSource
from ag_workstation.consent.permit import CapturePermit, PermitError
from ag_workstation.consent.sink import (
    CaptureOutcome,
    GatedEvidenceSink,
    PermittedBurstStore,
    SavedBurst,
)

__all__ = [
    "BundleSource",
    "CaptureGate",
    "CaptureOutcome",
    "CapturePermit",
    "GateDecision",
    "GateReason",
    "GatedEvidenceSink",
    "PermitError",
    "PermittedBurstStore",
    "SavedBurst",
    "SessionSource",
    "break_intervals",
    "images_withheld_for",
    "on_break",
]
