"""Local evidence store: encryption, hash check, disk policy, alerting, crash safety, age purge."""

import errno
import logging
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ag_shared.api import (
    EventType,
    ImagesWithheld,
    LocalAlertKind,
    RetentionLocal,
    ScanPayload,
)
from ag_shared.ids import is_uuid7, new_uuid7
from ag_shared.states import AttentionState, ScanOutcome
from ag_workstation.clock import FakeClock
from ag_workstation.engine_api import (
    JPEG_SOI,
    BurstKind,
    EvidenceBurst,
    EvidenceFrame,
    FrameRole,
)
from ag_workstation.evidence import (
    FILE_OVERHEAD,
    MIB,
    EncryptedImageFiles,
    EvidenceCorruptError,
    EvidenceStore,
    EvidenceUnavailableError,
    QueueDiskFullAlert,
    RemovalReason,
    StoredImage,
)
from ag_workstation.evidence import store as store_module
from ag_workstation.evidence.files import TMP_PREFIX, derive_evidence_key
from ag_workstation.fakes import BurstBuilder
from ag_workstation.queue import EventQueue, InMemoryKeyProvider
from pydantic import JsonValue

KEY = bytes(range(32))
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
SENTINEL = b"SENTINEL-jpeg-4b1d77"
SIZE = 200_000
"""Five such frames fit in 1 MiB, six do not."""
PLENTY = 10**12


def _jpeg(size: int = SIZE, tag: bytes = b"") -> bytes:
    head = JPEG_SOI + SENTINEL + tag
    return head + b"\x5a" * (size - len(head))


def burst(
    event_id: UUID | None = None,
    sizes: tuple[int, ...] = (SIZE,),
    at: datetime = T0,
) -> EvidenceBurst:
    """One key frame, then post-frames, from slot 1."""
    event_id = event_id or new_uuid7()
    frames = []
    for k, size in enumerate(sizes):
        frames.append(
            EvidenceFrame(
                camera_slot=1,
                captured_at=at + timedelta(milliseconds=500 * k),
                offset_ms=500 * k,
                seq=100 + k,
                role=FrameRole.KEY if k == 0 else FrameRole.POST,
                jpeg=_jpeg(size, f"{event_id}/{k}".encode()),
            )
        )
    return EvidenceBurst(
        event_id=event_id,
        kind=BurstKind.SCAN_WARNING,
        triggered_at=at,
        trigger_ts=0.0,
        frames=tuple(frames),
        expected_frames=len(frames),
    )


def row(s: EvidenceStore, image_id: UUID) -> StoredImage:
    found = s.image(image_id)
    assert found is not None
    return found


class Alerts:
    def __init__(self, fail: int = 0) -> None:
        self.calls: list[dict[str, JsonValue]] = []
        self.fail = fail

    def disk_full(self, detail: dict[str, JsonValue]) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("queue unavailable")
        self.calls.append(detail)


class Withheld:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[UUID, ImagesWithheld, tuple[UUID, ...]]] = []
        self.fail = fail

    def images_withheld(
        self, event_id: UUID, reason: ImagesWithheld, image_ids: tuple[UUID, ...]
    ) -> None:
        self.calls.append((event_id, reason, image_ids))
        if self.fail:
            raise RuntimeError("listener bug")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_utc=T0)


@pytest.fixture
def alerts() -> Alerts:
    return Alerts()


@pytest.fixture
def withheld() -> Withheld:
    return Withheld()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "evidence"


def _open(
    root: Path,
    clock: FakeClock,
    *,
    mb: int = 1,
    alerts: Alerts | None = None,
    withheld: Withheld | None = None,
    free: int = PLENTY,
    alert_min_interval: timedelta = timedelta(hours=1),
) -> EvidenceStore:
    return EvidenceStore.open(
        root,
        InMemoryKeyProvider(KEY),
        clock,
        RetentionLocal(max_disk_mb=mb, max_age_days=30),
        alert_sink=alerts,
        withheld_listener=withheld,
        disk_free=lambda _: free,
        min_free_mb=1,
        alert_min_interval=alert_min_interval,
    )


