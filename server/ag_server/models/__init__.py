"""SQLAlchemy models for every table in `docs/schema.md`.

Importing this package registers all tables on `Base.metadata`, which Alembic
autogenerate and the drift test compare against the migrations.
"""

from ag_server.models.base import Base
from ag_server.models.configuration import (
    PpeProfile,
    Shift,
    ShiftBreak,
    StationAssignment,
    ThresholdProfile,
    WorkstationConfig,
)
from ag_server.models.governance import (
    AppRelease,
    AuditLog,
    EvidenceDeletionItem,
    EvidenceDeletionRequest,
    IngestBatch,
    PurgeLog,
    ReportRun,
    ReportSchedule,
)
from ag_server.models.operational import (
    Alert,
    AlertDelivery,
    AttentionBucket,
    AttentionEvent,
    DeviceHealth,
    Dispute,
    EvidenceImage,
    LiveViewSession,
    ScanRecord,
    WorkerSession,
)
from ag_server.models.organization import Camera, Line, Site, Workstation
from ag_server.models.people import (
    ConsentPolicy,
    ConsentRecord,
    Role,
    SupervisorLine,
    User,
    WebSession,
)

__all__ = [
    "Alert",
    "AlertDelivery",
    "AppRelease",
    "AttentionBucket",
    "AttentionEvent",
    "AuditLog",
    "Base",
    "Camera",
    "ConsentPolicy",
    "ConsentRecord",
    "DeviceHealth",
    "Dispute",
    "EvidenceDeletionItem",
    "EvidenceDeletionRequest",
    "EvidenceImage",
    "IngestBatch",
    "Line",
    "LiveViewSession",
    "PpeProfile",
    "PurgeLog",
    "ReportRun",
    "ReportSchedule",
    "Role",
    "ScanRecord",
    "Shift",
    "ShiftBreak",
    "Site",
    "StationAssignment",
    "SupervisorLine",
    "ThresholdProfile",
    "User",
    "WebSession",
    "WorkerSession",
    "Workstation",
    "WorkstationConfig",
]
