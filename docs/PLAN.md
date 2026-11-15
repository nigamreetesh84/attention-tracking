# AttentionGuard build plan

Seven phases from the scope's delivery plan (phase 0 is non-software). Each
task fits one sitting. Owners: **architect**, **builder**, **helper** (see
`.claude/agents/`). Design references: `schema.md`, `api-contract.md`,
`detection-engine.md`, `security-privacy.md`.

Every task also has these standing acceptance criteria: tests written first,
`pytest`, `ruff check`, `ruff format --check` and `mypy` pass, no test weakened
or deleted, user-facing strings externalized, one commit, PROGRESS.md ticked.

Each phase ends with a review task (`n.R`) owned by the architect.

---

## Phase 1: Detection prototype

Goal: a single-station engine that turns frames into a station state, with a
replay harness and an accuracy baseline method. No UI, no server.

### 1.1 Tooling and package skeletons (helper)
- Do: create `shared/ag_shared`, `workstation/ag_workstation`,
  `server/ag_server` packages and matching `tests/` folders; make the commands
  in CLAUDE.md work; add a `video` pytest marker and coverage config.
- Accept: all four quality commands pass on the empty tree; editable install
  works.
- Tests: one smoke test per package import.

### 1.2 Shared domain and config models (architect)
- Do: `AttentionState`, severity order, signal types, `Thresholds`,
  `PpeProfile` (six built-in profiles), `CameraConfig`, `CaptureConfig`,
  `EngineConfig`, threshold resolution (defaults -> site -> PPE -> shift ->
  station).
- Accept: defaults equal the scope's trigger table; invalid values rejected.
- Tests: defaults, range validation, resolution order, built-in profile
  signal sets, JSON round trip.

### 1.3 Camera capture layer (builder)
- Do: `CameraSource` interface; OpenCV source for USB, RTSP and built-in;
  `FileSource` for video replay; `FakeSource` for tests; reconnect with
  backoff; per-frame timestamps; latest-frame-wins. ONVIF discovery is an
  interface stub that resolves to an RTSP URI.
- Accept: a failing source reports `offline` and recovers without affecting
  other sources.
- Tests: fake and file sources, reconnect, frame dropping, status reporting.
  Real cameras are mocked.

### 1.4 Landmarks and signal extraction (architect)
- Do: `LandmarkProvider` interface with a MediaPipe implementation (face
  landmarker, pose); signal extractors for presence, quality, EAR, eyes
  closed, PERCLOS, blink rate, yawn, head pose relative to neutral, nod, head
  drop, posture, motion; ROI filter; glare invalidation.
- Accept: extractors are pure functions over landmark data; only the provider
  imports MediaPipe; no identity features computed.
- Tests: synthetic landmark fixtures per signal; window edge cases; validity
  flags; ROI selection.

### 1.5 Per-camera state estimator with PPE profiles (architect)
- Do: candidate state and confidence per camera from signals, timers and the
  active profile, including profile-specific sleeping rules and eye-signal
  fallback.
- Accept: each profile row in `detection-engine.md` section 3 behaves as
  written.
- Tests: synthetic traces per state per profile; timer boundaries; gap
  tolerance; disabled signals are ignored.

### 1.6 Multi-camera fusion (architect)
- Do: pure fusion function implementing rules 1-7 of section 6, including
  `low_confidence` and degraded-camera reporting.
- Accept: works for 1-4 cameras.
- Tests: exhaustive state-pair table for two cameras; presence exception;
  primary Unknown; all offline; three- and four-camera cases.

### 1.7 Station state machine (architect)
- Do: severity ordering, entry timers, recovery hysteresis, break calendar
  (On Break), episode open/close, per-minute buckets, scan-state decision
  (section 5).
- Accept: no flicker at boundaries; break suppresses events; scan decision
  follows the three rules.
- Tests: every row of the scope state table; escalation and recovery; shift
  crossing midnight; scan decision cases including the 40% rule.

### 1.8 Engine runtime (architect)
- Do: threaded pipeline wiring sources, provider, estimator, fusion and state
  machine; state query API; health metrics (fps, quality, restarts); live
  config apply; failure isolation.
- Accept: scan-state query answers from memory in under 200 ms; a crashing
  camera thread does not stop the engine; an invalid config is refused.
- Tests: end-to-end with `FakeSource` and a fake provider; injected faults;
  query latency; config swap.

### 1.9 Evidence ring buffer and burst capture (builder)
- Do: in-memory JPEG ring per camera; burst assembly (key, pre, post frames)
  behind an `EvidenceSink` interface; nothing touches disk here.
