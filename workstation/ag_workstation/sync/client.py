"""The workstation sync client (PLAN 2.9, `api-contract.md` sections 4-7).

`SyncClient.tick()` runs one round of every channel that is due and returns
how long the caller may sleep. It never raises: every failure becomes a
channel backoff and a `SyncFlag`. It runs in the service parent on its own
thread (`SyncRunner`), as the queue's single sender, so the scan path never
waits for it: scans are written by the UI process to the local queue and
nothing in this module holds a database transaction across a network call.

Channels and their rules:

- **heartbeat**: every 2 s; on failure 2, 4, 8, 16, then 30 s (jittered).
  Carries acknowledgements of handled commands; commands are executed once
  per id (see `_handle_command`).
- **config**: pulled at start, when the heartbeat advertises another
  version, and on `pull_config`. Validated and installed atomically by
  `ConfigStore`; an invalid bundle is refused, the previous one stays, and
  `rejected_config` rides on every heartbeat until a bundle is applied. A
  refused version is not pulled again unless a `pull_config` names it.
- **upload**: event batches, oldest first, then evidence images (metadata
  before images). Per-item results follow contract 4.3; batch-level errors
  follow section 7: 400/422/413 split the batch until the bad item is alone,
  which is then dead-lettered; 401 refreshes the token once; 403, 404, 409,
  429, 5xx and timeouts keep the batch and resend it unchanged, with the
  same `batch_id`, after a backoff of 1 s to 5 min. Images follow 4.4.
- **426** on any request: `update_required` flag, everything stays buffered,
  and the channel re-probes every 5 minutes.
"""

import logging
import random
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol, TypeVar
from uuid import UUID

from ag_shared.api import (
    MAX_EVIDENCE_BYTES,
    AssignWorkerCommand,
    BatchResult,
    Command,
    ConfigBundle,
    EndSessionCommand,
    EventType,
    HeartbeatRequest,
    HeartbeatResponse,
    ItemResult,
    LiveStillCommand,
    LiveVideoCommand,
    LocalAlertKind,
    LocalAlertPayload,
    PullConfigCommand,
    RawEventBatch,
    RejectedConfig,
    RotateSecretCommand,
    UploadOutcome,
    upload_outcome,
)
from ag_shared.ids import Uuid7Generator
from pydantic import BaseModel, ValidationError

from ag_workstation.clock import Clock
from ag_workstation.evidence.store import (
    EvidenceCorruptError,
    EvidenceUnavailableError,
    StoredImage,
    UploadImage,
)
from ag_workstation.queue.backoff import MAX_BACKOFF_S, backoff_delay
from ag_workstation.queue.store import ApplySummary, ClaimedRecord, DeadLetterReason, QueueStats
from ag_workstation.sync.api import (
    ApiError,
    CredentialSaveError,
    DeviceApi,
    Exchange,
    NotEnrolledError,
)
from ag_workstation.sync.clock_offset import ClockOffsetEstimator, CorrectedClock
from ag_workstation.sync.commands import CommandInbox
from ag_workstation.sync.config_store import ConfigStore
from ag_workstation.sync.status import StationStatus, StatusSource, SyncFlag, SyncHealth
from ag_workstation.sync.transport import TransportError

logger = logging.getLogger(__name__)

_M = TypeVar("_M", bound=BaseModel)

HEARTBEAT_INTERVAL_S: Final = 2.0
HEARTBEAT_MAX_S: Final = 30.0
UPLOAD_IDLE_S: Final = 1.0
"""How often an empty queue is looked at again."""
UPDATE_PROBE_S: Final = MAX_BACKOFF_S
NOT_ENROLLED_SLEEP_S: Final = 5.0
MAX_SLEEP_S: Final = 1.0
IMAGES_PER_TICK: Final = 5
RECENT_COMMAND_S: Final = 600.0
"""Executed command ids are remembered this long after their ack was delivered."""
DRIFT_ALERT_MIN_INTERVAL_S: Final = 3600.0


