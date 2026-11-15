# Risks and open questions

## Decisions confirmed by the owner (2026-11-15)

These change or sharpen `scope-v1.md`. Where they differ, these win.

| # | Decision |
|---|---|
| D1 | Fail-open: scanning never stops on camera, engine or server failure. |
| D2 | 2 cameras per station by default (1-4 configurable). Pilot: 2 stations. |
| D3 | Supervisor live view is status and still images by default. Live video is a per-site setting, off by default. Every view is audit-logged. |
| D4 | Evidence deletion needs approval from a second Admin. |
| D5 | UI in English; all strings externalized for Hindi and Telugu later. |
| D6 | Alerts by dashboard and email in release 1; a channel interface allows SMS/WhatsApp later. |
| D7 | **Workers have no badges.** Login is employee ID + PIN typed at the station (keypad or touchscreen). A supervisor can assign a worker to a station from the dashboard at shift start as a fallback. The login interface is pluggable so face match can be added later as an optional, consent-gated method. No face templates in release 1. |
| D8 | Workers reach their own data and the dispute process in the workstation app only. No worker web login. |
| D9 | Console sign-in: local accounts with TOTP MFA (mandatory for Admins). LDAP/AD/SSO deferred behind an interface. |
| D10 | Phases 6 and 7 cover the software parts only (pilot readiness; installer, update, docs). Field work is listed as manual checklists. |
| D11 | Stack: all Python 3.11+. PySide6, OpenCV, MediaPipe; FastAPI, PostgreSQL, SQLAlchemy, Alembic, Docker Compose; Jinja2 + HTMX console. This replaces the scope's React and WebRTC suggestions. |

## Design assumptions made without asking

These follow from the scope but were my choice. Tell me if any is wrong.

| # | Assumption | Where |
|---|---|---|
| A1 | A scan with no worker logged in is still accepted and logged as unattributed; the screen then asks for login. | scan flow |
| A2 | Warned states are Distracted, Drowsy, Sleeping and Absent. Unknown and On Break never warn the worker. | detection-engine.md 5 |
| A3 | Scan state = current non-attentive state, else the non-attentive state holding at least 40% of the last 5 s. | detection-engine.md 5 |
| A4 | A good-quality face on any camera rules out Absent, even if another camera sees no face. Refines the "most conservative" fusion rule. | detection-engine.md 6 |
| A5 | Evidence burst: key frame + 2 pre + 2 post frames from the primary, key frame from secondaries. Configurable. | detection-engine.md 7 |
| A6 | No images are captured when nobody is logged in, because consent cannot be established. | security-privacy.md 1 |
| A7 | Evidence store is an encrypted filesystem volume behind an interface; MinIO/S3 can be added later. | security-privacy.md 4 |
| A8 | The dashboard updates by HTMX polling every 2 s; workstations heartbeat every 2 s. Dashboard lag can reach about 4 s. The under-1-s latency target applies on the workstation. | api-contract.md 4.1 |
| A9 | `blocking_mode` and `fail_open = false` exist as settings but release 1 refuses to enable them. | schema.md site settings |
| A10 | Server runs on Linux in Docker Compose (on a Windows Server host this means a Linux VM or WSL2). | deploy |
| A11 | Attention score excludes Unknown and On Break time. | schema.md |
| A12 | Worker-facing alert is a sound plus banner; no spoken voice alerts in release 1. | scope 11.6 |
| A13 | Email goes through an SMTP relay that the site provides. | alerts |

## Open questions for the owner

| # | Question | Default if unanswered |
|---|---|---|
| Q1 | Does a labeled video set exist, or must it be recorded? Who labels it? Phase 1 accuracy numbers depend on it. | Build the harness; baseline stays empty until videos arrive | Test videos: [none yet / we will record ~__ hours]. Labeled by: [name/team].
| Q2 | Which cameras will the pilot use (USB or IP, IR or not)? | Develop against USB webcams and video files | Pilot cameras: [USB / IP-RTSP], [IR: yes / no], model: [if known].
| Q3 | Are workstations touchscreens, or is a keypad attached for ID + PIN entry? | Support both; on-screen keypad plus keyboard input | ID + PIN entry: [touchscreen / keypad / keyboard].
| Q4 | What are the employee ID format and PIN length? Who issues and resets PINs? | Alphanumeric ID up to 16 characters; 4-6 digit PIN; Admin resets |Employee ID format: [e.g. 6 digits]. PIN: [4 / 6] digits. PINs issued
    and reset by: [Admin / HR / supervisor].