- Accept: bounded memory; correct frames selected by timestamp.
- Tests: ring eviction, burst selection, post-frame completion, missing frames.

### 1.10 Replay harness and metrics (architect)
- Do: label file format, video replay through the full pipeline with frame
  time as the clock, metrics (event recall/precision, latency, false alarms
  per shift, confusion matrix, fairness slices), signal-trace recorder, CLI,
  baseline comparison.
- Accept: runs on synthetic traces in CI; `video` tests skip without
  `AG_TEST_VIDEOS`.
- Tests: metric functions on hand-built timelines; label parsing; baseline
  regression check; trace round trip.

### 1.11 Baseline document and fixtures (helper)
- Do: `docs/accuracy-baseline.md` template filled by the harness, sample label
  files, committed synthetic traces, a short "how to record and label videos"
  guide.
- Accept: harness output matches the template.
- Tests: fixture files validate against the label model.

### 1.R Phase review (architect)
- Review the phase diff against `detection-engine.md`; fix findings.

---

## Phase 2: Workstation app

Goal: scan screen, worker login, warn-and-log, encrypted offline queue and
evidence capture, tested against a mock server.

### 2.1 Encrypted local queue (architect)
- Do: SQLite (WAL) queue with AES-256-GCM payloads; `KeyProvider` interface
  with DPAPI and in-memory implementations; record states, dead-letter table,
  `seq` allocation; safe for two processes.
- Accept: no plaintext payload on disk; survives a kill during write.
- Tests: encrypt/decrypt, tamper detection, crash recovery, concurrent
  writers, ordering.

### 2.2 Local evidence store and disk policy (architect)
- Do: encrypted image files, SHA-256, quota tracking, purge order from
  `api-contract.md` section 6, `disk_full` local alert.
- Accept: metadata is never dropped to free space.
- Tests: quota enforcement order, hash check, purge after upload, alert raised.

### 2.3 Service host and IPC (builder)
- Do: Windows service wrapper; engine supervisor with auto-restart within
  5 s; named-pipe IPC for state queries and events; health reporting.
- Accept: killing the engine process restores monitoring; IPC calls time out
  at 150 ms.
- Tests: supervisor restart logic with a fake child; IPC round trip and
  timeout. Windows service registration is mocked and listed for manual check.

### 2.4 Worker login and sessions (architect)
- Do: `LoginMethod` plug-in interface; employee ID + PIN against the cached
  roster (argon2id); lockout; session start/end records; supervisor-assigned
  sessions from a command; PPE override on login.
- Accept: works offline; no face-match code; lockout after 5 failures.
- Tests: success, wrong PIN, lockout and expiry, inactive worker, assignment
  command, session replacement, plug-in registration.

### 2.5 Consent and capture gate (architect)
- Do: single gate deciding whether frames may be written (session exists,
  worker has consent, not on break); wire `EvidenceSink` to the local store;
  set `images_withheld`.
- Accept: no code path writes a frame without passing the gate.
- Tests: each gate condition; property test that no file appears when the
  gate is closed.

### 2.6 Scan flow (builder)
- Do: keyboard-wedge barcode parser (serial/HID behind an interface); record
  the scan first, then query state; outcome `attentive`, `warned` or
  `unmonitored`; warned scans create an event with evidence; repeated-warning
  escalation counter; unattributed scans.
- Accept: the scan is persisted before any engine or network call; engine
  down, timeout or exception still yields an accepted scan.
- Tests: all outcomes, engine timeout, engine exception, no session,
  escalation threshold, break.

### 2.7 String catalog and i18n (helper)
- Do: shared message catalog loader (English), lookup helper for PySide6 and
  Jinja2, a test that fails on hard-coded user-facing strings in UI modules.
- Accept: adding a language is adding a catalog file.
- Tests: lookup, fallback to English, missing-key detection, pseudo-locale.

### 2.8 Scan screen UI (builder)
- Do: full-screen PySide6 screen: status indicator, last item, message area,
  warning banner and sound, "monitoring in progress" label, "Monitoring
  unavailable" banner, live-view-active indicator, login screen with on-screen
  keypad.
- Accept: UI logic lives in testable presenters; the screen stays responsive
  with the service down.
- Tests: presenter tests for every state and banner; `pytest-qt` smoke test.

### 2.9 Sync client (builder)
- Do: client for every device endpoint using `/shared` models; token refresh;
  batch upload, image upload, config pull and atomic apply, heartbeat and
  command handling, clock offset, backoff.