@pytest.fixture
def store(
    root: Path, clock: FakeClock, alerts: Alerts, withheld: Withheld
) -> Iterator[EvidenceStore]:
    s = _open(root, clock, alerts=alerts, withheld=withheld)
    yield s
    s.close()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in (root / "images").iterdir())


# -- storage and encryption ----------------------------------------------------


def test_save_and_read_back(store: EvidenceStore) -> None:
    b = burst(sizes=(SIZE, 1000))
    result = store.save_burst(b)
    assert result.images_withheld is ImagesWithheld.NONE
    assert result.refused == 0
    assert len(result.image_ids) == 2
    assert all(is_uuid7(i) for i in result.image_ids)
    for image_id, frame in zip(result.image_ids, b.frames, strict=True):
        up = store.read_for_upload(image_id)
        assert up.jpeg == frame.jpeg
        assert up.headers.event_id == b.event_id
        assert up.headers.sha256 == frame.sha256
        assert up.headers.camera_slot == frame.camera_slot
        assert up.headers.seq == frame.seq
        assert up.headers.offset_ms == frame.offset_ms
        assert up.headers.captured_at == frame.captured_at
        row = store.image(image_id)
        assert row is not None
        assert row.size_bytes == frame.size_bytes
        assert row.stored_bytes == frame.size_bytes + FILE_OVERHEAD
        assert row.created_at == T0
        assert not row.uploaded
    assert store.stats().images == 2


def test_event_id_override_binds_images(store: EvidenceStore) -> None:
    other = new_uuid7()
    result = store.save_burst(burst(), other)
    assert result.event_id == other
    assert store.read_for_upload(result.image_ids[0]).headers.event_id == other


def test_empty_burst(store: EvidenceStore) -> None:
    b = EvidenceBurst(
        event_id=new_uuid7(),
        kind=BurstKind.EPISODE_KEYFRAME,
        triggered_at=T0,
        trigger_ts=0.0,
        frames=(),
        expected_frames=1,
    )
    result = store.save_burst(b)
    assert result.image_ids == ()
    assert result.images_withheld is ImagesWithheld.NONE


def test_builder_burst_round_trip(store: EvidenceStore) -> None:
    b = BurstBuilder(secondary_slots=(2,)).build(new_uuid7(), BurstKind.SCAN_WARNING, T0)
    result = store.save_burst(b)
    assert [store.read_for_upload(i).jpeg for i in result.image_ids] == [f.jpeg for f in b.frames]


def test_no_plaintext_jpeg_on_disk(store: EvidenceStore, root: Path) -> None:
    b = burst(sizes=(SIZE, 5000, 300))
    store.save_burst(b)
    store.close()
    files = [p for p in root.rglob("*") if p.is_file()]
    assert any(p.suffix == ".agev" for p in files)
    for path in files:
        data = path.read_bytes()
        assert SENTINEL not in data, path
        for frame in b.frames:
            assert frame.jpeg[2:64] not in data, path


def test_files_use_a_derived_key(root: Path) -> None:
    keys = InMemoryKeyProvider(KEY)
    assert derive_evidence_key(keys) != KEY
    assert len(derive_evidence_key(keys)) == 32


def test_file_swapped_to_another_id_is_corrupt(store: EvidenceStore, root: Path) -> None:
    a = store.save_burst(burst()).image_ids[0]
    b = store.save_burst(burst()).image_ids[0]
    files = EncryptedImageFiles(root / "images", InMemoryKeyProvider(KEY))
    files.path_for(b).write_bytes(files.path_for(a).read_bytes())
    with pytest.raises(EvidenceCorruptError):
        store.read_for_upload(b)
    row = store.image(b)
    assert row is not None
    assert row.removed_reason is RemovalReason.CORRUPT
    assert not files.path_for(b).exists()
    assert store.stats().corrupt == 1
    assert store.read_for_upload(a).jpeg  # the original is untouched


