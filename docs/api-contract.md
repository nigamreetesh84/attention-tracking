# Workstation-server API contract (v1)

This is the contract between the workstation service and the central server.
Pydantic models for every payload live in `/shared` and are imported by both
sides, so the contract is enforced by types and by contract tests. Changing it
needs the owner's approval (see CLAUDE.md).

## 1. Principles

- **The workstation never depends on the server to function.** Every call may
  fail or time out; the client buffers and retries. No server response can
  block a scan.
- **At-least-once delivery, idempotent ingest.** The client may send anything
  twice. The server must produce the same end state.
- **The client pulls; the server never calls the workstation.** Config,
  commands and assignments ride on heartbeat and config responses, so
  workstations need outbound HTTPS only.
- HTTPS only (TLS 1.2+). JSON bodies, UTF-8, `snake_case` keys, UTC ISO-8601
  timestamps with millisecond precision.

## 2. Versioning

- URL prefix `/api/v1/device/`. A breaking change means `/api/v2/`; the server
  serves the current and previous major version side by side.
- Every request carries `X-AG-Schema: <int>` (payload schema revision within
  the major version) and `X-AG-App-Version`.
- Additive changes only within a major version. Both sides ignore unknown
  fields. Enums may gain values; receivers map unknown values to a safe
  default and log them.
- If the server no longer supports a client's schema it answers `426` with a
  problem body. The client keeps scanning and buffering, and raises a local
  "update required" health flag. Buffered data is uploaded after the update;
  queue records store their schema revision so they can be migrated.

## 3. Authentication

1. **Enrollment.** An Admin creates a workstation in the console and gets a
   one-time enrollment token (valid 24 h, stored hashed).
   `POST /enroll` with `{enrollment_token, hostname, hardware_fingerprint}`
   returns `{workstation_id, device_secret, server_time}`. The secret is 256
   random bits, shown once, stored on the device with DPAPI and on the server
   as a hash.
2. **Access token.** `POST /token` with `{workstation_id, device_secret}`
   returns `{access_token, expires_in}`: a signed JWT (15 minutes) with
   `sub = workstation_id`, `site_id`, `scope = device`. All other endpoints
   need `Authorization: Bearer`.
3. **Rotation and revocation.** `POST /secret/rotate` issues a new secret; the
   old one stays valid for 10 minutes. An Admin can revoke a device; its
   tokens then fail with `401 device_revoked` and the client keeps working
   offline and shows a health warning.
4. Device tokens grant device endpoints only. They can never reach console
   routes. A device can write only its own data: the server takes
   `workstation_id` from the token, never from the body.
5. Rate limits: `/enroll` and `/token` are limited per IP and per workstation.

Workers do not authenticate to the server. Worker login happens on the
workstation against the cached roster (section 6) so it works offline.

## 4. Endpoints

| Method and path | Purpose |
|---|---|
| `POST /enroll` | One-time device enrollment |
| `POST /token` | Get an access token |
| `POST /secret/rotate` | Rotate the device secret |
| `POST /heartbeat` | Live status and health; returns commands |
| `GET /config` | Pull the config bundle |
| `POST /events:batch` | Upload queued records |
| `PUT /evidence/{image_id}` | Upload one evidence image |
| `POST /live/{request_id}/frame` | Answer a live still request |
| `WS /live/{session_id}/stream` | Live video relay (only if the site enables it) |
| `GET /release/{version}` | Download a signed app update (phase 7) |

### 4.1 Heartbeat

`POST /heartbeat` every 2 s while online (backs off to 30 s on failure).

Request: `{seq, sent_at, state, confidence, low_confidence, worker_user_id,
session_id, cameras: [{slot, status, fps, quality}], engine_status,
queue_depth, oldest_unsent_at, disk_free_mb, clock_offset_ms,
applied_config_version, shift_counters: {state: count}}`.

Response: `{server_time, config_version, commands: [...]}`. Commands:

