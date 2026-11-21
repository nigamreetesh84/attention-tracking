"""The local evidence store and its disk policy (PLAN 2.2).

Images from evidence bursts are sealed into files (`files.py`) and indexed in
a small SQLite database next to them. The index holds only metadata (ids,
slot, seq, offset, capture time, plaintext SHA-256, sizes, upload flag);
image bytes never reach it.

Disk policy (`api-contract.md` section 6, "Limits (fail-open)"). Before an
image is written the store makes room within the limit, which is the
configured `retention_local.max_disk_mb`, lowered when the volume itself is
nearly full (free space minus a reserve). In order:

1. delete the oldest images that are already uploaded (lossless);
2. delete the oldest images not yet uploaded, never those of the event being
   saved; their events are recorded as `images_withheld = disk_full` and the
   `WithheldListener` is told;
3. only then refuse the new image: `save_burst` returns a result with
   `images_withheld = disk_full` instead of raising.

Losing evidence (step 2 or 3, or a failed write) starts a disk-full episode
and queues one `disk_full` local alert; the episode ends when a save loses
nothing and use is back under `CLEAR_FRACTION` of the limit. A new episode
within `alert_min_interval` of the last alert does not alert again.

The store has no handle on the event queue: the alert sink can only enqueue.
Scan and event metadata are therefore never dropped to make space.

Consent is decided by the capture gate (PLAN 2.5), not here, but the store
enforces that the gate was asked: `save_burst` demands a `CapturePermit`
that only `CaptureGate` mints, and re-checks it before every frame and once
after the last one. If the gate closes mid-burst (logout, consent change,
break) the frames already written for this burst are deleted and the result
carries the gate's `images_withheld` reason.
"""

import hashlib
import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Protocol, Self
from uuid import UUID

from ag_shared.api import EvidenceUploadHeaders, ImagesWithheld, RetentionLocal
from ag_shared.ids import Uuid7Generator
from pydantic import JsonValue

from ag_workstation.clock import Clock
from ag_workstation.consent.decision import GateDecision
from ag_workstation.consent.permit import CapturePermit, PermitError
from ag_workstation.engine_api import EvidenceBurst, EvidenceFrame, FrameRole
from ag_workstation.evidence.alerts import DiskFullAlertSink
from ag_workstation.evidence.files import (
    FILE_OVERHEAD,
    EncryptedImageFiles,
    image_associated_data,
)
from ag_workstation.queue.crypto import PayloadIntegrityError
from ag_workstation.queue.keys import KeyProvider

logger = logging.getLogger(__name__)

DB_VERSION: Final = 1
MIB: Final = 1024 * 1024
DEFAULT_MIN_FREE_MB: Final = 256
"""Free space kept on the volume for the queue and the OS."""
CLEAR_FRACTION: Final = 0.9
"""A disk-full episode ends once use is at or below this share of the limit."""
DEFAULT_ALERT_MIN_INTERVAL: Final = timedelta(hours=1)
INDEX_NAME: Final = "evidence.db"
IMAGES_DIR: Final = "images"

_SCHEMA = (
    "CREATE TABLE evidence_meta (k TEXT PRIMARY KEY, v INTEGER)",
    """
    CREATE TABLE evidence_image (
        ord INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        event_id TEXT NOT NULL,
        camera_slot INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        offset_ms INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        stored_bytes INTEGER NOT NULL,
        uploaded INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        removed_at INTEGER,
        removed_reason TEXT
    )
    """,
    "CREATE INDEX ix_evidence_image_active ON evidence_image (removed_reason, uploaded, ord)",
    "CREATE INDEX ix_evidence_image_event ON evidence_image (event_id)",
    """
    CREATE TABLE withheld_event (
        event_id TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        recorded_at INTEGER NOT NULL,
        image_count INTEGER NOT NULL
    )
    """,
)


_META_EPISODE: Final = "disk_full_active"
_META_LAST_ALERT: Final = "last_alert_at"
_META_RESEALED: Final = "legacy_resealed"
"""Set once pre-3.0 files were re-sealed under the service key (or there were none)."""