def test_hash_mismatch_is_corrupt_and_not_uploaded(store: EvidenceStore, root: Path) -> None:
    image_id = store.save_burst(burst()).image_ids[0]
    row = store.image(image_id)
    assert row is not None
    # A file that authenticates for this row but holds other bytes.
    files = EncryptedImageFiles(root / "images", InMemoryKeyProvider(KEY))
    files.write(image_id, _jpeg(SIZE, b"other"), row.associated_data())
    with pytest.raises(EvidenceCorruptError, match="hash"):
        store.read_for_upload(image_id)
    assert store.pending_uploads() == []
    with pytest.raises(EvidenceUnavailableError):
        store.read_for_upload(image_id)


def test_garbage_and_missing_files_are_corrupt(store: EvidenceStore, root: Path) -> None:
    a, b = store.save_burst(burst(sizes=(1000, 1000))).image_ids
    files = EncryptedImageFiles(root / "images", InMemoryKeyProvider(KEY))
    files.path_for(a).write_bytes(b"not an evidence file")
    files.path_for(b).unlink()
    for image_id in (a, b):
        with pytest.raises(EvidenceCorruptError):
            store.read_for_upload(image_id)
    assert store.stats().corrupt == 2


def test_unknown_image_is_unavailable(store: EvidenceStore) -> None:
    with pytest.raises(EvidenceUnavailableError):
        store.read_for_upload(new_uuid7())
    assert store.image(new_uuid7()) is None
    assert store.delete_consent_missing(new_uuid7()) == 0
    assert not store.mark_uploaded(new_uuid7())


# -- upload bookkeeping ----------------------------------------------------------


def test_pending_uploads_and_mark_uploaded(store: EvidenceStore, clock: FakeClock) -> None:
    first = store.save_burst(burst(sizes=(1000, 1000)))
    clock.advance(1)
    second = store.save_burst(burst(sizes=(1000,)))
    pending = [i.image_id for i in store.pending_uploads()]
    assert pending == [*first.image_ids, *second.image_ids]
    assert [i.image_id for i in store.pending_uploads(limit=1)] == [first.image_ids[0]]
    assert store.mark_uploaded(first.image_ids[0])
    assert store.pending_uploads()[0].image_id == first.image_ids[1]
    stats = store.stats()
    assert (stats.images, stats.pending_upload, stats.uploaded_held) == (3, 2, 1)
    # An uploaded image stays readable until space or age removes it.
    assert store.read_for_upload(first.image_ids[0]).jpeg


def test_consent_missing_deletes_the_events_images(store: EvidenceStore, root: Path) -> None:
    keep = store.save_burst(burst(sizes=(1000,)))
    gone = store.save_burst(burst(sizes=(1000, 1000, 1000)))
    assert store.delete_consent_missing(gone.image_ids[1]) == 3
    for image_id in gone.image_ids:
        row = store.image(image_id)
        assert row is not None
        assert row.removed_reason is RemovalReason.CONSENT_MISSING
        with pytest.raises(EvidenceUnavailableError):
            store.read_for_upload(image_id)
    assert [i.image_id for i in store.pending_uploads()] == list(keep.image_ids)
    assert len(_files(root)) == 1
    assert [i.image_id for i in store.event_images(gone.event_id)] == list(gone.image_ids)


# -- disk policy -------------------------------------------------------------------