| Command | Payload | Client action |
|---|---|---|
| `assign_worker` | `assignment_id, user_id, valid_until` | Start a `supervisor_assigned` session |
| `end_session` | `session_id` | End the worker session |
| `live_still` | `request_id, camera_slot, expires_at` | Post one frame, do not store it |
| `live_video` | `session_id, camera_slot, expires_at` | Open the stream if the site setting allows |
| `pull_config` | `version` | Fetch config now |
| `rotate_secret` | | Call `/secret/rotate` |

Commands are repeated until acknowledged in the next heartbeat
(`acked_commands: [id]`), so they are idempotent by `command id`.

The live wall treats a workstation as offline after 15 s without a heartbeat.

### 4.2 Config

`GET /config` with `If-None-Match: "<version>"` returns `304` or the bundle:

```
{ version, generated_at,
  site: {timezone, settings},
  workstation: {id, name, line_id, camera_count},
  cameras: [{slot, role, kind, uri, roi, calibration, is_enabled}],
  ppe_profile: {...}, thresholds: {... fully resolved ...},
  shift_thresholds: [{shift_id, thresholds}],
  profile_thresholds: [{ppe_profile_id, ppe_profile: {...}, thresholds,
                        shift_thresholds: [{shift_id, thresholds}]}],
  shifts: [{id, name, start_time, end_time, days_of_week, breaks: [...]}],
  roster: [{user_id, employee_id, display_name, pin_hash, has_consent,
            ppe_profile_override, preferred_language, is_active}],
  consent_policy: {version, language, body},
  capture: {pre_frames, post_frames, frame_interval_ms, jpeg_quality},
  retention_local: {max_age_days, max_disk_mb},
  target_app_version }
```

- The client validates the bundle, applies it atomically and reports
  `applied_config_version`. An invalid bundle is rejected and the previous
  one stays active (fail-open); the rejection is reported in the heartbeat.
- `profile_thresholds` (added 2026-11-20, owner-approved; optional, default
  empty) lists every PPE profile a roster `ppe_profile_override` (a
  `ppe_profile` id) refers to, with thresholds fully resolved with that
  profile as the PPE layer, so an override applies offline. Lookup for a
  worker and shift: the override's entry (its shift entry, else its
  `thresholds`); if there is no override or no entry for it, the station
  `ppe_profile` with its shift entry, else `thresholds`.
- The roster holds only active workers of the workstation's site. `pin_hash`
  is argon2id. The bundle is stored encrypted on the device.
- Camera URIs may contain credentials, so the bundle is sensitive in transit
  (TLS) and at rest (encrypted).

### 4.3 Event batch

`POST /events:batch`, at most 200 items or 1 MB.

```
{ batch_id: uuid, sent_at, items: [
    { type, id: uuidv7, seq: int, occurred_at, payload: {...} } ] }
```

| `type` | Payload (main fields) |
|---|---|
| `session_start` | `user_id, method, station_assignment_id` |
| `session_end` | `session_id, end_reason` |
| `scan` | `session_id, user_id, barcode, state_at_scan, confidence, outcome` |
| `attention_event` | `kind, state, confidence, low_confidence, started_at, ended_at, scan_id, session_id, user_id, signals, camera_states, engine_version, config_version, images_withheld, image_ids: [...]` |
| `bucket` | `bucket_start, user_id, seconds: {state: n}` |
| `dispute` | `attention_event_id, user_id, message` |
| `health` | one-minute health sample |
| `local_alert` | `kind, detail` (camera offline, disk full, clock drift, engine restart) |

Response `200`:

```
{ batch_id, server_time, results: [
    { id, status: accepted | duplicate | rejected | retry_later, reason } ] }
```

- `accepted` and `duplicate`: the client deletes the queue record.
- `rejected`: permanent (validation failure). The client moves the record to a
  dead-letter table, keeps it for 30 days and reports the count in health.
- `retry_later`: a parent is missing (for example an event whose scan has not
  arrived). The client keeps the record and retries.
- A non-2xx response or timeout means nothing is assumed; the same batch is
  sent again with the same `batch_id`.

### 4.4 Evidence upload

`PUT /evidence/{image_id}`, body `image/jpeg`, headers `X-AG-Event-Id`,
`X-AG-Camera-Slot`, `X-AG-Seq`, `X-AG-Offset-Ms`, `X-AG-Captured-At`,
`X-AG-SHA256`. Max 2 MB.