- Accept: follows the result-status and error tables in `api-contract.md`;
  never blocks the scan path.
- Tests: against a fault-injecting fake server: loss, duplication, reordering,
  `retry_later`, `426`, `403 consent_missing`, token expiry, invalid config.

### 2.10 "My record" screen and disputes (builder)
- Do: worker's own counts and events for the current and recent shifts from
  local data; dispute submission queued for the server.
- Accept: shows only the logged-in worker's data.
- Tests: data scoping, dispute queued once, presenter tests.

### 2.11 Fail-open test suite (builder)
- Do: scenario tests across the assembled workstation: camera loss, both
  cameras lost, engine crash, server unreachable, disk full, clock drift.
- Accept: every row of the scope's failure table (4.4) has a passing test in
  which scans keep being accepted.
- Tests: the suite itself.

### 2.R Phase review (architect)

---

## Phase 3: Server and admin

Goal: database, device API, console authentication and RBAC, admin
configuration screens, audit.

### 3.1 Server skeleton and Compose (builder)
- Do: FastAPI app factory, settings, health endpoints, Docker Compose
  (postgres, app, worker, TLS proxy), test database fixture.
- Accept: `docker compose up` serves `/healthz`; refuses production start with
  default secrets.
- Tests: health, settings validation, secrets guard.

### 3.2 Models and initial migration (architect)
- Do: SQLAlchemy models and Alembic migration for every table in `schema.md`;
  seed roles and built-in PPE profiles.
- Accept: migration up and down runs clean; constraints from the schema exist.
- Tests: migration round trip, constraint tests (one primary camera, deletion
  CHECK, unique keys).

### 3.3 Audit log service (architect)
- Do: append-only writer with hash chain, DB trigger and grants, chain
  verification command, helper to log with before/after detail.
- Accept: UPDATE and DELETE on `audit_log` fail for the app role.
- Tests: chain integrity, tamper detection, trigger enforcement, no secrets in
  detail.

### 3.4 Console authentication (architect)
- Do: `AuthProvider` interface with local accounts, argon2id, password policy,
  lockout, TOTP enrollment and verification, recovery codes, server-side
  sessions, CSRF, idle and absolute timeouts.
- Accept: Admin cannot reach any page without MFA.
- Tests: login paths, lockout, MFA required, session expiry and rotation,
  CSRF rejection, cookie flags.

### 3.5 RBAC and scoping (architect)
- Do: permission registry, route dependency, site and line scope filters,
  `public` marker.
- Accept: default deny; every route declares a permission.
- Tests: route-table walk; role x permission matrix; cross-site and
  cross-line access returns 404.

### 3.6 Device enrollment and tokens (architect)
- Do: enrollment token issue, `/enroll`, `/token`, `/secret/rotate`,
  revocation, rate limits.
- Accept: a device token cannot call console routes or write another
  device's data.
- Tests: enrollment once only, expiry, rotation overlap, revocation, scope
  isolation.

### 3.7 Ingest API and evidence store (architect)
- Do: `/events:batch` with idempotency and dependency ordering;
  `/evidence/{id}` with hash check and server-side consent gate;
  `EvidenceStore` with envelope encryption.
- Accept: replays and duplicates leave one row; no image stored for a worker
  without consent.
- Tests: duplicate item, replayed batch, out-of-order parent, bad item in a
  good batch, hash mismatch, consent gate, encryption round trip.

### 3.8 Config bundle and heartbeat (builder)
- Do: bundle builder with resolved thresholds and roster, versioning in
  `workstation_config`, `/config` with ETag, `/heartbeat` updating live status
  and returning commands, minute health samples.
- Accept: any relevant admin change produces a new version.
- Tests: bundle content, version bump triggers, `304`, command delivery and
  acknowledgment.

### 3.9 Console base, layout and i18n (helper)
- Do: Jinja2 base templates, navigation by permission, vendored HTMX, CSP,
  error pages, catalog integration.
- Accept: no inline scripts; no CDN assets.
- Tests: template render smoke tests, CSP header, hard-coded string check.

### 3.10 Admin: sites, lines, workstations, cameras (builder)
- Do: CRUD screens, enrollment token display, camera settings, ROI drawing on
  a still, calibration wizard (neutral pose capture), preview by live still.
- Accept: every change audit-logged; preview is audit-logged as a live view.
- Tests: CRUD, validation, audit entries, permission checks, ROI model.