class RemovalReason(StrEnum):
    """Why an image file is gone (its index row stays as a tombstone)."""

    EVICTED = "evicted"
    """Already uploaded; deleted to make room."""
    DISK_FULL = "disk_full"
    """Not uploaded; deleted to make room. Its event is images_withheld=disk_full."""
    CONSENT_MISSING = "consent_missing"
    """The server answered `403 consent_missing`."""
    EXPIRED = "expired"
    """Older than `retention_local.max_age_days`."""
    CORRUPT = "corrupt"
    """Missing file, failed authentication or hash mismatch."""
    GATE_CLOSED = "gate_closed"
    """The capture gate closed while its burst was being written."""


class EvidenceUnavailableError(LookupError):
    """The image is not in the store or its file has been removed."""


class EvidenceCorruptError(Exception):
    """The image failed its integrity or hash check; it was removed and must not be uploaded."""


class WithheldListener(Protocol):
    """Told when an event loses images after its burst was saved."""

    def images_withheld(
        self, event_id: UUID, reason: ImagesWithheld, image_ids: tuple[UUID, ...]
    ) -> None:
        """Called after the change is committed. Must not block; exceptions are logged."""
        ...


@dataclass(frozen=True, slots=True)
class StoredImage:
    """One row of the index."""

    image_id: UUID
    event_id: UUID
    camera_slot: int
    seq: int
    offset_ms: int
    captured_at: datetime
    sha256: str
    size_bytes: int
    """JPEG size."""
    stored_bytes: int
    """Encrypted file size (what counts against the quota)."""
    uploaded: bool
    created_at: datetime
    removed_reason: RemovalReason | None

    @property
    def present(self) -> bool:
        """Whether the file is still held."""
        return self.removed_reason is None

    def upload_headers(self) -> EvidenceUploadHeaders:
        """The `X-AG-*` metadata of its upload (contract 4.4)."""
        return EvidenceUploadHeaders(
            event_id=self.event_id,
            camera_slot=self.camera_slot,
            seq=self.seq,
            offset_ms=self.offset_ms,
            captured_at=self.captured_at,
            sha256=self.sha256,
        )

    def associated_data(self) -> bytes:
        """The AES-GCM associated data of its file."""
        return image_associated_data(
            self.image_id,
            self.event_id,
            self.camera_slot,
            self.seq,
            self.offset_ms,
            self.captured_at,
            self.sha256,
        )


@dataclass(frozen=True, slots=True)
class UploadImage:
    """A verified image ready to `PUT /evidence/{image_id}`."""

    image_id: UUID
    headers: EvidenceUploadHeaders
    jpeg: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SaveResult:
    """What `save_burst` stored; feeds `attention_event.image_ids` and `images_withheld`."""

    event_id: UUID
    image_ids: tuple[UUID, ...]
    """Saved images in burst order (camera slot, offset)."""
    images_withheld: ImagesWithheld
    """`disk_full` when any frame could not be saved, else `none`."""
    refused: int = 0
    """Frames not saved."""


@dataclass(frozen=True, slots=True)
class WithheldRecord:
    """An event that lost images to the disk policy."""

    event_id: UUID
    reason: ImagesWithheld
    recorded_at: datetime
    image_count: int
    """Images refused or deleted unsent, over all losses."""


@dataclass(frozen=True, slots=True)
class HousekeepingSummary:
    """Outcome of `purge_expired` / `housekeeping`."""

    expired: int = 0
    expired_unsent: int = 0
    evicted: int = 0
    purged_unsent: int = 0
    tombstones_dropped: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceStats:
    """Numbers for health reporting."""

    images: int
    """Image files held."""
    bytes_used: int
    pending_upload: int
    uploaded_held: int
    limit_bytes: int
    """The configured quota."""
    disk_full: bool
    """A disk-full episode is active."""
    corrupt: int
    withheld_events: int


@dataclass(slots=True)
class _Room:
    ok: bool
    evicted: list[UUID] = field(default_factory=list)
    purged: list[tuple[UUID, UUID]] = field(default_factory=list)
    """(image id, event id) of unsent images deleted."""


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _frame_priority(frame: EvidenceFrame) -> tuple[int, int, int]:
    # Key frames first (they carry the decision), then the frames nearest them.
    return (0 if frame.role is FrameRole.KEY else 1, abs(frame.offset_ms), frame.camera_slot)