def test_purge_order_uploaded_then_unsent_then_refuse(
    store: EvidenceStore, clock: FakeClock, alerts: Alerts, withheld: Withheld
) -> None:
    events = []
    for _ in range(5):
        events.append(store.save_burst(burst()))
        clock.advance(1)
    assert store.stats().bytes_used == 5 * (SIZE + FILE_OVERHEAD)
    # Events 1 and 3 uploaded; event 0 is the oldest unsent.
    store.mark_uploaded(events[1].image_ids[0])
    store.mark_uploaded(events[3].image_ids[0])

    # 1. The oldest uploaded image goes first, not the older unsent one.
    r = store.save_burst(burst())
    assert r.images_withheld is ImagesWithheld.NONE
    assert row(store, events[1].image_ids[0]).removed_reason is RemovalReason.EVICTED
    assert row(store, events[0].image_ids[0]).present
    assert alerts.calls == []

    # 2. Then the next uploaded one.
    store.save_burst(burst())
    assert row(store, events[3].image_ids[0]).removed_reason is RemovalReason.EVICTED
    assert row(store, events[0].image_ids[0]).present
    assert alerts.calls == []

    # 3. No uploaded images left: the oldest unsent image goes.
    r = store.save_burst(burst())
    assert r.images_withheld is ImagesWithheld.NONE  # the new event keeps its image
    assert row(store, events[0].image_ids[0]).removed_reason is RemovalReason.DISK_FULL
    assert row(store, events[2].image_ids[0]).present
    assert withheld.calls == [(events[0].event_id, ImagesWithheld.DISK_FULL, events[0].image_ids)]
    assert store.withheld_reason(events[0].event_id) is ImagesWithheld.DISK_FULL
    assert store.withheld_reason(events[2].event_id) is ImagesWithheld.NONE
    assert len(alerts.calls) == 1
    assert store.stats().disk_full
    assert store.stats().bytes_used <= MIB


def test_burst_larger_than_the_rest_is_refused_last(
    store: EvidenceStore, clock: FakeClock, withheld: Withheld
) -> None:
    old = store.save_burst(burst())
    clock.advance(1)
    big = store.save_burst(burst(sizes=(SIZE,) * 6))
    # Everything else is purged before any frame of the new event is refused.
    assert big.refused == 1
    assert len(big.image_ids) == 5
    assert big.images_withheld is ImagesWithheld.DISK_FULL
    assert row(store, old.image_ids[0]).removed_reason is RemovalReason.DISK_FULL
    assert store.withheld_reason(big.event_id) is ImagesWithheld.DISK_FULL
    records = {r.event_id: r for r in store.withheld_events()}
    assert records[big.event_id].image_count == 1
    assert records[old.event_id].image_count == 1
    assert records[old.event_id].recorded_at == T0 + timedelta(seconds=1)


def test_frame_that_can_never_fit_is_refused_without_loss(
    store: EvidenceStore, alerts: Alerts
) -> None:
    small = store.save_burst(burst())
    r = store.save_burst(burst(sizes=(MIB + 1,)))
    assert r.image_ids == ()
    assert r.images_withheld is ImagesWithheld.DISK_FULL
    assert row(store, small.image_ids[0]).present
    assert len(alerts.calls) == 1


def test_key_frames_are_saved_first(store: EvidenceStore) -> None:
    event_id = new_uuid7()
    layout = [(-1000, FrameRole.PRE), (-500, FrameRole.PRE), (0, FrameRole.KEY)]
    layout += [(500 * k, FrameRole.POST) for k in (1, 2, 3)]
    frames = tuple(
        EvidenceFrame(
            camera_slot=1,
            captured_at=T0 + timedelta(milliseconds=offset),
            offset_ms=offset,
            seq=10 + n,
            role=role,
            jpeg=_jpeg(SIZE, str(offset).encode()),
        )
        for n, (offset, role) in enumerate(layout)
    )
    b = EvidenceBurst(
        event_id=event_id,
        kind=BurstKind.SCAN_WARNING,
        triggered_at=T0,
        trigger_ts=0.0,
        frames=frames,
        expected_frames=len(frames),
    )
    r = store.save_burst(b)
    assert r.refused == 1
    offsets = [row(store, i).offset_ms for i in r.image_ids]
    # Only the frame farthest from the key frame is dropped; ids stay in burst order.
    assert offsets == [-1000, -500, 0, 500, 1000]