| Q5 | Can a worker without consent work at a monitored station at all? | Yes; metadata only, as the scope says | accept the defaults.
| Q6 | On consent withdrawal, are that worker's existing images deleted immediately, or kept until normal retention? | Removed at the next purge run |accept the defaults.
| Q7 | Should events under dispute or pending deletion be held past retention? | Yes, until resolved, with a 90-day cap |accept the defaults.
| Q8 | Who receives each alert type by email (supervisors of the line, all Admins)? | Line supervisors for worker alerts; Admins for system alerts |accept the defaults.
| Q9 | Shift patterns: fixed per site, per line, or rotating rosters per worker? | Per site with optional per-line override; no per-worker rosters |accept the defaults.
| Q10 | Is the scanner a keyboard wedge at every station? | Yes; serial/HID left as an interface |accept the defaults.
| Q11 | Does the same person ever hold Admin at more than one site? | Supported (`site_id` null = all sites) |accept the defaults.
| Q12 | Server hosting for the pilot: which machine and OS, and is there an internal CA for TLS? | Linux host, self-signed internal CA documented in the runbook |Pilot server: [machine, Windows or Linux]. TLS: [internal CA exists /
    use self-signed].
| Q13 | Is HR/legal sign-off on the consent policy and privacy notice in progress? It gates the pilot. | Tracked as a go-live blocker | HR/legal sign-off: [in progress / not started]. Keep it as a go-live blocker.
Also: each site will have at least 2 Admins (R11).
All assumptions A1-A13 accepted.

## Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | No labeled test videos | Accuracy targets (95% sleeping recall, under 2 false alarms per shift) cannot be shown | Harness and synthetic traces first; record and label during phase 0/pilot; signal-trace recording for tuning |
| R2 | Detection accuracy differs across skin tones, ages, glasses, lighting | Unfair false positives; legal and trust damage | Fairness slices in the harness; IR camera recommendation; review-before-action policy; pilot tuning |
| R3 | MediaPipe wheel availability and CPU cost on Windows; two cameras at 10-15 FPS on an i5 | Low frame rate, slow state updates | Pin Python 3.11/3.12 and tested versions; latest-frame-wins; measure FPS in phase 1 on target hardware; reduce resolution or stagger cameras |
| R4 | PIN is a weak identifier; workers may share IDs | Events attributed to the wrong person | Lockout, supervisor assignment, review step, audit; face match as a later option |
| R5 | Sunglasses and helmet profiles lack eye signals | Sleeping missed or over-reported | Posture and stillness rules; lower confidence; documented limits; pilot data |
| R6 | DPAPI keys are tied to the machine or account | Unsent queue unreadable after OS reinstall or disk move | Short local retention; upload promptly; documented in the runbook |
| R7 | Clock drift on offline workstations | Untrustworthy evidence times | Server-time offset, drift alert, Windows time sync in the runbook |
| R8 | Legal basis and consent not signed off | Pilot cannot start lawfully | Go-live blocker; consent module enforced in code |
| R9 | HTMX console without a JS build step must still do ROI drawing and live wall | UI effort and limits | Small vendored vanilla JS for the canvas only; polling for live data |
| R10 | Service and kiosk hardening on Windows IoT differs by image | Monitoring can be stopped locally | Service account, ACLs, watchdog; verify on the real image in the pilot |
| R11 | Single Admin at a site | Evidence can never be deleted manually | By design; documented; retention still purges |
| R12 | Evidence volume growth (100+ stations) | Disk exhaustion on the server | Retention purge, size caps on images, disk alerts, capacity note in the runbook |
| R13 | Roster with PIN hashes is cached on every workstation | Offline guessing of 4-6 digit PINs if a disk is stolen | Encrypted at rest, argon2id, device revocation; PIN grants only station login |
| R14 | Key loss (server KEK) | All evidence unreadable | Key backup procedure separate from data backups; restore drill in phase 5 |

## What only real hardware can confirm

Frame rate and CPU load per camera; IR and low-light behavior; glare with
glasses; RTSP/ONVIF camera compatibility; Windows service recovery and kiosk
lock-down; scanner behavior; touchscreen keypad usability; 8-hour soak;
installer and auto-update on the real image.
