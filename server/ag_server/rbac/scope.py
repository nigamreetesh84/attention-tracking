"""Site and line scoping of queries (security-privacy 2).

Every console read or write of a scoped table goes through this module:

- `scoped_select(Model, scope)`: `select(Model)` with the scope filter; add
  your own `.where`, `.order_by` and so on.
- `get_scoped(db, Model, key, scope)`: one object by primary key, or a 404
  that looks exactly like a missing row (an out-of-scope id is not
  confirmed to exist).
- `scope_filter(Model, scope)`: the bare filter, for joins and subqueries.

Scope (built per request by `load_scope`, from the database, so a changed
site or line assignment applies on the next request):

- Admin, null `user.site_id`: everything.
- Admin with a site: that site.
- Supervisor: the assigned lines (`supervisor_line`), restricted to the
  user's site when it is set. Rows of other lines are invisible.
- Anyone else, or a permission the role lacks: nothing.

How each table is reached is declared once in `_FILTERS`; a table that is
neither there nor in `GLOBAL_MODELS` cannot be queried through this module
at all (`LookupError`), and a test requires every model to be in one of them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, TypeVar

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Select, and_, false, inspect, or_, select, true
from sqlalchemy.orm import InstrumentedAttribute, Session

from ag_server.auth.service import ConsoleSession
from ag_server.models import (
    Alert,
    AlertDelivery,
    AppRelease,
    AttentionBucket,
    AttentionEvent,
    AuditLog,
    Base,
    Camera,
    ConsentPolicy,
    ConsentRecord,
    DeviceHealth,
    Dispute,
    EvidenceDeletionItem,
    EvidenceDeletionRequest,
    EvidenceImage,
    IngestBatch,
    Line,
    LiveViewSession,
    PpeProfile,
    PurgeLog,
    ReportRun,
    ReportSchedule,
    Role,
    ScanRecord,
    Shift,
    ShiftBreak,
    Site,
    StationAssignment,
    SupervisorLine,
    ThresholdProfile,
    User,
    WebSession,
    WorkerSession,
    Workstation,
    WorkstationConfig,
)
from ag_server.rbac.permissions import GrantLevel, Permission, grant_level

M = TypeVar("M", bound=Base)


class NotFoundError(HTTPException):
    """404 for a missing or out-of-scope object; the body equals FastAPI's own 404."""

    def __init__(self) -> None:
        super().__init__(status_code=404, detail="Not Found")


@dataclass(frozen=True)
class Scope:
    """What one request may see.

    `all_sites`: no restriction. Otherwise `site_ids` are the sites in reach
    and `line_ids` is None (every line of those sites) or the exact lines.
    """

    all_sites: bool = False
    site_ids: frozenset[uuid.UUID] = frozenset()
    line_ids: frozenset[uuid.UUID] | None = None

    @classmethod
    def everything(cls) -> Scope:
        return cls(all_sites=True)

    @classmethod
    def nothing(cls) -> Scope:
        return cls(line_ids=frozenset())

    @classmethod
    def site(cls, site_id: uuid.UUID) -> Scope:
        return cls(site_ids=frozenset({site_id}))

    @classmethod
    def lines(cls, lines: Iterable[tuple[uuid.UUID, uuid.UUID]]) -> Scope:
        """From (line id, site id) pairs; no pairs means nothing."""
        pairs = list(lines)
        if not pairs:
            return cls.nothing()
        return cls(
            site_ids=frozenset(s for _, s in pairs), line_ids=frozenset(line for line, _ in pairs)
        )

    @property
    def line_restricted(self) -> bool:
        return not self.all_sites and self.line_ids is not None

    @property
    def empty(self) -> bool:
        return not self.all_sites and not self.site_ids

    def allows_site(self, site_id: uuid.UUID | None) -> bool:
        """For writes naming a site: may this request act on it?"""
        return self.all_sites or (site_id is not None and site_id in self.site_ids)

    def allows_line(self, line_id: uuid.UUID) -> bool:
        """For writes naming a line, under a line scope or everything.

        A site scope does not know which site a line belongs to: use
        `get_scoped(db, Line, line_id, scope)` instead.
        """
        if self.all_sites:
            return True
        if self.line_ids is None:
            raise ValueError("a site scope needs the line's site: use get_scoped(db, Line, ...)")
        return line_id in self.line_ids