def test_free_disk_space_lowers_the_limit(root: Path, clock: FakeClock) -> None:
    volume = 3 * (SIZE + FILE_OVERHEAD) + MIB  # reserve is 1 MiB in tests

    def free(_: Path) -> int:
        images = root / "images"
        return volume - sum(p.stat().st_size for p in images.iterdir())

    s = EvidenceStore.open(
        root,
        InMemoryKeyProvider(KEY),
        clock,
        RetentionLocal(max_disk_mb=100),
        disk_free=free,
        min_free_mb=1,
    )
    with s:
        r = s.save_burst(burst(sizes=(SIZE,) * 4))
        assert r.refused == 1
        assert len(r.image_ids) == 3


def test_unknown_free_space_uses_the_quota(root: Path, clock: FakeClock) -> None:
    def broken(_: Path) -> int:
        raise OSError("statvfs failed")

    s = EvidenceStore.open(
        root,
        InMemoryKeyProvider(KEY),
        clock,
        RetentionLocal(max_disk_mb=1),
        disk_free=broken,
    )
    with s:
        assert s.save_burst(burst(sizes=(SIZE,) * 6)).refused == 1


def test_enforce_quota_after_the_quota_shrinks(
    root: Path, clock: FakeClock, alerts: Alerts, withheld: Withheld
) -> None:
    with _open(root, clock, mb=2, alerts=alerts, withheld=withheld) as s:
        ids = []
        for _ in range(8):
            ids.append(s.save_burst(burst()).image_ids[0])
            clock.advance(1)
        s.mark_uploaded(ids[5])
        s.set_limits(RetentionLocal(max_disk_mb=1, max_age_days=30))
        assert s.limits.max_disk_mb == 1
        summary = s.enforce_quota()
        assert (summary.evicted, summary.purged_unsent) == (1, 2)
        assert [row(s, i).removed_reason for i in ids[:3]] == [
            RemovalReason.DISK_FULL,
            RemovalReason.DISK_FULL,
            None,
        ]
        assert s.stats().bytes_used <= MIB
        assert len(alerts.calls) == 1
        assert len(withheld.calls) == 2
        assert s.enforce_quota().purged_unsent == 0


# -- disk_full alert ----------------------------------------------------------------


def _fill(store: EvidenceStore, clock: FakeClock, n: int = 5) -> list[UUID]:
    ids: list[UUID] = []
    for _ in range(n):
        ids.extend(store.save_burst(burst()).image_ids)
        clock.advance(1)
    return ids


def test_alert_once_per_episode_and_again_after_recovery(
    root: Path, clock: FakeClock, alerts: Alerts
) -> None:
    with _open(root, clock, alerts=alerts, alert_min_interval=timedelta(minutes=10)) as s:
        _fill(s, clock)
        for _ in range(4):  # every save now deletes an unsent image
            s.save_burst(burst())
            clock.advance(1)
        s.save_burst(burst(sizes=(MIB + 1,)))  # and one refusal
        assert len(alerts.calls) == 1
        assert alerts.calls[0]["quota_mb"] == 1
        assert alerts.calls[0]["purged_unsent"] == 1

        # Recover: uploads acknowledged, consent deletions free space.
        for image in s.pending_uploads():
            s.delete_consent_missing(image.image_id)
        s.save_burst(burst())  # no loss and under 90%: episode over
        assert not s.stats().disk_full

        # A new episode inside the minimum interval does not alert again...
        _fill(s, clock, 5)
        assert s.stats().disk_full
        assert len(alerts.calls) == 1
        # ...but one after recovery and past the interval does.
        for image in s.pending_uploads():
            s.delete_consent_missing(image.image_id)
        s.save_burst(burst())
        clock.advance(600)
        _fill(s, clock, 5)
        assert len(alerts.calls) == 2