class SyncQueue(Protocol):
    """The parts of `EventQueue` the client uses."""

    def claim_batch(self) -> list[ClaimedRecord]:
        """Claim the oldest due records."""
        ...

    def apply_results(
        self, claimed: Sequence[ClaimedRecord], results: Sequence[ItemResult]
    ) -> ApplySummary:
        """Apply per-item results."""
        ...

    def dead_letter(
        self, record_id: UUID, reason: DeadLetterReason, detail: str | None = None
    ) -> bool:
        """Dead-letter one record."""
        ...

    def stats(self) -> QueueStats:
        """Depth and oldest unsent."""
        ...

    def enqueue(self, item_type: str, payload: BaseModel, occurred_at: datetime) -> object:
        """Queue a record (local alerts)."""
        ...


class SyncEvidence(Protocol):
    """The parts of `EvidenceStore` the client uses."""

    def pending_uploads(self, limit: int = 50) -> list[StoredImage]:
        """Held, not yet uploaded images."""
        ...

    def read_for_upload(self, image_id: UUID) -> UploadImage:
        """Decrypt and verify one image."""
        ...

    def mark_uploaded(self, image_id: UUID) -> bool:
        """Record a 200/201."""
        ...

    def delete_consent_missing(self, image_id: UUID) -> int:
        """Handle 403 consent_missing."""
        ...


@dataclass(slots=True)
class _Channel:
    name: str
    due: float = 0.0
    failures: int = 0
    flags: set[SyncFlag] = field(default_factory=set)
    offline: bool = False


@dataclass(slots=True)
class _Batch:
    batch_id: UUID
    records: list[ClaimedRecord]


