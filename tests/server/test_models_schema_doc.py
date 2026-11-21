"""The SQLAlchemy models match `docs/schema.md` (no database needed).

The doc is the source of truth: every table it describes has a model, no model
invents a table or a column the doc does not name, every enum column is a
PostgreSQL enum named `<table>_<column>` and its values are the doc's values.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ag_server.models import Base
from sqlalchemy import Enum as SaEnum
from sqlalchemy import Table

SCHEMA_MD = Path(__file__).resolve().parents[2] / "docs" / "schema.md"

# Backticked words in a table's paragraph that are neither columns nor enum
# values: type and model names, role names, settings keys and similar prose.
NOT_COLUMNS: dict[str, set[str]] = {
    "site": {"SiteSettings"},
    "role": {"admin", "supervisor", "workstation_user"},
    "threshold_profile": {"Thresholds"},
    "user": set(),
    "consent_record": {"granted"},
    "attention_bucket": {"unknown", "on_break"},
    "audit_log": {
        "evidence.view",
        "hash = sha256(prev_hash || canonical_row)",
        "retention_audit_days",
    },
    "evidence_deletion_request": {"decided_by <> requested_by"},
    "alert": {"status <> 'closed'"},
    "attention_event": set(),
}


def _doc_paragraphs() -> dict[str, str]:
    """`**table**: ...` paragraphs of the "Tables" section, keyed by table name."""
    text = SCHEMA_MD.read_text(encoding="utf-8")
    tables_section = text.split("## Tables", 1)[1].split("## Scope entity mapping", 1)[0]
    paragraphs: dict[str, str] = {}
    for block in re.split(r"\n\s*\n", tables_section):
        match = re.match(r"\*\*(\w+)\*\*:", block.strip())
        if match:
            paragraphs[match.group(1)] = block
    return paragraphs


DOC = _doc_paragraphs()


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _backticked(paragraph: str) -> set[str]:
    return set(re.findall(r"`([^`]+)`", paragraph))


def test_doc_parsing_finds_the_known_tables() -> None:
    # Guards the parser itself: a doc reformat must not make the checks vacuous.
    assert len(DOC) == 34
    assert {"site", "camera", "attention_event", "audit_log", "app_release"} <= set(DOC)


def test_every_doc_table_has_a_model_and_no_model_is_undocumented() -> None:
    assert set(Base.metadata.tables) == set(DOC)


@pytest.mark.parametrize("table_name", sorted(DOC))
def test_model_columns_are_all_named_in_the_doc(table_name: str) -> None:
    named = _backticked(DOC[table_name])
    invented = {c.name for c in _table(table_name).columns} - named
    assert not invented, f"{table_name}: columns not in schema.md: {sorted(invented)}"


@pytest.mark.parametrize("table_name", sorted(DOC))
def test_doc_columns_all_exist_in_the_model(table_name: str) -> None:
    table = _table(table_name)
    enum_values: set[str] = set()
    for column in table.columns:
        if isinstance(column.type, SaEnum):
            enum_values |= set(column.type.enums)
    prose = NOT_COLUMNS.get(table_name, set()) | set(DOC)  # mentions of other tables
    words = _backticked(DOC[table_name]) - enum_values - prose
    missing = {w for w in words if w not in table.columns}
    assert not missing, f"{table_name}: schema.md columns missing from model: {sorted(missing)}"


def _enum_columns() -> list[tuple[str, str, SaEnum]]:
    found = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, SaEnum):
                found.append((table.name, column.name, column.type))
    return found


def test_enums_are_native_and_named_table_column() -> None:
    enums = _enum_columns()
    assert len(enums) == 28
    for table_name, column_name, enum in enums:
        assert enum.native_enum, (table_name, column_name)
        assert enum.name == f"{table_name}_{column_name}", (table_name, column_name)


# Values copied from docs/schema.md; the order is the doc's order.
EXPECTED_ENUMS: dict[tuple[str, str], list[str]] = {
    ("workstation", "status"): ["pending", "active", "revoked"],
    ("camera", "role"): ["primary", "secondary"],
    ("camera", "kind"): ["usb", "rtsp", "onvif", "builtin"],
    ("consent_record", "status"): ["granted", "withdrawn"],
    ("consent_record", "method"): ["digital_signature", "captured_acknowledgment"],
    ("ppe_profile", "kind"): [
        "open_face",
        "clear_glasses",
        "sunglasses",
        "mask",
        "helmet_shield",
        "custom",
    ],
    ("station_assignment", "status"): ["pending", "applied", "superseded", "expired"],
    ("worker_session", "method"): ["employee_id_pin", "supervisor_assigned"],
    ("worker_session", "end_reason"): [
        "logout",
        "timeout",
        "shift_end",
        "replaced",
        "assignment_expired",
        "supervisor_ended",
    ],
    ("scan_record", "outcome"): ["attentive", "warned", "unmonitored"],
    ("attention_event", "kind"): ["scan_warning", "state_episode"],
    ("attention_event", "images_withheld"): [
        "none",
        "no_consent",
        "no_worker",
        "on_break",
        "disk_full",
    ],
    ("attention_event", "review_status"): ["pending", "confirmed", "false_positive"],
    ("evidence_image", "removed_reason"): [
        "retention",
        "approved_deletion",
        "consent_withdrawal",
    ],
    ("dispute", "reason"): ["was_attentive", "camera_view", "on_break", "other_task", "other"],
    ("dispute", "status"): ["open", "upheld", "rejected"],
    ("alert", "kind"): [
        "sleeping",
        "absent",
        "repeated_warnings",
        "camera_offline",
        "monitoring_unavailable",
        "workstation_offline",
        "engine_restart",
        "disk_full",
        "clock_drift",
        "server_health",
    ],
    ("alert", "status"): ["open", "acknowledged", "closed"],
    ("alert_delivery", "channel"): ["dashboard", "email", "sms", "whatsapp"],
    ("alert_delivery", "status"): ["pending", "sent", "failed"],
    ("live_view_session", "mode"): ["still", "video"],
    ("evidence_deletion_request", "status"): [
        "pending",
        "approved",
        "rejected",
        "executed",
        "cancelled",
    ],
    ("purge_log", "kind"): ["images", "metadata", "audit", "consent_withdrawal"],
    ("report_schedule", "format"): ["xlsx", "pdf"],
    ("app_release", "channel"): ["pilot", "stable"],
}

ATTENTION_STATES = [
    "attentive",
    "distracted",
    "drowsy",
    "sleeping",
    "absent",
    "unknown",
    "on_break",
]
for _state_column in (
    ("workstation", "current_state"),
    ("scan_record", "state_at_scan"),
    ("attention_event", "state"),
):
    EXPECTED_ENUMS[_state_column] = ATTENTION_STATES


@pytest.mark.parametrize(("table_name", "column_name"), sorted(EXPECTED_ENUMS))
def test_enum_values_match_the_doc(table_name: str, column_name: str) -> None:
    column_type = _table(table_name).columns[column_name].type
    assert isinstance(column_type, SaEnum)
    assert list(column_type.enums) == EXPECTED_ENUMS[(table_name, column_name)]


def test_every_enum_column_is_covered_by_the_expectations() -> None:
    assert {(t, c) for t, c, _ in _enum_columns()} == set(EXPECTED_ENUMS)


def test_audit_and_purge_logs_use_bigserial_ids_and_no_foreign_keys() -> None:
    for name in ("audit_log", "purge_log"):
        table = _table(name)
        (pk,) = table.primary_key.columns
        assert pk.name == "id"
        assert pk.type.python_type is int
        assert pk.autoincrement is True
        assert not table.foreign_keys, name


CLIENT_ID_TABLES = ("worker_session", "scan_record", "attention_event", "evidence_image", "dispute")


@pytest.mark.parametrize("table_name", CLIENT_ID_TABLES)
def test_device_originated_rows_take_the_client_id(table_name: str) -> None:
    pk = _table(table_name).columns["id"]
    assert pk.primary_key
    # No default of any kind: the client-generated UUIDv7 is the idempotency key.
    assert pk.default is None
    assert pk.server_default is None


def test_timestamps_are_timezone_aware() -> None:
    from sqlalchemy import DateTime

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone, f"{table.name}.{column.name}"
