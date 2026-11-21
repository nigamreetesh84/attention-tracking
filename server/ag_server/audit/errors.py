"""Audit service errors. Messages never contain detail values."""

from __future__ import annotations


class AuditError(Exception):
    """An audit entry could not be written (invalid entry or unsafe transaction)."""


class AuditDetailError(AuditError):
    """The detail holds a value that cannot be stored as audit JSON."""