@dataclass(slots=True)
class _ImageRetry:
    failures: int
    due: float


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Tunables (defaults follow the contract)."""

    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S
    heartbeat_max_s: float = HEARTBEAT_MAX_S
    upload_idle_s: float = UPLOAD_IDLE_S
    images_per_tick: int = IMAGES_PER_TICK


class SyncClient:
    """Drives every device endpoint from `tick()`. Use from one thread."""

    def __init__(
        self,
        *,
        api: DeviceApi,
        queue: SyncQueue,
        config_store: ConfigStore,
        clock: Clock,
        offset: ClockOffsetEstimator,
        evidence: SyncEvidence | None = None,
        inbox: CommandInbox | None = None,
        status: StatusSource | None = None,
        rng: Callable[[], float] | None = None,
        config: SyncConfig | None = None,
        on_config_applied: Callable[[ConfigBundle], None] | None = None,
    ) -> None:
        self._api = api
        self._queue = queue
        self._config_store = config_store
        self._clock = clock
        self._offset = offset
        self._utc = CorrectedClock(clock, offset)
        self._evidence = evidence
        self._inbox = inbox
        self._status = status
        self._rng = rng or random.SystemRandom().random
        self._cfg = config or SyncConfig()
        self._on_config_applied = on_config_applied
        self._ids = Uuid7Generator(now=self._utc.now)

        self._heartbeat = _Channel("heartbeat")
        self._config = _Channel("config")
        self._upload = _Channel("upload")
        self._channels = (self._heartbeat, self._config, self._upload)
        self._flags: set[SyncFlag] = set()
        self._last_contact: datetime | None = None
        self._hb_seq = 0

        self._config_wanted = True
        self._advertised_version: int | None = None
        self._rejected: RejectedConfig | None = None
        self._config_commands: set[UUID] = set()

        self._ready_acks: set[UUID] = set()
        self._recent: dict[UUID, float] = {}

        self._batches: deque[_Batch] = deque()
        self._image_retry: dict[UUID, _ImageRetry] = {}
        self._image_dead: set[UUID] = set()

        self._drift_alerted = False
        self._last_drift_alert: float | None = None

    # -- public --------------------------------------------------------------

    def tick(self) -> float:
        """Run every due channel once; returns seconds until the next is due. Never raises."""
        try:
            return self._tick()
        except Exception as exc:  # the boundary: nothing escapes the sync loop
            logger.exception("sync round failed (%s)", type(exc).__name__)
            self._flags.add(SyncFlag.SYNC_ERROR)
            return MAX_SLEEP_S

    def health(self) -> SyncHealth:
        """Current flags and figures."""
        flags = set(self._flags)
        for channel in self._channels:
            flags |= channel.flags
        return SyncHealth(
            flags=frozenset(flags),
            last_contact_at=self._last_contact,
            clock_offset_ms=self._offset.smoothed_ms,
            applied_config_version=self._config_store.applied_version,
            rejected_config=self._rejected,
            evidence_rejected=len(self._image_dead),
        )

    @property
    def utc(self) -> CorrectedClock:
        """The server-corrected clock."""
        return self._utc

    def request_config(self) -> None:
        """Pull the config bundle at the next opportunity."""
        self._config_wanted = True
        self._config.due = min(self._config.due, self._clock.monotonic())

    # -- round ---------------------------------------------------------------

    def _tick(self) -> float:
        try:
            enrolled = self._api.credentials() is not None
        except Exception as exc:
            logger.error("device credentials unreadable (%s)", type(exc).__name__)
            self._flags.add(SyncFlag.CREDENTIAL_STORE_FAILED)
            return NOT_ENROLLED_SLEEP_S
        if not enrolled:
            self._flags.add(SyncFlag.NOT_ENROLLED)
            return NOT_ENROLLED_SLEEP_S
        self._flags.discard(SyncFlag.NOT_ENROLLED)
        if self._api.has_unsaved_credentials and self._api.retry_credential_save():
            self._flags.discard(SyncFlag.CREDENTIAL_STORE_FAILED)
        if not self._api.has_unsaved_credentials:
            self._flags.discard(SyncFlag.CREDENTIAL_STORE_FAILED)
        self._flags.discard(SyncFlag.SYNC_ERROR)

        if self._clock.monotonic() >= self._heartbeat.due:
            self._guarded(self._heartbeat, self._send_heartbeat)
        if self._config_wanted and self._clock.monotonic() >= self._config.due:
            self._guarded(self._config, self._pull_config)
        if self._clock.monotonic() >= self._upload.due:
            self._guarded(self._upload, self._upload_once)
        due = [self._heartbeat.due, self._upload.due]
        if self._config_wanted:
            due.append(self._config.due)
        return max(0.0, min(MAX_SLEEP_S, min(due) - self._clock.monotonic()))

    def _guarded(self, channel: _Channel, step: Callable[[], None]) -> None:
        try:
            step()
        except NotEnrolledError:
            self._flags.add(SyncFlag.NOT_ENROLLED)
            self._fail(channel, None)
        except TransportError as exc:
            logger.info("%s: no response (%s)", channel.name, exc)
            self._fail(channel, None)
        except ApiError as exc:
            self._fail(channel, exc.status, exc.code)
        except Exception as exc:
            logger.exception("%s step failed (%s)", channel.name, type(exc).__name__)
            self._fail(channel, None, offline=False)
            channel.flags.add(SyncFlag.SYNC_ERROR)

    # -- backoff -------------------------------------------------------------

    def _delay(self, channel: _Channel) -> float:
        if channel is self._heartbeat:
            step = backoff_delay(channel.failures + 1, self._rng)
            return min(self._cfg.heartbeat_max_s, max(self._cfg.heartbeat_interval_s, step))
        return backoff_delay(channel.failures, self._rng)

    def _fail(
        self,
        channel: _Channel,
        status: int | None,
        code: str | None = None,
        *,
        offline: bool = True,
    ) -> None:
        """Apply the section 7 reaction for a failed request on `channel`."""
        channel.failures += 1
        channel.flags.clear()
        channel.offline = status is None and offline
        delay = self._delay(channel)
        if status is None:
            if offline:
                channel.flags.add(SyncFlag.OFFLINE)
        elif status == 401:
            flag = SyncFlag.DEVICE_REVOKED if code == "device_revoked" else SyncFlag.AUTH_FAILED
            channel.flags.add(flag)
            if flag is SyncFlag.DEVICE_REVOKED:
                delay = MAX_BACKOFF_S
        elif status == 403:
            channel.flags.add(SyncFlag.FORBIDDEN)
        elif status == 426:
            channel.flags.add(SyncFlag.UPDATE_REQUIRED)
            delay = UPDATE_PROBE_S
        elif status == 429 or status >= 500:
            channel.flags.add(SyncFlag.SERVER_ERROR)
        channel.due = self._clock.monotonic() + delay
        logger.info(
            "%s failed (%s); next attempt in %.1f s",
            channel.name,
            "no response" if status is None else status,
            delay,
        )

    def _succeed(self, channel: _Channel, next_in: float) -> None:
        was_offline = channel.offline
        channel.failures = 0
        channel.flags.clear()
        channel.offline = False
        now = self._clock.monotonic()
        channel.due = now + next_in
        self._last_contact = self._clock.now()
        if was_offline:
            # The server is reachable again: channels that were waiting out a
            # network backoff try now instead of sleeping up to 5 minutes.
            for other in self._channels:
                if other is not channel and other.offline:
                    other.due = min(other.due, now)

    def _contact(self) -> None:
        """Any answer from the server (even an error) proves the network is up."""
        self._last_contact = self._clock.now()

    # -- heartbeat -----------------------------------------------------------

    def _station_status(self) -> StationStatus:
        if self._status is None:
            return StationStatus()
        try:
            return self._status.station_status()
        except Exception as exc:
            logger.warning("station status unavailable (%s)", type(exc).__name__)
            return StationStatus()

    def _pending_acks(self) -> list[UUID]:
        acks = set(self._ready_acks)
        if self._inbox is not None:
            try:
                acks.update(self._inbox.handled_unacked())
            except Exception as exc:
                logger.warning("command inbox unreadable (%s)", type(exc).__name__)
        return sorted(acks)

    def _send_heartbeat(self) -> None:
        status = self._station_status()
        try:
            stats: QueueStats | None = self._queue.stats()
        except Exception as exc:
            logger.warning("queue stats unavailable (%s)", type(exc).__name__)
            stats = None
        acks = self._pending_acks()
        self._hb_seq += 1
        request = HeartbeatRequest(
            seq=self._hb_seq,
            sent_at=self._utc.now(),
            state=status.state,
            confidence=status.confidence,
            low_confidence=status.low_confidence,
            worker_user_id=status.worker_user_id,
            session_id=status.session_id,
            cameras=status.cameras,
            engine_status=status.engine_status,
            queue_depth=0 if stats is None else stats.depth,
            oldest_unsent_at=None if stats is None else stats.oldest_unsent_at,
            disk_free_mb=status.disk_free_mb,
            clock_offset_ms=self._offset.smoothed_ms,
            applied_config_version=self._config_store.applied_version,
            shift_counters=status.shift_counters,
            acked_commands=tuple(acks),
            rejected_config=self._rejected,
        )
        ex = self._api.send("POST", "/heartbeat", body=request.model_dump_json().encode())
        if ex.status != 200:
            self._contact()
            self._fail(self._heartbeat, ex.status, ex.problem_code)
            return
        reply = self._parse(HeartbeatResponse, ex)
        if reply is None:
            self._fail(self._heartbeat, 502)
            return
        self._api.sample_clock(reply.server_time, ex)
        self._check_drift()
        self._acks_delivered(acks)
        self._succeed(self._heartbeat, self._cfg.heartbeat_interval_s)
        applied = self._config_store.applied_version
        if reply.config_version != applied and (
            self._rejected is None or self._rejected.version != reply.config_version
        ):
            self._advertised_version = reply.config_version
            self.request_config()
        for command in reply.commands:
            self._handle_command(command)

    def _parse(self, model: type[_M], ex: Exchange) -> _M | None:
        try:
            return model.model_validate_json(ex.response.content)
        except ValidationError:
            logger.warning("unreadable %s from the server", model.__name__)
            return None

    def _acks_delivered(self, acks: list[UUID]) -> None:
        now = self._clock.monotonic()
        for command_id in acks:
            self._ready_acks.discard(command_id)
            self._recent[command_id] = now
        if self._inbox is not None and acks:
            try:
                self._inbox.mark_acked(acks)
            except Exception as exc:
                logger.warning("command acks not recorded (%s)", type(exc).__name__)
        cutoff = now - RECENT_COMMAND_S
        for command_id in [c for c, t in self._recent.items() if t < cutoff]:
            del self._recent[command_id]

    def _handle_command(self, command: Command) -> None:
        """Execute a command once per id; acknowledge it when its action is final.

        - `assign_worker`, `end_session`: into the inbox for the UI process;
          acknowledged once the UI handled them.
        - `pull_config`: acknowledged after the pull ends (applied, 304 or refused).
        - `rotate_secret`: acknowledged once the server issued a new secret.
        - `live_still`, `live_video` (live view is phase 4.7) and unknown
          types: logged and acknowledged without action, so the server stops
          repeating them.
        """
        cid = command.id
        if cid in self._ready_acks or cid in self._recent or cid in self._config_commands:
            return
        if isinstance(command, AssignWorkerCommand | EndSessionCommand):
            if self._inbox is None:
                logger.warning("no command inbox; %s %s not handled", command.type, cid)
                return
            self._inbox.put(command)
        elif isinstance(command, PullConfigCommand):
            self._config_commands.add(cid)
            self._advertised_version = command.version
            self.request_config()
        elif isinstance(command, RotateSecretCommand):
            try:
                self._api.rotate_secret()
            except CredentialSaveError:
                self._flags.add(SyncFlag.CREDENTIAL_STORE_FAILED)
            except (ApiError, TransportError) as exc:
                logger.warning("secret rotation failed (%s); waiting for the repeat", exc)
                return
            self._ready_acks.add(cid)
        elif isinstance(command, LiveStillCommand | LiveVideoCommand):
            logger.info(
                "live view command %s (%s) not supported yet; acknowledged", cid, command.type
            )
            self._ready_acks.add(cid)
        else:
            self._ready_acks.add(cid)

    def _check_drift(self) -> None:
        if not self._offset.drift:
            if self._drift_alerted:
                logger.info("clock drift cleared")
            self._drift_alerted = False
            self._flags.discard(SyncFlag.CLOCK_DRIFT)
            return
        self._flags.add(SyncFlag.CLOCK_DRIFT)
        if self._drift_alerted:
            return
        self._drift_alerted = True
        now = self._clock.monotonic()
        if (
            self._last_drift_alert is not None
            and now - self._last_drift_alert < DRIFT_ALERT_MIN_INTERVAL_S
        ):
            return
        self._last_drift_alert = now
        raw = self._offset.raw_ms or 0
        logger.warning("device clock is %d ms off the server", raw)
        try:
            self._queue.enqueue(
                EventType.LOCAL_ALERT,
                LocalAlertPayload(kind=LocalAlertKind.CLOCK_DRIFT, detail={"offset_ms": raw}),
                self._utc.now(),
            )
        except Exception as exc:
            logger.error("clock drift alert not queued (%s)", type(exc).__name__)

    # -- config --------------------------------------------------------------

    def _pull_config(self) -> None:
        applied = self._config_store.applied_version
        headers = {} if applied is None else {"If-None-Match": f'"{applied}"'}
        ex = self._api.send("GET", "/config", content_type=None, extra_headers=headers)
        if ex.status == 304:
            self._config_done()
            return
        if ex.status != 200:
            self._contact()
            self._fail(self._config, ex.status, ex.problem_code)
            return
        result = self._config_store.apply(
            ex.response.content, advertised_version=self._advertised_version
        )
        if result.applied and result.bundle is not None:
            self._rejected = None
            self._flags.discard(SyncFlag.CONFIG_REJECTED)
            if self._on_config_applied is not None:
                try:
                    self._on_config_applied(result.bundle)
                except Exception as exc:
                    logger.error("config listener failed (%s)", type(exc).__name__)
        else:
            self._rejected = result.rejected
            self._flags.add(SyncFlag.CONFIG_REJECTED)
        self._config_done()

    def _config_done(self) -> None:
        self._config_wanted = False
        self._ready_acks.update(self._config_commands)
        self._config_commands.clear()
        self._succeed(self._config, 0.0)

    # -- upload --------------------------------------------------------------

    def _upload_once(self) -> None:
        if not self._batches:
            claimed = self._queue.claim_batch()
            if claimed:
                self._batches.append(_Batch(self._ids(), claimed))
        if self._batches:
            self._send_batch(self._batches[0])
            return
        if self._upload_images():
            return
        self._succeed(self._upload, self._cfg.upload_idle_s)

    def _send_batch(self, batch: _Batch) -> None:
        body = RawEventBatch(
            batch_id=batch.batch_id,
            sent_at=self._utc.now(),
            items=tuple(r.to_item() for r in batch.records),
        )
        ex = self._api.send("POST", "/events:batch", body=body.model_dump_json().encode())
        if ex.status == 200:
            reply = self._parse(BatchResult, ex)
            if reply is None:
                self._fail(self._upload, 502)
                return
            if reply.batch_id != batch.batch_id:
                logger.warning("batch result names another batch id; applying by item id")
            self._api.sample_clock(reply.server_time, ex)
            summary = self._queue.apply_results(batch.records, reply.results)
            self._batches.popleft()
            logger.info(
                "batch of %d: %d done, %d dead-lettered, %d to retry",
                len(batch.records),
                summary.done,
                summary.dead,
                summary.retry,
            )
            self._succeed(self._upload, 0.0)
            return
        self._contact()
        if ex.status in (400, 413, 422):
            self._split_or_dead_letter(batch, ex)
            self._succeed(self._upload, 0.0)
            return
        self._fail(self._upload, ex.status, ex.problem_code)

    def _split_or_dead_letter(self, batch: _Batch, ex: Exchange) -> None:
        self._batches.popleft()
        if len(batch.records) == 1:
            record = batch.records[0]
            detail = f"HTTP {ex.status}" + (f" {ex.problem_code}" if ex.problem_code else "")
            self._queue.dead_letter(record.id, DeadLetterReason.REJECTED, detail)
            return
        half = len(batch.records) // 2
        # New batch ids: the halves are different batches from the server's view.
        self._batches.appendleft(_Batch(self._ids(), batch.records[half:]))
        self._batches.appendleft(_Batch(self._ids(), batch.records[:half]))
        logger.info("batch of %d answered %d; split in two", len(batch.records), ex.status)

    def _upload_images(self) -> bool:
        """Upload due images; True if any request was made."""
        evidence = self._evidence
        if evidence is None:
            return False
        now = self._clock.monotonic()
        sent = 0
        for image in evidence.pending_uploads():
            if sent >= self._cfg.images_per_tick:
                break
            iid = image.image_id
            retry = self._image_retry.get(iid)
            if iid in self._image_dead or (retry is not None and retry.due > now):
                continue
            try:
                upload = evidence.read_for_upload(iid)
            except (EvidenceUnavailableError, EvidenceCorruptError):
                continue  # the store already removed or tombstoned it
            if len(upload.jpeg) > MAX_EVIDENCE_BYTES:
                logger.error("evidence image %s is over 2 MB; not uploaded", iid)
                self._image_dead.add(iid)
                continue
            sent += 1
            ex = self._api.send(
                "PUT",
                f"/evidence/{iid}",
                body=upload.jpeg,
                content_type="image/jpeg",
                extra_headers=upload.headers.to_headers(),
            )
            if not self._image_outcome(evidence, iid, ex):
                return True
        if sent:
            self._succeed(self._upload, 0.0)
        return sent > 0

    def _image_outcome(self, evidence: SyncEvidence, image_id: UUID, ex: Exchange) -> bool:
        """Apply contract 4.4; False when the channel must back off."""
        code = ex.problem_code
        outcome = upload_outcome(ex.status, code)
        if outcome is UploadOutcome.DONE:
            evidence.mark_uploaded(image_id)
            self._image_retry.pop(image_id, None)
            return True
        self._contact()
        if outcome is UploadOutcome.DELETE_LOCAL:
            evidence.delete_consent_missing(image_id)
            self._image_retry.pop(image_id, None)
            return True
        if outcome in (UploadOutcome.HASH_MISMATCH, UploadOutcome.DEAD_LETTER):
            logger.error("evidence image %s refused permanently (HTTP %d)", image_id, ex.status)
            self._image_dead.add(image_id)
            self._image_retry.pop(image_id, None)
            return True
        if ex.status == 404:
            previous = self._image_retry.get(image_id)
            failures = 1 if previous is None else previous.failures + 1
            due = self._clock.monotonic() + backoff_delay(failures, self._rng)
            self._image_retry[image_id] = _ImageRetry(failures, due)
            return True
        self._fail(self._upload, ex.status, code)
        return False