class EvidenceStore:
    """Encrypted evidence images on the workstation with quota and age limits. Thread-safe."""

    def __init__(
        self,
        root: Path,
        conn: sqlite3.Connection,
        files: EncryptedImageFiles,
        clock: Clock,
        limits: RetentionLocal,
        *,
        alert_sink: DiskFullAlertSink | None,
        withheld_listener: WithheldListener | None,
        disk_free: Callable[[Path], int],
        min_free_mb: int,
        alert_min_interval: timedelta,
        stamp: Callable[[datetime], datetime] | None = None,
    ) -> None:
        """Use `EvidenceStore.open`."""
        self._root = root
        self._conn = conn
        self._files = files
        self._clock = clock
        self._limits = limits
        self._alert_sink = alert_sink
        self._listener = withheld_listener
        self._disk_free = disk_free
        self._reserve = min_free_mb * MIB
        self._alert_min_interval = alert_min_interval
        self._stamp = stamp
        self._ids = Uuid7Generator(now=clock.now)
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(
        cls,
        root: Path | str,
        keys: KeyProvider,
        clock: Clock,
        limits: RetentionLocal | None = None,
        *,
        alert_sink: DiskFullAlertSink | None = None,
        withheld_listener: WithheldListener | None = None,
        disk_free: Callable[[Path], int] = _free_bytes,
        min_free_mb: int = DEFAULT_MIN_FREE_MB,
        alert_min_interval: timedelta = DEFAULT_ALERT_MIN_INTERVAL,
        stamp: Callable[[datetime], datetime] | None = None,
        legacy_keys: KeyProvider | None = None,
    ) -> Self:
        """Open (creating if needed) the store under `root` and repair it after a crash.

        `keys` is the service-only key (task 3.0). `legacy_keys` (the queue data
        key) is passed by the service parent only: the first open that has it
        re-seals files written before 3.0 under the service key, once
        (`_reseal_legacy`); later opens never read legacy files again.

        `stamp` turns a frame's device capture time into the corrected UTC
        stored and uploaded as `captured_at` (contract 6, "Clock"); None keeps
        it. The capture gate has decided on device time before this point.

        Recovery: temporary files are deleted, files no index row holds are
        deleted, and rows whose file is missing are marked corrupt.
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        files = EncryptedImageFiles(root / IMAGES_DIR, keys, legacy_keys=legacy_keys)
        conn = sqlite3.connect(
            root / INDEX_NAME, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            store = cls(
                root,
                conn,
                files,
                clock,
                limits or RetentionLocal(),
                alert_sink=alert_sink,
                withheld_listener=withheld_listener,
                disk_free=disk_free,
                min_free_mb=min_free_mb,
                alert_min_interval=alert_min_interval,
                stamp=stamp,
            )
            store._init_schema()
            store._recover()
            if legacy_keys is not None:
                store._reseal_legacy()
        except BaseException:
            conn.close()
            raise
        return store

    def close(self) -> None:
        """Close the index. Safe to call twice."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _init_schema(self) -> None:
        with self._tx() as conn:
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            if version > DB_VERSION:
                raise RuntimeError(
                    f"evidence index version {version} is newer than this build ({DB_VERSION})"
                )
            if version == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {DB_VERSION}")

    def _recover(self) -> None:
        temporaries = self._files.remove_temporaries()
        now = self._now_ms()
        with self._tx() as conn:
            active = {
                UUID(r[0])
                for r in conn.execute("SELECT id FROM evidence_image WHERE removed_reason IS NULL")
            }
            on_disk = self._files.stored_ids()
            missing = active - on_disk
            conn.executemany(
                "UPDATE evidence_image SET removed_reason = ?, removed_at = ? WHERE id = ?",
                [(RemovalReason.CORRUPT.value, now, str(i)) for i in missing],
            )
        orphans = on_disk - active
        for image_id in orphans:
            self._files.delete(image_id)
        if temporaries or orphans or missing:
            logger.warning(
                "evidence store recovery: %d temporary, %d orphan files removed; "
                "%d missing files marked corrupt",
                temporaries,
                len(orphans),
                len(missing),
            )

    def _reseal_legacy(self) -> None:
        """Migration to the service key (3.0), done once per store.

        Every held image whose file is a pre-3.0 file is re-sealed under the
        service key; one that does not open with the legacy key is removed as
        corrupt (it could never be uploaded). Then `_META_RESEALED` is set, so
        a legacy file that appears later (planted by an account holding only
        the data key) is never read: it fails as corrupt at upload.
        """
        with self._lock:
            if self._meta(self._conn, _META_RESEALED):
                return
            rows = self._conn.execute(
                "SELECT id, event_id, camera_slot, seq, offset_ms, captured_at, "
                "sha256, size_bytes, stored_bytes, uploaded, created_at, removed_reason "
                "FROM evidence_image WHERE removed_reason IS NULL ORDER BY ord"
            ).fetchall()
        resealed = 0
        failed: list[UUID] = []
        for image in (self._row(r) for r in rows):
            if not self._files.is_legacy(image.image_id):
                continue
            try:
                self._files.reseal_legacy(image.image_id, image.associated_data())
                resealed += 1
            except Exception as exc:
                # A key or integrity failure; disk errors leave the old file, which
                # then fails at upload as corrupt like any unreadable file.
                logger.error(
                    "evidence image %s not re-sealed (%s)", image.image_id, type(exc).__name__
                )
                failed.append(image.image_id)
        if failed:
            self._remove(failed, RemovalReason.CORRUPT)
        with self._tx() as conn:
            self._set_meta(conn, _META_RESEALED, 1)
        if resealed or failed:
            logger.warning(
                "evidence re-sealed under the service key: %d files, %d removed as corrupt",
                resealed,
                len(failed),
            )

    def _now_ms(self) -> int:
        return _ms(self._clock.now())

    # -- limits ---------------------------------------------------------------

    @property
    def limits(self) -> RetentionLocal:
        """The active local retention limits."""
        return self._limits

    def set_limits(self, limits: RetentionLocal) -> None:
        """Apply new limits (from a config bundle); enforced on the next save or housekeeping."""
        with self._lock:
            self._limits = limits

    def _quota_bytes(self) -> int:
        return self._limits.max_disk_mb * MIB

    def _used(self, conn: sqlite3.Connection) -> int:
        (used,) = conn.execute(
            "SELECT coalesce(sum(stored_bytes), 0) FROM evidence_image WHERE removed_reason IS NULL"
        ).fetchone()
        return int(used)

    def _capacity(self, used: int) -> int:
        """The effective limit: the quota, lowered when the volume is nearly full."""
        quota = self._quota_bytes()
        try:
            free = self._disk_free(self._root)
        except OSError:
            logger.warning("evidence store: free disk space unknown; using the quota only")
            return quota
        return min(quota, used + max(0, free - self._reserve))

    def _make_room(self, conn: sqlite3.Connection, need: int, protect: UUID | None) -> _Room:
        """Free space for `need` bytes in the documented order, inside `conn`'s transaction."""
        used = self._used(conn)
        capacity = self._capacity(used)
        room = _Room(ok=used + need <= capacity)
        if room.ok:
            return room
        protected = 0
        if protect is not None:
            (protected,) = conn.execute(
                "SELECT coalesce(sum(stored_bytes), 0) FROM evidence_image "
                "WHERE removed_reason IS NULL AND event_id = ?",
                (str(protect),),
            ).fetchone()
        if protected + need > capacity:
            # Deleting everything else would not make it fit: refuse without loss.
            return room
        now = self._now_ms()
        for rid, eid, uploaded, stored in conn.execute(
            "SELECT id, event_id, uploaded, stored_bytes FROM evidence_image "
            "WHERE removed_reason IS NULL AND event_id != ? ORDER BY uploaded DESC, ord",
            (str(protect) if protect is not None else "",),
        ).fetchall():
            if used + need <= capacity:
                break
            reason = RemovalReason.EVICTED if uploaded else RemovalReason.DISK_FULL
            conn.execute(
                "UPDATE evidence_image SET removed_reason = ?, removed_at = ? WHERE id = ?",
                (reason.value, now, rid),
            )
            if uploaded:
                room.evicted.append(UUID(rid))
            else:
                room.purged.append((UUID(rid), UUID(eid)))
            used -= stored
        room.ok = used + need <= capacity
        return room

    # -- saving ---------------------------------------------------------------

    def save_burst(
        self, burst: EvidenceBurst, event_id: UUID | None = None, *, permit: CapturePermit
    ) -> SaveResult:
        """Seal and store a burst's frames under `event_id` (default: the burst's).

        `permit` comes from the capture gate for this very burst; without a
        valid, unused one this raises `PermitError` and writes nothing. The
        gate is asked again before every frame and after the last: once it
        is closed, this burst's frames are deleted and the result carries the
        gate's reason. Never raises for disk problems: frames that cannot be
        stored are left out and the result says `images_withheld = disk_full`.
        Key frames are stored first, so a nearly full store keeps the most
        useful frames.
        """
        if not isinstance(permit, CapturePermit):
            raise PermitError("save_burst needs a capture permit from the capture gate")
        permit.redeem(burst)
        target = event_id or burst.event_id
        saved: dict[int, UUID] = {}
        refused = 0
        purged: list[tuple[UUID, UUID]] = []
        closed: GateDecision | None = None
        with self._lock:
            order = sorted(range(len(burst.frames)), key=lambda i: _frame_priority(burst.frames[i]))
            for index in order:
                decision = permit.recheck()
                if not decision.open:
                    closed = decision
                    break
                frame = burst.frames[index]
                image_id, room = self._save_frame(target, frame)
                purged.extend(room.purged)
                if image_id is None:
                    refused += 1
                else:
                    saved[index] = image_id
            if closed is None:
                final = permit.recheck()
                closed = None if final.open else final
            if closed is not None:
                self._withdraw(target, list(saved.values()), closed)
                saved.clear()
                refused = 0
            try:
                if refused:
                    self._record_withheld([(target, refused)])
                self._after_change(purged, lost=bool(refused or purged), refused=refused)
            except sqlite3.Error:
                # The frames are saved or not; only bookkeeping failed. Fail open.
                logger.exception("evidence index error after saving a burst")
        if closed is not None:
            return SaveResult(
                event_id=target,
                image_ids=(),
                images_withheld=closed.images_withheld,
                refused=len(burst.frames),
            )
        return SaveResult(
            event_id=target,
            image_ids=tuple(saved[i] for i in sorted(saved)),
            images_withheld=ImagesWithheld.DISK_FULL if refused else ImagesWithheld.NONE,
            refused=refused,
        )

    def _withdraw(self, event_id: UUID, image_ids: list[UUID], decision: GateDecision) -> None:
        """Delete frames of a burst whose gate closed while it was written."""
        logger.info(
            "capture gate closed mid-burst (%s): %d frames of event %s deleted",
            decision.reason.value,
            len(image_ids),
            event_id,
        )
        if not image_ids:
            return
        try:
            self._remove(image_ids, RemovalReason.GATE_CLOSED)
        except (sqlite3.Error, OSError):
            logger.exception("evidence index error while withdrawing frames; deleting files")
            # A row without its file is marked corrupt by recovery; never keep the image.
            for image_id in image_ids:
                try:
                    self._files.delete(image_id)
                except OSError:
                    logger.exception("could not delete withdrawn evidence file %s", image_id)

    def _save_frame(self, event_id: UUID, frame: EvidenceFrame) -> tuple[UUID | None, _Room]:
        image_id = self._ids()
        need = frame.size_bytes + FILE_OVERHEAD
        try:
            with self._tx() as conn:
                room = self._make_room(conn, need, event_id)
        except sqlite3.Error:
            logger.exception("evidence index error while making room; frame not saved")
            return None, _Room(ok=False)
        self._unlink(room)
        if not room.ok:
            logger.warning("evidence store full: frame of event %s not saved", event_id)
            return None, room
        digest = hashlib.sha256(frame.jpeg).hexdigest()
        captured_at = self._captured_at(frame.captured_at)
        ad = image_associated_data(
            image_id,
            event_id,
            frame.camera_slot,
            frame.seq,
            frame.offset_ms,
            captured_at,
            digest,
        )
        try:
            stored = self._files.write(image_id, frame.jpeg, ad)
        except OSError as exc:
            logger.error("evidence file write failed (%s); frame not saved", type(exc).__name__)
            return None, room
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO evidence_image (id, event_id, camera_slot, seq, offset_ms, "
                    "captured_at, "
                    "sha256, size_bytes, stored_bytes, uploaded, created_at, removed_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)",
                    (
                        str(image_id),
                        str(event_id),
                        frame.camera_slot,
                        frame.seq,
                        frame.offset_ms,
                        captured_at.astimezone(UTC).isoformat(),
                        digest,
                        frame.size_bytes,
                        stored,
                        self._now_ms(),
                    ),
                )
        except sqlite3.Error:
            logger.exception("evidence index insert failed; frame not saved")
            self._files.delete(image_id)
            return None, room
        return image_id, room

    def _captured_at(self, device_time: datetime) -> datetime:
        if self._stamp is None:
            return device_time
        try:
            return self._stamp(device_time)
        except Exception as exc:
            # Fail-open: the device time is still a valid capture time.
            logger.error("capture time not corrected (%s)", type(exc).__name__)
            return device_time

    def _unlink(self, room: _Room) -> None:
        # After the commit: a crash in between leaves an orphan file that
        # recovery deletes, never a row pointing at a missing file.
        for image_id in (*room.evicted, *(i for i, _ in room.purged)):
            self._files.delete(image_id)

    def _record_withheld(self, losses: Sequence[tuple[UUID, int]]) -> None:
        now = self._now_ms()
        with self._tx() as conn:
            for event_id, count in losses:
                conn.execute(
                    "INSERT INTO withheld_event (event_id, reason, recorded_at, image_count) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (event_id) "
                    "DO UPDATE SET image_count = image_count + excluded.image_count",
                    (str(event_id), ImagesWithheld.DISK_FULL.value, now, count),
                )

    def _after_change(self, purged: list[tuple[UUID, UUID]], *, lost: bool, refused: int) -> None:
        by_event: dict[UUID, list[UUID]] = {}
        for image_id, event_id in purged:
            by_event.setdefault(event_id, []).append(image_id)
        if by_event:
            self._record_withheld([(e, len(ids)) for e, ids in by_event.items()])
            logger.warning(
                "evidence store full: deleted %d unsent images of %d events",
                len(purged),
                len(by_event),
            )
        if lost:
            self._note_loss(purged=len(purged), refused=refused)
        else:
            self._maybe_clear_episode()
        if self._listener is not None:
            for event_id, ids in by_event.items():
                try:
                    self._listener.images_withheld(event_id, ImagesWithheld.DISK_FULL, tuple(ids))
                except Exception:
                    logger.exception("withheld listener failed for event %s", event_id)

    # -- disk-full episode ----------------------------------------------------

    def _meta(self, conn: sqlite3.Connection, key: str) -> int | None:
        row = conn.execute("SELECT v FROM evidence_meta WHERE k = ?", (key,)).fetchone()
        return None if row is None else int(row[0])

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: int) -> None:
        conn.execute(
            "INSERT INTO evidence_meta (k, v) VALUES (?, ?) "
            "ON CONFLICT (k) DO UPDATE SET v = excluded.v",
            (key, value),
        )

    def _note_loss(self, *, purged: int, refused: int) -> None:
        now = self._now_ms()
        with self._tx() as conn:
            if self._meta(conn, _META_EPISODE):
                return
            last = self._meta(conn, _META_LAST_ALERT)
            used = self._used(conn)
        cooled = last is None or now - last >= self._alert_min_interval.total_seconds() * 1000
        if cooled and self._alert_sink is not None:
            detail: dict[str, JsonValue] = {
                "used_mb": round(used / MIB, 1),
                "quota_mb": self._limits.max_disk_mb,
                "purged_unsent": purged,
                "refused": refused,
            }
            try:
                self._alert_sink.disk_full(detail)
            except Exception:
                # Episode stays inactive so the next loss tries again.
                logger.exception("could not queue the disk_full alert")
                return
            logger.warning("disk_full alert queued")
        with self._tx() as conn:
            self._set_meta(conn, _META_EPISODE, 1)
            if cooled and self._alert_sink is not None:
                self._set_meta(conn, _META_LAST_ALERT, now)

    def _maybe_clear_episode(self) -> None:
        with self._tx() as conn:
            if not self._meta(conn, _META_EPISODE):
                return
            used = self._used(conn)
            if used <= CLEAR_FRACTION * self._capacity(used):
                self._set_meta(conn, _META_EPISODE, 0)
                logger.info("evidence store back under its limit; disk-full episode over")

    # -- upload side ----------------------------------------------------------

    def pending_uploads(self, limit: int = 50) -> list[StoredImage]:
        """Held images not yet uploaded, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, event_id, camera_slot, seq, offset_ms, captured_at, "
                "sha256, size_bytes, stored_bytes, uploaded, created_at, removed_reason "
                "FROM evidence_image WHERE removed_reason IS NULL AND uploaded = 0 "
                "ORDER BY ord LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def image(self, image_id: UUID) -> StoredImage | None:
        """One index row (tombstones included), or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, event_id, camera_slot, seq, offset_ms, captured_at, "
                "sha256, size_bytes, stored_bytes, uploaded, created_at, removed_reason "
                "FROM evidence_image WHERE id = ?",
                (str(image_id),),
            ).fetchone()
        return None if row is None else self._row(row)

    def event_images(self, event_id: UUID) -> list[StoredImage]:
        """All index rows of an event, in save order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, event_id, camera_slot, seq, offset_ms, captured_at, "
                "sha256, size_bytes, stored_bytes, uploaded, created_at, removed_reason "
                "FROM evidence_image WHERE event_id = ? ORDER BY ord",
                (str(event_id),),
            ).fetchall()
        return [self._row(r) for r in rows]

    def read_for_upload(self, image_id: UUID) -> UploadImage:
        """Decrypt an image and check its SHA-256 against the index.

        Raises `EvidenceUnavailableError` if it is unknown or removed, and
        `EvidenceCorruptError` (after removing it as corrupt) if the file is
        missing, does not authenticate, or its hash does not match.
        """
        image = self.image(image_id)
        if image is None or not image.present:
            raise EvidenceUnavailableError(f"evidence image {image_id} is not held")
        try:
            jpeg = self._files.read(image_id, image.associated_data())
        except (FileNotFoundError, PayloadIntegrityError) as exc:
            self._mark_corrupt(image_id, type(exc).__name__)
            raise EvidenceCorruptError(f"evidence image {image_id} is corrupt") from exc
        if hashlib.sha256(jpeg).hexdigest() != image.sha256:
            self._mark_corrupt(image_id, "hash mismatch")
            raise EvidenceCorruptError(f"evidence image {image_id} failed its hash check")
        return UploadImage(image_id=image_id, headers=image.upload_headers(), jpeg=jpeg)

    def _mark_corrupt(self, image_id: UUID, why: str) -> None:
        logger.error("evidence image %s is corrupt (%s); removed, not uploaded", image_id, why)
        self._remove([image_id], RemovalReason.CORRUPT)

    def _remove(self, image_ids: Sequence[UUID], reason: RemovalReason) -> int:
        now = self._now_ms()
        with self._tx() as conn:
            count = 0
            for image_id in image_ids:
                count += conn.execute(
                    "UPDATE evidence_image SET removed_reason = ?, removed_at = ? "
                    "WHERE id = ? AND removed_reason IS NULL",
                    (reason.value, now, str(image_id)),
                ).rowcount
        for image_id in image_ids:
            self._files.delete(image_id)
        return count

    def mark_uploaded(self, image_id: UUID) -> bool:
        """Record a `201`/`200` answer. The file stays until space or age removes it."""
        with self._tx() as conn:
            changed = conn.execute(
                "UPDATE evidence_image SET uploaded = 1 WHERE id = ?", (str(image_id),)
            ).rowcount
        return bool(changed)

    def delete_consent_missing(self, image_id: UUID) -> int:
        """Handle `403 consent_missing`: delete this image and its event's other held images.

        Consent belongs to the event's worker, so the rest of the event would
        get the same answer. Returns the number of files deleted.
        """
        image = self.image(image_id)
        if image is None:
            return 0
        with self._lock:
            ids = [i.image_id for i in self.event_images(image.event_id) if i.present]
            count = self._remove(ids, RemovalReason.CONSENT_MISSING)
        logger.info("deleted %d images of event %s: consent missing", count, image.event_id)
        return count

    # -- housekeeping -------------------------------------------------------

    def purge_expired(self) -> HousekeepingSummary:
        """Delete images older than `max_age_days`, uploaded or not, and old tombstones."""
        now = self._clock.now()
        cutoff = _ms(now - timedelta(days=self._limits.max_age_days))
        with self._lock:
            with self._tx() as conn:
                rows = conn.execute(
                    "SELECT id, uploaded FROM evidence_image "
                    "WHERE removed_reason IS NULL AND created_at < ?",
                    (cutoff,),
                ).fetchall()
                conn.executemany(
                    "UPDATE evidence_image SET removed_reason = ?, removed_at = ? WHERE id = ?",
                    [(RemovalReason.EXPIRED.value, _ms(now), r[0]) for r in rows],
                )
                dropped = conn.execute(
                    "DELETE FROM evidence_image WHERE removed_reason IS NOT NULL "
                    "AND removed_at < ?",
                    (cutoff,),
                ).rowcount
                conn.execute("DELETE FROM withheld_event WHERE recorded_at < ?", (cutoff,))
            for rid, _ in rows:
                self._files.delete(UUID(rid))
        unsent = sum(1 for _, uploaded in rows if not uploaded)
        if unsent:
            logger.warning("%d evidence images expired before upload", unsent)
        return HousekeepingSummary(
            expired=len(rows), expired_unsent=unsent, tombstones_dropped=dropped
        )

    def enforce_quota(self) -> HousekeepingSummary:
        """Bring use under the limit (after the quota shrank), in the same order as a save."""
        with self._lock:
            with self._tx() as conn:
                room = self._make_room(conn, 0, None)
            self._unlink(room)
            self._after_change(room.purged, lost=bool(room.purged), refused=0)
        return HousekeepingSummary(evicted=len(room.evicted), purged_unsent=len(room.purged))

    def housekeeping(self) -> HousekeepingSummary:
        """Age purge then quota enforcement (run periodically by the service)."""
        aged = self.purge_expired()
        quota = self.enforce_quota()
        return HousekeepingSummary(
            expired=aged.expired,
            expired_unsent=aged.expired_unsent,
            evicted=quota.evicted,
            purged_unsent=quota.purged_unsent,
            tombstones_dropped=aged.tombstones_dropped,
        )

    # -- reporting ------------------------------------------------------------

    def withheld_events(self) -> list[WithheldRecord]:
        """Events that lost images to the disk policy, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, reason, recorded_at, image_count FROM withheld_event "
                "ORDER BY recorded_at, event_id"
            ).fetchall()
        return [
            WithheldRecord(
                event_id=UUID(e), reason=ImagesWithheld(r), recorded_at=_dt(t), image_count=n
            )
            for e, r, t, n in rows
        ]

    def withheld_reason(self, event_id: UUID) -> ImagesWithheld:
        """`disk_full` if the event lost images to the disk policy, else `none`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT reason FROM withheld_event WHERE event_id = ?", (str(event_id),)
            ).fetchone()
        return ImagesWithheld.NONE if row is None else ImagesWithheld(row[0])

    def stats(self) -> EvidenceStats:
        """Store numbers for the health sample."""
        with self._lock:
            images, used, pending = self._conn.execute(
                "SELECT count(*), coalesce(sum(stored_bytes), 0), "
                "coalesce(sum(uploaded = 0), 0) FROM evidence_image WHERE removed_reason IS NULL"
            ).fetchone()
            (corrupt,) = self._conn.execute(
                "SELECT count(*) FROM evidence_image WHERE removed_reason = ?",
                (RemovalReason.CORRUPT.value,),
            ).fetchone()
            (withheld,) = self._conn.execute("SELECT count(*) FROM withheld_event").fetchone()
            episode = self._meta(self._conn, _META_EPISODE)
        return EvidenceStats(
            images=images,
            bytes_used=used,
            pending_upload=pending,
            uploaded_held=images - pending,
            limit_bytes=self._quota_bytes(),
            disk_full=bool(episode),
            corrupt=corrupt,
            withheld_events=withheld,
        )

    @staticmethod
    def _row(
        row: Any,
    ) -> StoredImage:  # Any: sqlite3 rows are untyped tuples (column order of the SELECTs)
        rid, eid, slot, seq, off, cap, sha, size, stored, uploaded, created, removed = row
        return StoredImage(
            image_id=UUID(rid),
            event_id=UUID(eid),
            camera_slot=slot,
            seq=seq,
            offset_ms=off,
            captured_at=datetime.fromisoformat(cap),
            sha256=sha,
            size_bytes=size,
            stored_bytes=stored,
            uploaded=bool(uploaded),
            created_at=_dt(created),
            removed_reason=None if removed is None else RemovalReason(removed),
        )
