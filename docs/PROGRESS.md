# AttentionGuard progress

Tick a task only when its acceptance criteria in `PLAN.md` are met, the
quality gate passes and the work is committed.

## Phase 1: Detection prototype
- [x] 1.1 Tooling and package skeletons (helper)
- [x] 1.2 Shared domain and config models (architect)
- [x] 1.3 Camera capture layer (builder)
- [x] 1.4 Landmarks and signal extraction (architect)
- [x] 1.5 Per-camera state estimator with PPE profiles (architect)
- [x] 1.6 Multi-camera fusion (architect)
- [x] 1.7 Station state machine (architect)
- [x] 1.8 Engine runtime (architect)
- [x] 1.9 Evidence ring buffer and burst capture (builder)
- [x] 1.10 Replay harness and metrics (architect)
- [x] 1.11 Baseline document and fixtures (helper)
- [x] 1.R Phase review (architect)

## Phase 2: Workstation app
- [ ] 2.1 Encrypted local queue (architect)
- [ ] 2.2 Local evidence store and disk policy (architect)
- [ ] 2.3 Service host and IPC (builder)
- [ ] 2.4 Worker login and sessions (architect)
- [ ] 2.5 Consent and capture gate (architect)
- [ ] 2.6 Scan flow (builder)
- [ ] 2.7 String catalog and i18n (helper)
- [ ] 2.8 Scan screen UI (builder)
- [ ] 2.9 Sync client (builder)
- [ ] 2.10 "My record" screen and disputes (builder)
- [ ] 2.11 Fail-open test suite (builder)
- [ ] 2.R Phase review (architect)

## Phase 3: Server and admin
- [ ] 3.1 Server skeleton and Compose (builder)
- [ ] 3.2 Models and initial migration (architect)
- [ ] 3.3 Audit log service (architect)
- [ ] 3.4 Console authentication (architect)
- [ ] 3.5 RBAC and scoping (architect)
- [ ] 3.6 Device enrollment and tokens (architect)
- [ ] 3.7 Ingest API and evidence store (architect)
- [ ] 3.8 Config bundle and heartbeat (builder)
- [ ] 3.9 Console base, layout and i18n (helper)
- [ ] 3.10 Admin: sites, lines, workstations, cameras (builder)
- [ ] 3.11 Admin: users, roles, profiles, shifts (builder)
- [ ] 3.12 Consent registry (architect)
- [ ] 3.13 Audit viewer and system health pages (builder)
- [ ] 3.14 Workstation-server integration tests (builder)
- [ ] 3.R Phase review (architect)

## Phase 4: Supervisor dashboard
- [ ] 4.1 Live status service (builder)
- [ ] 4.2 Live wall UI (builder)
- [ ] 4.3 Alert engine and channels (builder)
- [ ] 4.4 Alert inbox UI (builder)
- [ ] 4.5 Evidence access service (architect)
- [ ] 4.6 Event review UI and disputes (builder)
- [ ] 4.7 Live view (architect)
- [ ] 4.8 Supervisor worker assignment (builder)
- [ ] 4.R Phase review (architect)

## Phase 5: Reports and retention
- [ ] 5.1 Report queries (builder)
- [ ] 5.2 Report screens (builder)
- [ ] 5.3 Excel and PDF export (builder)
- [ ] 5.4 Scheduled email reports (builder)
- [ ] 5.5 Retention purge jobs (architect)
- [ ] 5.6 Evidence deletion with dual approval (architect)
- [ ] 5.7 Health monitoring (builder)
- [ ] 5.8 Backup and restore (builder)
- [ ] 5.R Phase review (architect)

## Phase 6: Pilot readiness
- [ ] 6.1 Signal-trace recording in production (architect)
- [ ] 6.2 Threshold tuning tool (architect)
- [ ] 6.3 Remote config push and rollback (builder)
- [ ] 6.4 Load and soak tests (builder)
- [ ] 6.5 Offline queue chaos tests (builder)
- [ ] 6.6 Security hardening pass (architect)
- [ ] 6.7 Pilot runbook and checklists (helper)
- [ ] 6.8 Locale readiness (helper)
- [ ] 6.R Phase review (architect)

## Phase 7: Rollout
- [ ] 7.1 Workstation packaging and silent installer (builder)
- [ ] 7.2 Auto-update with rollback (architect)
- [ ] 7.3 Server deployment package (builder)
- [ ] 7.4 Kiosk and service hardening (builder)
- [ ] 7.5 Manuals (helper)
- [ ] 7.6 Release checklist and versioning (helper)
- [ ] 7.R Phase review (architect)

## Deviations and notes

