"""Audit log service (task 3.3): append-only, hash-chained, secret-scrubbed.

- `write_audit`: append an entry in the caller's transaction.
- `log_change`: create/update/delete with before and after.
- `log_login_failure`: one `worker.login_failure` row, never the typed ID.
- `verify_chain`: check every chain; `python -m ag_server.audit verify`.

The database enforces append-only too (migration 0002): a trigger rejects
UPDATE, DELETE and TRUNCATE, and the app role has INSERT and SELECT only.
"""

from ag_server.audit.errors import AuditDetailError, AuditError
from ag_server.audit.verify import ChainHead, Problem, VerifyResult, verify_chain
from ag_server.audit.writer import (
    LOGIN_FAILURE_ACTION,
    AuditEntry,
    AuditRecord,
    log_change,
    log_login_failure,
    write_audit,
)

__all__ = [
    "LOGIN_FAILURE_ACTION",
    "AuditDetailError",
    "AuditEntry",
    "AuditError",
    "AuditRecord",
    "ChainHead",
    "Problem",
    "VerifyResult",
    "log_change",
    "log_login_failure",
    "verify_chain",
    "write_audit",
]