def test_episode_survives_a_restart(root: Path, clock: FakeClock, alerts: Alerts) -> None:
    with _open(root, clock, alerts=alerts) as s:
        _fill(s, clock, 6)
    assert len(alerts.calls) == 1
    with _open(root, clock, alerts=alerts) as s:
        assert s.stats().disk_full
        _fill(s, clock, 2)
    assert len(alerts.calls) == 1


def test_alert_retried_when_the_sink_fails(
    root: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    sink = Alerts(fail=1)
    with _open(root, clock, alerts=sink) as s:
        _fill(s, clock, 6)
        assert sink.calls == []
        assert not s.stats().disk_full
        assert "could not queue the disk_full alert" in caplog.text
        _fill(s, clock, 1)
        assert len(sink.calls) == 1
        _fill(s, clock, 1)
        assert len(sink.calls) == 1


def test_no_alert_sink_still_tracks_the_episode(root: Path, clock: FakeClock) -> None:
    with _open(root, clock) as s:
        _fill(s, clock, 6)
        assert s.stats().disk_full


def test_listener_failure_is_logged(
    root: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    listener = Withheld(fail=True)
    with _open(root, clock, withheld=listener) as s:
        _fill(s, clock, 6)
    assert len(listener.calls) == 1
    assert "withheld listener failed" in caplog.text


def test_metadata_never_dropped_and_alert_queued_once(root: Path, clock: FakeClock) -> None:
    queue = EventQueue.open(root.parent / "queue.db", InMemoryKeyProvider(KEY), clock)
    with queue:
        for n in range(20):
            queue.enqueue(
                EventType.SCAN,
                ScanPayload(
                    barcode=f"B{n}",
                    state_at_scan=AttentionState.ATTENTIVE,
                    outcome=ScanOutcome.ATTENTIVE,
                ),
                clock.now(),
            )
        s = EvidenceStore.open(
            root,
            InMemoryKeyProvider(KEY),
            clock,
            RetentionLocal(max_disk_mb=1),
            alert_sink=QueueDiskFullAlert(queue, clock),
            disk_free=lambda _: PLENTY,
            min_free_mb=1,
        )
        with s:
            _fill(s, clock, 12)
            s.save_burst(burst(sizes=(MIB + 1,)))
            s.set_limits(RetentionLocal(max_disk_mb=1, max_age_days=1))
            clock.advance(2 * 86400)
            s.housekeeping()
            assert s.stats().images == 0
        # 20 scans plus exactly one local alert; nothing removed.
        assert queue.stats().depth == 21
        claimed = queue.claim_batch()
        types = [c.type for c in claimed]
        assert types.count(EventType.SCAN) == 20
        alert = [c for c in claimed if c.type == EventType.LOCAL_ALERT]
        assert len(alert) == 1
        assert alert[0].payload["kind"] == LocalAlertKind.DISK_FULL


def test_store_api_has_no_queue_access() -> None:
    # The only queue operation reachable from the evidence package is enqueue.
    alert = QueueDiskFullAlert.__init__.__code__.co_names
    assert "enqueue" in alert
    assert not any(
        name in alert for name in ("mark_done", "dead_letter", "purge_dead_letters", "retry")
    )
    assert "EventQueue" not in vars(store_module)


# -- crash safety ----------------------------------------------------------------------


def test_failed_rename_leaves_nothing(
    store: EvidenceStore, root: Path, monkeypatch: pytest.MonkeyPatch, alerts: Alerts
) -> None:
    def crash(src: object, dst: object) -> None:
        raise OSError(errno.EIO, "simulated crash during rename")

    monkeypatch.setattr("ag_workstation.evidence.files.os.replace", crash)
    r = store.save_burst(burst(sizes=(1000, 1000)))
    assert r.image_ids == ()
    assert r.refused == 2
    assert r.images_withheld is ImagesWithheld.DISK_FULL
    assert _files(root) == []
    assert store.stats().images == 0
    assert len(alerts.calls) == 1


def test_disk_full_on_write_is_withheld(
    store: EvidenceStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def enospc(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("ag_workstation.evidence.files.os.fsync", enospc)
    r = store.save_burst(burst())
    assert r.images_withheld is ImagesWithheld.DISK_FULL
    assert _files(root) == []
    monkeypatch.setattr("ag_workstation.evidence.files.os.fsync", real_fsync)
    assert store.save_burst(burst()).images_withheld is ImagesWithheld.NONE


def test_index_failure_removes_the_written_file(
    store: EvidenceStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = EncryptedImageFiles.write

    def write_then_break_index(
        self: EncryptedImageFiles, image_id: UUID, jpeg: bytes, ad: bytes
    ) -> int:
        size = real_write(self, image_id, jpeg, ad)
        store._conn.execute("DROP TABLE evidence_image")  # the insert will fail
        return size

    monkeypatch.setattr(EncryptedImageFiles, "write", write_then_break_index)
    r = store.save_burst(burst())
    assert r.images_withheld is ImagesWithheld.DISK_FULL
    assert _files(root) == []


def test_index_failure_while_making_room(
    store: EvidenceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_make_room", broken)
    assert store.save_burst(burst()).images_withheld is ImagesWithheld.DISK_FULL


def test_recovery_after_crash(
    root: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    with _open(root, clock) as s:
        kept, lost = s.save_burst(burst(sizes=(1000, 1000))).image_ids
    files = EncryptedImageFiles(root / "images", InMemoryKeyProvider(KEY))
    # A write interrupted before the rename, one after the rename but before
    # the index insert, and a file lost under a committed row.
    (root / "images" / f"{TMP_PREFIX}{new_uuid7()}.abcd").write_bytes(b"partial")
    orphan = new_uuid7()
    files.write(orphan, _jpeg(1000), b"ad")
    (root / "images" / "not-a-uuid.agev").write_bytes(b"x")
    files.path_for(lost).unlink()
    with caplog.at_level(logging.WARNING), _open(root, clock) as s:
        names = {p.name for p in _files(root)}
        assert names == {files.path_for(kept).name, "not-a-uuid.agev"}
        assert row(s, lost).removed_reason is RemovalReason.CORRUPT
        assert s.image(orphan) is None
        assert s.read_for_upload(kept).jpeg
        assert [i.image_id for i in s.pending_uploads()] == [kept]
    assert "recovery" in caplog.text


def test_newer_index_version_is_refused(root: Path, clock: FakeClock) -> None:
    _open(root, clock).close()
    with sqlite3.connect(root / "evidence.db") as conn:
        conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        _open(root, clock)


def test_close_twice(root: Path, clock: FakeClock) -> None:
    s = _open(root, clock)
    s.close()
    s.close()


# -- age purge -------------------------------------------------------------------------


def test_age_purge(store: EvidenceStore, clock: FakeClock, root: Path) -> None:
    old = store.save_burst(burst(sizes=(1000, 1000)))
    store.mark_uploaded(old.image_ids[0])
    clock.advance(20 * 86400)
    young = store.save_burst(burst())
    clock.advance(10 * 86400 + 1)
    summary = store.purge_expired()
    assert (summary.expired, summary.expired_unsent) == (2, 1)
    for image_id in old.image_ids:
        assert row(store, image_id).removed_reason is RemovalReason.EXPIRED
    assert row(store, young.image_ids[0]).present
    assert len(_files(root)) == 1
    # Tombstones go one retention period after removal.
    clock.advance(30 * 86400 + 1)
    summary = store.housekeeping()
    assert summary.expired == 1
    assert summary.tombstones_dropped == 2
    assert store.image(old.image_ids[0]) is None
    assert _files(root) == []


def test_withheld_records_expire(store: EvidenceStore, clock: FakeClock) -> None:
    store.save_burst(burst(sizes=(MIB + 1,)))
    assert len(store.withheld_events()) == 1
    clock.advance(31 * 86400)
    store.purge_expired()
    assert store.withheld_events() == []
    assert store.stats().withheld_events == 0