Record here, with the task number and date: anything built against a mock or
interface, anything untested, decisions taken without the owner, and manual
checks the owner must do.

| Date | Task | Note |
|---|---|---|
| 2026-11-15 | Setup | Design docs, plan and agents created. No application code. Owner decisions D1-D11 and assumptions A1-A13 are in `risks-open-questions.md`. |
| 2026-11-15 | 1.1 | weasyprint installs but fails to import on this machine: it needs the GTK/Pango runtime DLLs (libgobject-2.0-0.dll) and the loader looks in `C:\Program Files\Tesseract-OCR`. PDF export (server, later phase) needs the GTK3 runtime installed and on PATH; not needed until reports work. Also: mediapipe pulls in opencv-contrib-python next to opencv-python (both installed, no conflict seen). Coverage `fail_under` left out: the tree is empty (100% trivially) and the 80% target applies to business logic only, so a global gate would mislead. |
| 2026-11-15 | 1.2 | **Sunglasses profile has yawn detection off.** `detection-engine.md` 3 lists yawn "on" for Sunglasses, but `scope-v1.md` 4.3 says "Head pose, posture and motion only". The scope was followed; drowsiness for that profile then rests on head nods. Owner may reverse this (one table entry in `ag_shared/ppe.py`). |
| 2026-11-15 | 1.2 | Decisions taken where the design docs were silent. (a) `scan_window_s` is listed both in `Thresholds` and in `EngineConfig`; it is stored once, in `Thresholds`, and `EngineConfig.scan_window_s` reads it. (b) `PpeProfile` has a `glare_tolerance` flag (true only for Clear glasses) and derives its sleeping rule from the enabled signals, which reproduces the table for the built-ins. (c) Presence and quality signals cannot be disabled, a derived signal needs its source signal, and only `custom` may change which signals are on; other kinds may only tune weights. (d) The built-in `custom` profile is a template that starts with the Open face signal set. (e) `CaptureConfig` adds `ring_seconds` (3 s) and `episode_keyframe_states` (Sleeping, Absent) with defaults, since the bundle's `capture` object does not carry them; a combination where the ring cannot hold the pre-frames is refused. (f) `shift_thresholds` entries are fully resolved (station layer already applied), because station overrides rank above shift overrides. (g) ROI points are `{x, y}` objects in 0-1; `days_of_week` uses 0 = Monday and names the day a shift starts; breaks must lie inside their shift. (h) `EngineConfig` also carries `timezone` (not yet checked against the IANA database; task 1.7 uses it) and `config_version`. (i) `gap_tolerance_s` must be shorter than every timer. |
| 2026-11-15 | 1.2 | Open point for tasks 2.4 / 3.8: the bundle carries resolved thresholds only, so when a worker's PPE override replaces the station profile and that profile has its own threshold overrides, the workstation cannot re-resolve them. Either the server resolves thresholds per roster profile or the bundle carries the layers. Needs an API-contract decision from the owner before then. The 40% scan-window share, the 1.5 s yawn duration and the 30 s degraded-camera limit are not in the `Thresholds` table and are left to the tasks that use them (1.5-1.8). |
| 2026-11-16 | 1.3 | Built and tested without hardware. `OpenCvSource` (USB, built-in, RTSP) is tested only against a mocked `cv2.VideoCapture`; `FileSource` against a tiny MJPG clip generated in a tmp dir. **Owner manual check:** a real USB webcam, the laptop built-in camera and an RTSP camera (unplug / power-cycle each and confirm it shows `offline` and comes back). ONVIF discovery is a stub (`StubOnvifResolver`): an `onvif` camera whose URI is already `rtsp://` works, any other ONVIF address stays `offline` until a real resolver (WS-Discovery + GetStreamUri) is written. Decisions: USB/built-in URIs are `"N"`, `"usb:N"`, `"builtin:N"` or `"builtin"`; RTSP uses the FFmpeg backend with 5 s open/read timeouts; a blocking open/read longer than `stale_after_s` (5 s) reports `offline` (`Hung`) and the supervisor abandons that thread and starts a fresh one (at most 2 abandoned threads per camera, since a Python thread cannot be killed); a camera with no frame for 5 s reports `offline` (`Stale`). `CameraStatus` (`starting`/`online`/`offline`/`stopped`) lives in `ag_workstation.capture`; the heartbeat task may move it to `ag_shared` if the payload model needs it. Hot reconfiguration of cameras (new `EngineConfig`) is left to 1.8: rebuild the `CaptureManager`. |
| 2026-11-20 | Decision | **Owner decision (2026-11-20), closes the 1.2 open point:** the server resolves thresholds per PPE profile. The config bundle gains an additive field with fully resolved thresholds for every PPE profile used by a roster override, so the workstation picks the right set on login without re-resolving layers. Implemented in 2.4 (client) and 3.8 (server); `api-contract.md` 4.2 gets the field when 3.8 is built. |
| 2026-11-20 | 1.4 | Signal extractors run on synthetic landmarks only. The real `MediaPipeLandmarker` path needs `face_landmarker.task` (and optionally `pose_landmarker_lite.task`) in `AG_MODEL_DIR`; the one test that loads them skips without it. **Owner manual check:** download both models from the MediaPipe model page, set `AG_MODEL_DIR`, run `pytest tests/workstation/test_landmark_provider.py`. Lint and type errors left in the first draft were fixed before ticking. |
| 2026-11-20 | 1.5 | Decisions where the design was silent (constants in `EstimatorParams`, to tune in the pilot): (a) "repeated head nods" = 3 nods in 60 s; (b) still = motion at or below 0.05 body scales/s; (c) when the face is unreadable, posture slump of 0.7 or more counts as head dropped; (d) eye fallback triggers when under 50% of the last 2 s of eye frames were measurable, Sleeping then comes from head drop (plus stillness when motion is on) after `sleeping_head_drop_s`, confidence x0.6; profiles without eye signals get x0.8 on Sleeping; (e) a visible upper body (posture-enabled profiles) is a person, so it is not Absent; (f) every "for N s" timer is strict (> N), matching the 4.0 vs 4.1 s rule; (g) while a fault has not yet met its timer (bad quality < 2 s, no face < 10 s) the previous candidate is held instead of reporting Attentive. Confidence = frame quality x the weight of the deciding signal. |
| 2026-11-20 | 1.6 | Fusion decisions: with 3-4 cameras and the primary unusable, rule 2 takes the single best-quality usable secondary (ties go to the lower slot); with the primary usable, all usable cameras are compared under rules 3-4. Rule 5 applies when a usable camera reports Absent while another usable camera sees a good-quality face; it does not by itself set `low_confidence`. A camera the capture layer reports offline is ignored even if it still has a recent candidate. Rule 7 is a separate `DegradedTracker` (30 s) so `fuse` stays a pure function. |
| 2026-11-20 | 1.7 | Decisions: (a) recovery needs `recovery_seconds` of uninterrupted less-severe candidates (any relapse restarts it, otherwise a brief Attentive spell would close an episode and reopen it); (b) Unknown is entered and left immediately (its timer already ran in the estimator) and closes any open episode; (c) scan rule 2 reads the fused, pre-hysteresis states of the last `scan_window_s` and only warned states count, while On Break short-circuits to "unmonitored" even if the previous seconds were warned; (d) buckets credit at most 2 s between two updates to the held state, the rest of a longer gap counts as Unknown; (e) shift/break times are matched in the site timezone, a break after midnight belongs to the shift that started the day before. `EngineConfig.timezone` is now validated against the IANA database, and `tzdata` was added to the dependencies because Windows has no system zone database. New shared helper: `ag_shared.ids.uuid7` (Python 3.11 has no `uuid.uuid7`). Bucket attribution to a worker (`user_id`) and splitting a minute at a session change are left to 2.4 / 2.6. During 1.7 the estimator and fusion gained `quality_ok`: a camera holding its state through a short bad-quality burst stays usable until `unknown_seconds`, but a camera with a good frame is preferred. |
| 2026-11-20 | 1.8 | The engine is split into `StationPipeline` (single-threaded, deterministic; reused by the replay harness) and `Engine` (one processing thread per camera, a tick thread every 100 ms, events through `drain_events`). The scan query waits at most 100 ms for the engine lock and otherwise answers Unknown/unmonitored; measured at well under 1 ms per query in CI. Isolation: a failing landmark model or a provider that cannot load leaves that camera stale (Unknown) and the others running; a dead camera thread is restarted by the tick loop; tick, processing and listener errors are counted, never raised. `apply_config` validates (including the timezone) before anything changes; only a change to the cameras restarts capture. Bug found and fixed while testing: `stop()` could wait the full 5 s join timeout because the supervisor tried to take the lifecycle lock held by `stop()`. **Owner manual check:** frame rate, CPU load and state latency with two real cameras and MediaPipe on the target i5 workstation (not measurable here). Process-level restart within 5 s is task 2.3. |
| 2026-11-20 | 1.9 | The ring samples frames by frame timestamp every `frame_interval_ms` (500 ms) and holds `ring_frames` per camera (6); JPEG encoding is injected, the real encoder (`capture.jpeg`, OpenCV) stays in the capture layer. Burst rules: the key frame is the frame nearest the scan time within 1.5 sample intervals; pre frames are the ones before the key frame, post frames the next primary frames after it; a burst waits for its post frames until (post_frames + 1) intervals after the key, then is delivered with the gaps listed in `missing`; at most 32 bursts wait at once. Wiring: `Engine(frame_listener=rings.offer)`, and the scan flow (2.6) calls `request_scan_burst` / `request_keyframe` and `poll` on a timer; the consent gate (2.5) sits behind `EvidenceSink`. |
| 2026-11-20 | 1.10 | Harness in `ag_workstation.harness` (`python -m ag_workstation.harness run DIR`). Decisions: (a) labels may also point at a signal trace (`*.trace.jsonl`), so the whole harness runs in CI on synthetic traces; (b) recordings sharing a `session` are replayed together as one station; the primary recording carries the ground truth; (c) a labelled episode counts as detected when a predicted episode of the same state overlaps it with 2 s tolerance on both sides; latency = predicted start minus labelled start; (d) a false alarm is a predicted warned episode that touches no labelled non-attentive time, scaled to 8 h of attentive footage; (e) the baseline is `docs/accuracy-baseline.md` plus `docs/accuracy-baseline.json`; a run regresses when any recall or precision drops more than 2 points or false alarms per shift rise more than 0.5; (f) labels must not carry names: unknown keys are rejected and tags are coarse groups. The pipeline gained a `signal_listener` hook (trace recording). **Not run here:** `pytest -m video` needs `AG_TEST_VIDEOS` and `AG_MODEL_DIR`; the video baseline stays empty until the labelled set exists (Q1). |
| 2026-11-20 | 1.6 | Fix (found by the 1.11 paired-camera fixture): when a worker came back into view, the estimator could still report Absent for up to `gap_tolerance_s` on a frame that showed the face, and fusion rule 5 then picked that Absent as the more severe state, giving a false Absent episode. The estimator now never reports Absent on a frame with a person, and rule 5 ignores face cameras whose own candidate still says Absent. |
| 2026-11-20 | 1.11 | Fixtures: 10 synthetic recordings in `tests/fixtures/traces/` (every PPE profile, every warned state, a glare fallback, near-miss blinks and glances, a paired two-camera session, 5 minutes of attentive work), regenerated by `python -m ag_workstation.harness.fixtures`; tests check the committed files and `docs/accuracy-baseline.{md,json}` match the code. `.gitattributes` keeps them LF on Windows. The committed baseline is synthetic (source `trace`) and proves only that the logic works; the video test ignores it and asks for a real baseline. Known behaviour for pilot tuning, visible in the baseline (distracted precision 25%): a head that drops for sleep is "looking down", so profiles that rely on head drop show Distracted for ~3 s before Sleeping. Also: three yawns keep the station Drowsy until they leave the 5-minute yawn window, as the threshold table defines. Sample video labels are in `tests/fixtures/labels/`; the guide is `docs/recording-and-labelling.md`. |
| 2026-11-20 | 1.R | Independent review of the phase diff against `detection-engine.md`. The architect agent is configured for Fable, which was not available on this account (usage credits), so the review ran on Opus as a separate agent that had not seen the work. It found 10 issues; all are fixed with tests that fail on the old code: (1, critical) timers treated dropped frames as interruptions, so below ~2 fps or with 0.5 s+ processing stalls nothing ever left Attentive, including a dark camera (fail-open broken): only a contrary or unreadable frame now ends a run, and health flags cameras processing under 2 fps; (2, high) the scan query could answer from a stopped or stalled engine: it now answers Unknown when stopped or when the last tick is older than `unknown_seconds`, and stop() publishes Unknown; (3) an empty station read Attentive for 10 s at start or after a new session: now Unknown until Absent; (4) label tags accepted names and ids: tags are now a fixed vocabulary; (5) a failing evidence sink dropped other bursts and broke the camera thread: errors are contained per burst, ring listeners are isolated, and `AsyncSink` delivers on its own thread through a bounded queue; (6) `open_episode`/`latest_candidates` now use the bounded wait; (7) the calendar compares in UTC, so a break in a repeated DST hour applies once; (8) the UUIDv7 counter is read under the lock; (9) a camera thread stuck in the model for 10 s is abandoned (at most 2 per camera) and replaced with a fresh provider; (10) replays tick every 0.1 s of frame time like the live engine. The fixture baseline was regenerated (two confusion-matrix cells moved). Changed test: `test_timer_restarts_when_true_returns_after_a_silent_gap` asserted the buggy behaviour of finding 1 and was replaced by `test_missing_frames_are_not_an_interruption`. |