### 3.11 Admin: users, roles, profiles, shifts (builder)
- Do: users and PIN/password reset, supervisor line assignment, PPE profiles
  including custom, threshold templates, shifts and breaks, site settings
  (rejecting `blocking_mode` and `fail_open = false`).
- Accept: resets force change or re-issue; changes audit-logged.
- Tests: CRUD, validation, reserved settings rejected, audit entries.

### 3.12 Consent registry (architect)
- Do: policy versions per language, record and withdraw consent with artifact
  upload, roster `has_consent` computation, withdrawal purge trigger.
- Accept: a new policy version requires fresh consent.
- Tests: grant, withdraw, version change, roster flag, audit entries.

### 3.13 Audit viewer and system health pages (builder)
- Do: filterable audit log view with chain status; device and server health
  overview.
- Accept: audit view is Admin only.
- Tests: filters, permissions, pagination.

### 3.14 Workstation-server integration tests (builder)
- Do: run the phase 2 sync client against the real app in-process: enroll,
  config, login, scans, events, images, heartbeat.
- Accept: the fault-injection scenarios from 2.9 pass against the real server.
- Tests: the suite itself; contract snapshot test.

### 3.R Phase review (architect)

---

## Phase 4: Supervisor dashboard

Goal: live wall, counters, alerts, event review, live view.

### 4.1 Live status service (builder)
- Do: station status from heartbeats, offline after 15 s, current worker,
  shift counters from buckets.
- Accept: scoped to assigned lines.
- Tests: status transitions, offline detection, scoping, counter sums.

### 4.2 Live wall UI (builder)
- Do: HTMX-polled grid with status color, worker, counters, health badges;
  per-worker shift detail.
- Accept: usable at 100 stations; no image data on the wall.
- Tests: fragment rendering, scoping, polling endpoint performance check.

### 4.3 Alert engine and channels (builder)
- Do: rules for sleeping, absent, repeated warnings, camera offline,
  monitoring unavailable, workstation offline, disk, clock; dedupe; lifecycle;
  `AlertChannel` interface with dashboard and email channels and SMS/WhatsApp
  stubs; delivery retries.
- Accept: late events do not notify; one open alert per dedupe key.
- Tests: each rule, dedupe, escalation window, channel failure and retry,
  recipient selection.

### 4.4 Alert inbox UI (builder)
- Do: list, filter, acknowledge, close; link to event or station.
- Tests: lifecycle actions, scoping, audit entries.

### 4.5 Evidence access service (architect)
- Do: single service that authorizes, audit-logs, decrypts and streams an
  image; consent check at serve time; no-store headers; hash verification.
- Accept: no other code path can read image bytes.
- Tests: audit entry per view, out-of-scope 404, withdrawn consent, tampered
  file, headers.

### 4.6 Event review UI and disputes (builder)
- Do: event list and detail with image burst, Confirm / Mark as false
  positive, notes, dispute queue and resolution.
- Accept: unreviewed events are labeled as such everywhere.
- Tests: review transitions, dispute resolution, scoping, audit entries.

### 4.7 Live view (architect)
- Do: live still request/command/frame flow with in-memory hold; live video
  relay behind the site setting; `live_view_session` rows; audit; time limits.
- Accept: nothing written to disk; video refused when the setting is off.
- Tests: still flow, expiry, setting off, audit entry per view, scoping,
  no-storage assertion.

### 4.8 Supervisor worker assignment (builder)
- Do: assign a worker to a station from the wall; `station_assignment` and
  heartbeat command; status tracking.
- Accept: only for assigned lines; audit-logged.
- Tests: assignment lifecycle, supersede, expiry at shift end, permissions.

### 4.R Phase review (architect)

---

## Phase 5: Reports and retention

Goal: reports, exports, purge jobs, deletion workflow, health monitoring,
backup.

### 5.1 Report queries (builder)
- Do: attention score per worker/shift/line/day; state event counts with
  trends; scan quality; camera uptime; false-positive rate.
- Accept: scoped; unreviewed vs confirmed shown separately.
- Tests: each query on seeded data, score formula, scoping, timezone and
  midnight-shift handling.

### 5.2 Report screens (builder)
- Do: filterable report pages with tables and simple server-rendered charts.
- Tests: rendering, filters, permissions.

### 5.3 Excel and PDF export (builder)
- Do: `xlsx` and `pdf` exporters for every report; export audit entry.
- Tests: file structure, content spot checks, audit entry.

### 5.4 Scheduled email reports (builder)
- Do: `report_schedule` CRUD, scheduler job, email delivery, run history.
- Tests: schedule evaluation, delivery with a fake SMTP, failure recording.

