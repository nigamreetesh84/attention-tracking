"""``python -m ag_server.audit verify``: check the audit hash chains.

Exit codes: 0 every chain verifies, 1 tampering or corruption found, 2 the
check could not run (settings or database). Uses the app's database settings
(`AG_DATABASE_URL` / `_FILE`); SELECT on audit_log and purge_log is enough.
Output names row ids and reasons, never detail contents or credentials.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence

from sqlalchemy import create_engine, pool
from sqlalchemy.exc import SQLAlchemyError

from ag_server.audit.verify import VerifyResult, verify_chain
from ag_server.settings import SettingsError, load_settings


def _report(result: VerifyResult) -> None:
    status = "OK" if result.ok else "FAILED"
    print(f"audit chain {status}: {result.rows} rows in {len(result.heads)} chains")
    for site, head in sorted(result.heads.items(), key=lambda kv: str(kv[0] or "")):
        name = "global" if site is None else str(site)
        print(f"  {name}: {head.rows} rows, last id {head.last_id}, head {head.hash.hex()}")
    for problem in result.problems:
        name = "global" if problem.site_id is None else str(problem.site_id)
        print(f"  row {problem.row_id} ({name}): {problem.reason}")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ag_server.audit")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="verify every audit hash chain (exit 1 on tampering)")
    parser.parse_args(argv)

    try:
        settings = load_settings(env)
    except SettingsError as exc:
        print(f"audit verify: settings: {exc}", file=sys.stderr)
        return 2
    engine = create_engine(settings.database_url.get_secret_value(), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            result = verify_chain(conn)
    except SQLAlchemyError as exc:
        # The exception text can contain the DSN; report only its kind.
        print(f"audit verify: database error ({type(exc).__name__})", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    _report(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