- `201` stored; `200` already stored with the same hash (idempotent);
  `409 hash_mismatch` if the id exists with a different hash;
  `404 event_unknown` means retry later (send the event first);
  `403 consent_missing` is permanent: the client deletes the local image.
- The server checks consent again at ingest. A workstation with a stale roster
  can therefore never cause an image of a non-consenting worker to be stored.
- Images are uploaded after metadata and at lower priority.

### 4.5 Live view

- **Still (default).** A supervisor's request creates a `live_still` command.
  The client captures one frame and posts it to `/live/{request_id}/frame`.
  The server holds it in memory for 30 s for that one viewer and never writes
  it to disk. The view is audit-logged when requested.
- **Video (site setting, off by default).** The client opens the WebSocket and
  sends JPEG frames (max 5 fps, max 640 px wide). The server relays them to the
  viewer as MJPEG and stores nothing. Sessions end after 2 minutes unless
  renewed; every start is audit-logged.
- The workstation shows a "live view active" indicator while frames are sent.

## 5. Idempotency rules

1. Every record has a client-generated UUIDv7 `id`, which is the server-side
   primary key. Insert is `ON CONFLICT (id) DO NOTHING`; a conflict returns
   `duplicate`.
2. `batch_id` is stored in `ingest_batch` for 30 days. A replayed batch returns
   the stored result without reprocessing.
3. Items inside a batch are processed in dependency order (session, scan,
   event, the rest), each in its own savepoint, so one bad item cannot fail
   the batch.
4. Buckets are upserted by (`workstation_id`, `bucket_start`, `user_id`); the
   latest value wins, which is safe because the client sends a bucket only
   after its minute is closed.
5. The server never trusts `seq` for uniqueness; it uses it to detect gaps and
   report `missing_seq_ranges` in health for diagnostics.
6. Out-of-order arrival is normal. Alerts created from late events are marked
   `late` and do not notify if older than 10 minutes.

## 6. Offline sync

**Local queue.** SQLite (WAL) on the workstation. Each record: `id`, `seq`,
`type`, `schema`, `created_at`, `status` (`pending`, `sending`, `dead`),
`attempts`, `next_attempt_at`, and an AES-256-GCM encrypted payload. Evidence
images are separate encrypted files referenced by `image_id`. The data key is
protected with Windows DPAPI.

**Send loop.** Oldest first by `seq`; metadata before images. Exponential
backoff with jitter from 1 s to 5 min on failure. `sending` records return to
`pending` on restart. A record is deleted only after `accepted`/`duplicate`.

**Limits (fail-open).** When disk use passes the configured quota: delete the
oldest already-uploaded images, then the oldest unsent images (the event keeps
`images_withheld = disk_full`), and only then stop saving new images. Scan and
event metadata are never dropped to make space. A `disk_full` local alert is
queued.

**Offline login.** The roster in the config bundle lets employee ID + PIN
login work without the server. PIN failures lock the worker on that station
for 5 minutes after 5 attempts. Roster changes (new worker, deactivation, PIN
reset, consent change) reach the station on the next config pull; a station
that has been offline uses its last roster.

**Clock.** Every response carries `server_time`. The client keeps a smoothed
offset, reports it, stamps records with corrected UTC, and raises
`clock_drift` if the raw offset exceeds 2 s. Windows time sync to the site NTP
source is part of the install runbook.

## 7. Errors

Errors use `application/problem+json`: `{type, title, status, detail, code}`.

| Status | Client behavior |
|---|---|
| `400`, `422` | Permanent for that item; dead-letter |
| `401` | Refresh the token once, then back off |
| `403` | Permanent; report in health |
| `404` on a parent | Retry later |
| `409` | See the endpoint rules |
| `413` | Split the batch |
| `426` | Update required; keep buffering |
| `429`, `5xx`, timeout | Back off and retry |

## 8. Contract tests

- `/shared` models are the single definition; a snapshot test fails when the
  JSON schema changes without a schema revision bump.
- The sync client is tested against the real FastAPI app in-process (phase 3)
  and against a fault-injecting fake (loss, duplication, reordering, slow
  responses) in phases 2 and 6.