### 5.5 Retention purge jobs (architect)
- Do: daily purge of images, metadata and audit rows per site settings;
  holds for disputes and pending deletions; consent-withdrawal purge;
  `purge_log`; idempotent and resumable.
- Accept: nothing newer than the cutoff is removed; file and key material are
  both gone.
- Tests: cutoffs, holds, resume after failure, log rows, audit chain anchor.

### 5.6 Evidence deletion with dual approval (architect)
- Do: request, approve/reject by a second Admin, execution job, UI.
- Accept: same-user approval impossible at service and DB level.
- Tests: happy path, self-approval, non-Admin approver, role removed before
  decision, single-Admin site, audit entries.

### 5.7 Health monitoring (builder)
- Do: device health history, camera uptime, server self-checks (disk, DB,
  queue lag, scheduler), health alerts.
- Tests: threshold alerts, uptime calculation.

### 5.8 Backup and restore (builder)
- Do: encrypted backup of database and evidence store, restore procedure,
  console trigger, key backup guidance.
- Accept: a restore into an empty stack passes the audit chain check.
- Tests: backup/restore round trip on test data.

### 5.R Phase review (architect)

---

## Phase 6: Pilot readiness (software for the pilot)

Goal: everything needed to run and tune two real stations.

### 6.1 Signal-trace recording in production (architect)
- Do: opt-in, numeric-only trace recording per station with size limits and
  upload; no images.
- Tests: no image data in traces, limits, gate by setting.

### 6.2 Threshold tuning tool (architect)
- Do: replay traces with alternative thresholds; compare event counts and,
  where reviewed labels exist, precision and recall; export a threshold
  profile.
- Tests: replay determinism, comparison metrics.

### 6.3 Remote config push and rollback (builder)
- Do: push a config version to selected stations, track applied versions,
  roll back to a previous version.
- Tests: push, rejection by the client, rollback, audit entries.

### 6.4 Load and soak tests (builder)
- Do: simulator for 100 workstations against the server; 8-hour-equivalent
  accelerated workstation soak.
- Accept: ingest keeps up; memory stays bounded.
- Tests: the simulators, run in a `slow` marker.

### 6.5 Offline queue chaos tests (builder)
- Do: long outage, duplication storms, reordering, partial uploads, clock
  jumps, power loss during write.
- Accept: no lost or duplicated server rows.
- Tests: the suite itself.

### 6.6 Security hardening pass (architect)
- Do: headers, rate limits, TLS settings, secrets handling, dependency audit,
  log scrubbing, authorization re-test of every route.
- Tests: header tests, secret-leak tests, `pip-audit` in the gate.

### 6.7 Pilot runbook and checklists (helper)
- Do: hardware and camera placement, calibration, consent sign-off, fairness
  test protocol, daily checks, tuning procedure, exit criteria.
- Accept: lists every manual check recorded under "Deviations and notes".

### 6.8 Locale readiness (helper)
- Do: pseudo-locale run of both UIs; fix clipped or hard-coded strings; empty
  Hindi and Telugu catalogs with a translation guide.
- Tests: pseudo-locale smoke tests.

### 6.R Phase review (architect)

---

## Phase 7: Rollout (software for deployment)

Goal: repeatable installation, update and operation.

### 7.1 Workstation packaging and silent installer (builder)
- Do: frozen build, MSI with silent install, service registration, service
  account and ACLs, enrollment step.
- Tests: build script checks; install steps listed for manual check.

### 7.2 Auto-update with rollback (architect)
- Do: signed releases, signature verification, staged rollout by channel,
  health check after update, automatic rollback.
- Accept: an unsigned or tampered package is never installed; scanning
  continues during update.
- Tests: signature verification, rollback on failed health check, version
  pinning.

### 7.3 Server deployment package (builder)
- Do: production Compose, TLS setup, secrets templates, migration and upgrade
  procedure, on-premise and private-cloud notes.
- Tests: Compose config validation, migration on a copy of seeded data.

### 7.4 Kiosk and service hardening (builder)
- Do: watchdog, restart on app close, restricted kiosk user guidance, startup
  at boot.
- Tests: watchdog logic; OS-level items listed for manual check.

### 7.5 Manuals (helper)
- Do: admin manual, supervisor guide, installation runbook, troubleshooting
  guide.

### 7.6 Release checklist and versioning (helper)
- Do: version stamping across packages, changelog, release checklist.
- Tests: version consistency test.

### 7.R Phase review (architect)
