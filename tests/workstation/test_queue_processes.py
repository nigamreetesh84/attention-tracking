"""Queue behavior across real processes: concurrent writers and kills mid-write."""

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from ag_workstation.clock import SystemClock
from ag_workstation.queue import EventQueue, InMemoryKeyProvider

KEY = bytes(range(32))

_PRELUDE = textwrap.dedent(
    """
    import os, sys
    from datetime import UTC, datetime
    from ag_workstation.clock import SystemClock
    from ag_workstation.queue import EventQueue, InMemoryKeyProvider
    db = sys.argv[1]
    q = EventQueue.open(
        db, InMemoryKeyProvider(bytes(range(32))), SystemClock(), recover_sending=False
    )
    now = datetime.now(UTC)
    """
)


def _run(script: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and test-owned script
        [sys.executable, "-c", _PRELUDE + textwrap.dedent(script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _spawn(script: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and test-owned script
        [sys.executable, "-c", _PRELUDE + textwrap.dedent(script), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _open(db: Path) -> EventQueue:
    return EventQueue.open(db, InMemoryKeyProvider(KEY), SystemClock())


def _all_seqs(db: Path) -> list[int]:
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return [s for (s,) in conn.execute("SELECT seq FROM queue_record ORDER BY seq")]


def test_concurrent_writers_get_unique_contiguous_seqs(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    _open(db).close()  # create the schema once so both writers start equal
    writer = """
        tag = sys.argv[2]
        seqs = [q.enqueue("scan", {"barcode": f"{tag}-{i}"}, now).seq for i in range(150)]
        assert seqs == sorted(seqs)
        print("ok")
    """
    procs = [_spawn(writer, str(db), tag) for tag in ("a", "b")]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        assert out.strip() == "ok"
    assert _all_seqs(db) == list(range(1, 301))
    with _open(db) as q:
        records = q.claim_batch(max_items=200) + q.claim_batch(max_items=200)
    assert len({r.id for r in records}) == 300
    by_tag: dict[str, list[int]] = {"a": [], "b": []}
    for r in records:
        barcode = r.payload["barcode"]
        assert isinstance(barcode, str)
        by_tag[barcode[0]].append(int(barcode[2:]))
    # Each writer's records come out in the order that writer wrote them.
    assert by_tag == {"a": list(range(150)), "b": list(range(150))}


def test_exit_inside_write_transaction_leaves_no_partial_record(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    script = """
        for i in range(20):
            q.enqueue("scan", {"n": i}, now)
        def die_on_commit(statement):
            if statement.startswith("COMMIT"):
                os._exit(9)
        q._conn.set_trace_callback(die_on_commit)
        q.enqueue("scan", {"n": 20}, now)
        print("unreachable")
    """
    result = _run(script, str(db))
    assert result.returncode == 9, result.stderr
    assert "unreachable" not in result.stdout
    assert _all_seqs(db) == list(range(1, 21))
    with _open(db) as q:
        # The interrupted write's seq was rolled back too.
        assert q.enqueue("scan", {}, SystemClock().now()).seq == 21
        assert len(q.claim_batch()) == 21
        assert q.stats().dead_letter_count == 0


def test_kill_during_writes_keeps_db_consistent(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    script = """
        i = 0
        while True:
            q.enqueue("scan", {"barcode": "x" * 500, "n": i}, now)
            i += 1
            if i == 30:
                print("ready", flush=True)
    """
    proc = _spawn(script, str(db))
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    proc.kill()
    proc.wait(timeout=10)
    seqs = _all_seqs(db)
    assert len(seqs) >= 30
    assert seqs == list(range(1, len(seqs) + 1))
    with _open(db) as q:
        claimed = q.claim_batch(max_items=200, max_bytes=10_000_000)
        assert [r.payload["n"] for r in claimed] == list(range(len(claimed)))
        assert q.stats().dead_letter_count == 0


def test_kill_after_claim_returns_records_to_pending(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    script = """
        for i in range(8):
            q.enqueue("scan", {"n": i}, now)
        q.claim_batch(max_items=5)
        os._exit(0)
    """
    result = _run(script, str(db))
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM queue_record WHERE status = 'sending'"
        ).fetchone() == (5,)
    with _open(db) as q:
        assert q.stats().sending == 0
        assert [r.payload["n"] for r in q.claim_batch()] == list(range(8))
