"""Server-only enums for the database (`docs/schema.md`).

Enums the workstation also uses live in `ag_shared` and are reused here, not
copied: `AttentionState`, `ScanOutcome` (states), `CameraRole`, `CameraKind`
(config), `PpeProfileKind` (ppe), `SessionMethod`, `SessionEndReason`,
`AttentionEventKind`, `ImagesWithheld`, `DisputeReasonCode` (api.events).
"""

from __future__ import annotations

from enum import StrEnum


class WorkstationStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


class ConsentStatus(StrEnum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class ConsentMethod(StrEnum):
    DIGITAL_SIGNATURE = "digital_signature"
    CAPTURED_ACKNOWLEDGMENT = "captured_acknowledgment"


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"


class RemovedReason(StrEnum):
    RETENTION = "retention"
    APPROVED_DELETION = "approved_deletion"
    CONSENT_WITHDRAWAL = "consent_withdrawal"


class DisputeStatus(StrEnum):
    OPEN = "open"
    UPHELD = "upheld"
    REJECTED = "rejected"


class AlertKind(StrEnum):
    SLEEPING = "sleeping"
    ABSENT = "absent"
    REPEATED_WARNINGS = "repeated_warnings"
    CAMERA_OFFLINE = "camera_offline"
    MONITORING_UNAVAILABLE = "monitoring_unavailable"
    WORKSTATION_OFFLINE = "workstation_offline"
    ENGINE_RESTART = "engine_restart"
    DISK_FULL = "disk_full"
    CLOCK_DRIFT = "clock_drift"
    SERVER_HEALTH = "server_health"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    CLOSED = "closed"


class DeliveryChannel(StrEnum):
    DASHBOARD = "dashboard"
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class LiveViewMode(StrEnum):
    STILL = "still"
    VIDEO = "video"


class DeletionRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


class PurgeKind(StrEnum):
    IMAGES = "images"
    METADATA = "metadata"
    AUDIT = "audit"
    CONSENT_WITHDRAWAL = "consent_withdrawal"


class ReportFormat(StrEnum):
    XLSX = "xlsx"
    PDF = "pdf"


class ReleaseChannel(StrEnum):
    PILOT = "pilot"
    STABLE = "stable"