def load_scope(db: Session, session: ConsoleSession, permission: Permission) -> Scope:
    """The scope of `permission` for the signed-in user, read from the database now.

    Runs in the caller's transaction if one is open, else in its own short one
    (so the route can then begin its own).
    """
    level = grant_level(session.role, permission)
    if level is GrantLevel.ALL:
        return Scope.everything() if session.site_id is None else Scope.site(session.site_id)
    if level is not GrantLevel.ASSIGNED_LINES:
        # "own only" is the workstation's business; no grant means no rows.
        return Scope.nothing()
    query = (
        select(Line.id, Line.site_id)
        .join(SupervisorLine, SupervisorLine.line_id == Line.id)
        .where(SupervisorLine.user_id == session.user_id)
    )
    if session.site_id is not None:
        # A line of another site assigned by mistake grants nothing.
        query = query.where(Line.site_id == session.site_id)
    if db.in_transaction():
        pairs = [(row.id, row.site_id) for row in db.execute(query)]
    else:
        with db.begin():
            pairs = [(row.id, row.site_id) for row in db.execute(query)]
    return Scope.lines(pairs)


# --- clauses -------------------------------------------------------------------------------

Column = InstrumentedAttribute[uuid.UUID] | InstrumentedAttribute[uuid.UUID | None]
Clause = ColumnElement[bool]


def _in(column: Column, ids: frozenset[uuid.UUID]) -> Clause:
    return column.in_(sorted(ids)) if ids else false()


def site_rows(column: Column, scope: Scope, *, line_scope_sees_site: bool) -> Clause:
    """Rows owned by a site. Line-scoped users see them only where the table says so."""
    if scope.all_sites:
        return true()
    if scope.line_restricted and not line_scope_sees_site:
        return false()
    return _in(column, scope.site_ids)


def line_rows(column: Column, scope: Scope) -> Clause:
    if scope.all_sites:
        return true()
    if scope.line_ids is None:
        if not scope.site_ids:
            return false()
        return column.in_(select(Line.id).where(_in(Line.site_id, scope.site_ids)))
    return _in(column, scope.line_ids)


def workstation_rows(column: Column, scope: Scope) -> Clause:
    if scope.all_sites:
        return true()
    if scope.empty:
        return false()
    return column.in_(select(Workstation.id).where(line_rows(Workstation.line_id, scope)))


def event_rows(column: Column, scope: Scope) -> Clause:
    if scope.all_sites:
        return true()
    if scope.empty:
        return false()
    return column.in_(
        select(AttentionEvent.id).where(workstation_rows(AttentionEvent.workstation_id, scope))
    )


def _user_rows(column: Column, scope: Scope, *, line_scope_sees_site: bool) -> Clause:
    if scope.all_sites:
        return true()
    return column.in_(
        select(User.id).where(
            site_rows(User.site_id, scope, line_scope_sees_site=line_scope_sees_site)
        )
    )


def _alert(scope: Scope) -> Clause:
    if scope.all_sites:
        return true()
    if scope.line_restricted:
        # Site-wide alerts (no workstation) are for site-wide roles.
        return and_(
            Alert.workstation_id.is_not(None),
            workstation_rows(Alert.workstation_id, scope),
            _in(Alert.site_id, scope.site_ids),
        )
    return _in(Alert.site_id, scope.site_ids)


def _shift(scope: Scope) -> Clause:
    if scope.all_sites:
        return true()
    if scope.line_restricted:
        return or_(
            line_rows(Shift.line_id, scope),
            and_(Shift.line_id.is_(None), _in(Shift.site_id, scope.site_ids)),
        )
    return _in(Shift.site_id, scope.site_ids)


def _ppe(scope: Scope) -> Clause:
    if scope.all_sites:
        return true()
    if scope.empty:
        return false()
    # Built-in profiles (no site) are shared reference data.
    return or_(PpeProfile.site_id.is_(None), _in(PpeProfile.site_id, scope.site_ids))


def _deletion_request(scope: Scope) -> Clause:
    return site_rows(EvidenceDeletionRequest.site_id, scope, line_scope_sees_site=False)


def _report_schedule(scope: Scope) -> Clause:
    return site_rows(ReportSchedule.site_id, scope, line_scope_sees_site=False)


def _report_run(scope: Scope) -> Clause:
    if scope.all_sites:
        return true()
    # An ad hoc run has no site (schema.md; PROGRESS 3.2 proposal 3): all-sites only.
    return ReportRun.report_schedule_id.in_(
        select(ReportSchedule.id).where(_report_schedule(scope))
    )


