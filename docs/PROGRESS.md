# AttentionGuard progress

Tick a task only when its acceptance criteria in `PLAN.md` are met, the
quality gate passes and the work is committed.

## Phase 1: Detection prototype
- [ ] 1.1 Tooling and package skeletons (helper)
- [ ] 1.2 Shared domain and config models (architect)
- [ ] 1.3 Camera capture layer (builder)
- [ ] 1.4 Landmarks and signal extraction (architect)
- [ ] 1.5 Per-camera state estimator with PPE profiles (architect)
- [ ] 1.6 Multi-camera fusion (architect)
- [ ] 1.7 Station state machine (architect)
- [ ] 1.8 Engine runtime (architect)
- [ ] 1.9 Evidence ring buffer and burst capture (builder)
- [ ] 1.10 Replay harness and metrics (architect)
- [ ] 1.11 Baseline document and fixtures (helper)
- [ ] 1.R Phase review (architect)

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
