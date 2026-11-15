# AttentionGuard

Worker-alertness monitoring for barcode scanning workstations, in warn-and-log
mode. Source of truth for scope: `docs/scope-v1.md`. Owner decisions that
override it: `docs/risks-open-questions.md` (D1-D11).

## Non-negotiable rules

1. **A scan is always accepted.** Record the scan first; nothing (engine,
   camera, server, disk, exception) may block, delay or reject it.
   `blocking_mode` exists as a setting but release 1 refuses to enable it.
2. **Fail-open.** Camera, engine or server failure never stops scanning. The
   state becomes Unknown, an alert is raised, work continues.
3. **Warn and log only.** Not attentive at scan time: warning banner and
   sound, evidence saved, event flagged for review. No automatic discipline;
   detections are input to human review.
4. **The workstation works fully offline.** Login, scanning, detection and
   evidence capture need no network. Events go to an encrypted local queue.
5. **Consent gates images.** No current consent, or no logged-in worker: no
   images, metadata only. Enforced at capture, at server ingest and at serving.
6. **Data minimization.** Images only on events. No continuous recording. Live
   view is never stored. No face templates or identity features.
7. **Every evidence view and live view is audit-logged**, by the same code
   path that serves it. The audit log is append-only.
8. **Live view is status and stills by default.** Live video is a per-site
   setting, off by default.
9. **Evidence deletion needs a second, different Admin.**
10. **RBAC is enforced server-side on every endpoint**, default deny, with
    site and assigned-line scoping. Hiding UI is not access control.
11. **Retention is enforced** by purge jobs with a purge log (images 30 days,
    metadata 365, audit at least 365; configurable per site).
12. **Worker login is employee ID + PIN** (no badges), with supervisor
    assignment as fallback. The login method is a plug-in interface.
13. **All user-facing strings are externalized** (English now; Hindi and
    Telugu later).
14. **Never modify** `docs/scope-v1.md` or `docs/prompts.md`.
15. **Do not change** `docs/schema.md`, `docs/api-contract.md` or the rules
    above without asking the owner.

## Layout

```
server/        ag_server: FastAPI app, models, Alembic, Jinja2 + HTMX console, jobs
workstation/   ag_workstation: PySide6 app, Windows service, engine, queue, sync
shared/        ag_shared: enums, config and API payload models used by both sides
deploy/        Docker Compose, proxy config, installer and release scripts
docs/          scope, design docs, PLAN.md, PROGRESS.md
tests/         tests/server, tests/workstation, tests/shared, fixtures
```

Design docs: `docs/schema.md`, `docs/api-contract.md`,
`docs/detection-engine.md`, `docs/security-privacy.md`,
`docs/risks-open-questions.md`. Read the relevant one before changing code in
its area.

## Stack

Python 3.11+ everywhere (use 3.11 or 3.12 on workstations for MediaPipe).

- Workstation: PySide6, Windows service (pywin32), OpenCV, MediaPipe, SQLite
  queue with AES-GCM payloads, DPAPI-protected keys.
- Server: FastAPI, PostgreSQL, SQLAlchemy 2.x, Alembic, Docker Compose.
- Console: FastAPI + Jinja2 + HTMX. No JS build step, no CDN assets.

## Commands

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[server,workstation,dev]"

.venv\Scripts\pytest                         # all tests with coverage
.venv\Scripts\pytest tests/workstation -k fusion
.venv\Scripts\pytest -m video                # needs AG_TEST_VIDEOS
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .          # use `ruff format .` to fix
.venv\Scripts\mypy

docker compose -f deploy/docker-compose.yml up -d        # server stack (phase 3+)
.venv\Scripts\alembic -c server/alembic.ini upgrade head
```

Quality gate before every commit: `pytest`, `ruff check .`,
`ruff format --check .`, `mypy` all pass.

## Coding standards

- Full type hints; `mypy --strict` clean. No `Any` without a comment saying
  why. No `# type: ignore` without an error code and reason.
- Tests first. At least 80% coverage on business logic (engine logic, scan
  flow, queue, sync, auth, RBAC, ingest, retention, alerts). Never weaken or
  delete a test to make it pass.
- Keep hardware and frameworks at the edges. OpenCV and MediaPipe are imported
  only in the capture and landmark layers; PySide6 only in views. Logic takes
  interfaces and an injected clock so it runs in CI without cameras.
- Anything needing hardware, videos or credentials is built behind an
  interface with a fake, and recorded in PROGRESS.md "Deviations and notes".
- Pydantic models in `shared/` define config and API payloads. Do not
  duplicate them.
- UTC everywhere; convert to site time only for display. Client-generated
  UUIDv7 ids for device-originated records.
- Server: every route declares a permission dependency or an explicit public
  marker. Every query passes through the scope filter. SQLAlchemy only, no
  string-built SQL. Schema changes only through Alembic migrations.
- Never log PINs, passwords, tokens, camera credentials or image bytes.
- Secrets come from the environment or Docker secrets, never from the repo.
- Errors on the workstation are caught at the boundary, logged and turned
  into Unknown state or a health flag. They never reach the scan path.
- Small modules, clear names, docstrings on public functions. Comments
  explain why, not what.
- One task per commit, message `phase.task: summary` (for example
  `1.6: multi-camera fusion`).

## Workflow

- `docs/PLAN.md` lists phases and tasks with acceptance criteria and owners.
  `docs/PROGRESS.md` tracks them. Build one phase per conversation.
- Per task: tests first, implement, run the quality gate, commit, tick
  PROGRESS.md.
- A phase ends with the architect reviewing its diff against the design docs.

## Model routing

Delegate each task to the agent named in PLAN.md. The coordinator does not do
architect-level work itself.

| Agent | Model | Owns |
|---|---|---|
| `architect` | fable | Detection engine, multi-camera fusion, schema and API contract changes, security-sensitive code (auth, RBAC, encryption, audit, consent), end-of-phase reviews |
| `builder` | opus | All other feature work: APIs, UI, client, reports, jobs, installers, and their tests |
| `helper` | sonnet | Boilerplate, fixtures, docs, renames, lint and type fixes |

If a builder or helper task turns out to touch architect territory, stop and
hand that part to the architect. Agents follow this file and return a short
summary, not file dumps.