def _on_workstation(model: type[Base]) -> Callable[[Scope], Clause]:
    column: Column = getattr(model, "workstation_id")  # noqa: B009 - typed per model below
    return lambda scope: workstation_rows(column, scope)


def _on_event(model: type[Base]) -> Callable[[Scope], Clause]:
    column: Column = getattr(model, "attention_event_id")  # noqa: B009 - typed per model below
    return lambda scope: event_rows(column, scope)


_FILTERS: Final[Mapping[type[Base], Callable[[Scope], Clause]]] = MappingProxyType(
    {
        # Organization
        Site: lambda s: site_rows(Site.id, s, line_scope_sees_site=True),
        Line: lambda s: line_rows(Line.id, s),
        Workstation: lambda s: line_rows(Workstation.line_id, s),
        Camera: _on_workstation(Camera),
        # People
        User: lambda s: site_rows(User.site_id, s, line_scope_sees_site=True),
        SupervisorLine: lambda s: line_rows(SupervisorLine.line_id, s),
        WebSession: lambda s: _user_rows(WebSession.user_id, s, line_scope_sees_site=False),
        ConsentPolicy: lambda s: site_rows(ConsentPolicy.site_id, s, line_scope_sees_site=True),
        ConsentRecord: lambda s: _user_rows(ConsentRecord.user_id, s, line_scope_sees_site=False),
        # Configuration
        PpeProfile: _ppe,
        ThresholdProfile: lambda s: site_rows(
            ThresholdProfile.site_id, s, line_scope_sees_site=True
        ),
        Shift: _shift,
        ShiftBreak: lambda s: ShiftBreak.shift_id.in_(select(Shift.id).where(_shift(s))),
        WorkstationConfig: _on_workstation(WorkstationConfig),
        StationAssignment: _on_workstation(StationAssignment),
        # Operational
        WorkerSession: _on_workstation(WorkerSession),
        ScanRecord: _on_workstation(ScanRecord),
        AttentionEvent: _on_workstation(AttentionEvent),
        EvidenceImage: _on_event(EvidenceImage),
        AttentionBucket: _on_workstation(AttentionBucket),
        Dispute: _on_event(Dispute),
        Alert: _alert,
        AlertDelivery: lambda s: AlertDelivery.alert_id.in_(select(Alert.id).where(_alert(s))),
        DeviceHealth: _on_workstation(DeviceHealth),
        LiveViewSession: _on_workstation(LiveViewSession),
        # Governance
        EvidenceDeletionRequest: _deletion_request,
        EvidenceDeletionItem: lambda s: EvidenceDeletionItem.request_id.in_(
            select(EvidenceDeletionRequest.id).where(_deletion_request(s))
        ),
        AuditLog: lambda s: site_rows(AuditLog.site_id, s, line_scope_sees_site=False),
        PurgeLog: lambda s: site_rows(PurgeLog.site_id, s, line_scope_sees_site=False),
        IngestBatch: _on_workstation(IngestBatch),
        ReportSchedule: _report_schedule,
        ReportRun: _report_run,
    }
)

# Tables with no site or line: readable by anyone whose route permission allows.
GLOBAL_MODELS: Final[frozenset[type[Base]]] = frozenset({Role, AppRelease})
SCOPED_MODELS: Final[frozenset[type[Base]]] = frozenset(_FILTERS)


def scope_filter(model: type[Base], scope: Scope) -> Clause:
    """The WHERE clause limiting `model` to `scope`. LookupError for an unknown table."""
    if model in GLOBAL_MODELS:
        return true()
    build = _FILTERS.get(model)
    if build is None:
        raise LookupError(f"{getattr(model, '__name__', model)!s} has no scope rule")
    return build(scope)


def scoped_select(model: type[M], scope: Scope) -> Select[M]:
    """`select(model)` limited to `scope`. Route code uses this instead of `select`."""
    return select(model).where(scope_filter(model, scope))


def get_scoped(db: Session, model: type[M], key: object, scope: Scope) -> M:
    """The row with primary key `key` if it is in scope, else `NotFoundError` (404).

    `key` is a scalar or, for composite keys, a tuple in primary-key order.
    Runs in the caller's transaction.
    """
    columns = inspect(model).primary_key
    values = key if isinstance(key, tuple) else (key,)
    if len(values) != len(columns):
        raise NotFoundError
    query = scoped_select(model, scope).where(
        *(column == value for column, value in zip(columns, values, strict=True))
    )
    found = db.execute(query).scalar_one_or_none()
    if found is None:
        raise NotFoundError
    return found
