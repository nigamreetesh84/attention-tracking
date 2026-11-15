# AttentionGuard build prompts

This file holds three prompts. Execute only the one I name in the chat.
Never run more than one prompt in a single conversation.

| Prompt | When | Model | Mode |
|---|---|---|---|
| 1. Setup | Once, at the start | Fable 5.2 | Plan mode |
| 2. Build | Once per phase (7 times), new conversation each time | Opus 5.5 | Edit automatically |
| 3. Final review | Once, after all phases | Fable 5.2 | Edit automatically |

---

## Prompt 1: Setup

You are the lead architect for AttentionGuard. Read docs/scope-v1.md fully.
It is the source of truth for scope, behavior, roles, retention and privacy.

### Decisions (final unless I change them)
- Fail-open: scanning never stops on camera, engine or server failure.
- 2 cameras per station by default (1-4 configurable). Pilot: 2 stations.
- Supervisor live view: status and still images by default; live video is a
  per-site setting, off by default, and every view is audit-logged.
- Evidence deletion: requires approval from a second Admin.
- UI language: English, with all strings externalized for Hindi/Telugu later.
- Alerts: dashboard and email in release 1; channel interface for SMS/WhatsApp.

### Stack (all Python 3.11+)
- Workstation: PySide6 app + Windows service, OpenCV + MediaPipe, encrypted
  offline queue.
- Server: FastAPI, PostgreSQL, SQLAlchemy, Alembic, Docker Compose.
- Web console: FastAPI + Jinja2 + HTMX (no JS build step).
- Quality: full type hints, pytest (>=80% coverage on business logic),
  ruff, mypy.

### Your task now: design and project setup only. No application code.
1. Create the monorepo layout: /server /workstation /shared /deploy /docs /tests.
2. Write to /docs: database schema with ER summary; workstation-server API
   contract (auth, event payload, offline sync, idempotency, versioning);
   detection-engine design (signals, fusion, state machine, PPE profiles,
   config model, test method against labeled videos); security and privacy
   design; risks and open questions.
3. Write CLAUDE.md in the repo root (under 150 lines): the non-negotiable rules
   from the scope, coding standards, commands to run tests and linters, and the
   model routing rules below.
4. Write docs/PLAN.md: phases 1-7 from the scope's delivery plan, each split
   into tasks small enough for one sitting, each with acceptance criteria,
   required tests, and the agent that owns it.
5. Write docs/PROGRESS.md: a checklist of every task in PLAN.md, all unchecked,
   plus a "Deviations and notes" section.
6. Create three subagents in .claude/agents/:
   - architect.md (model: fable): detection engine, multi-camera fusion,
     schema or API contract changes, security-sensitive code (auth, RBAC,
     encryption, audit, consent), and reviewing each finished phase.
   - builder.md (model: opus): all other feature work: APIs, UI, client,
     reports, jobs, installers, and their tests.
   - helper.md (model: sonnet): boilerplate, fixtures, docs, renames, lint
     and type fixes.
   Give each a clear description so delegation is automatic, and tell each to
   follow CLAUDE.md and to return a short summary, not file dumps.
7. Do not modify docs/scope-v1.md or docs/prompts.md.

Ask me about anything important that the scope leaves unclear instead of
assuming. Then stop for my review.

---

## Prompt 2: Build the next phase

Read CLAUDE.md, docs/PLAN.md and docs/PROGRESS.md. Build the next unfinished
phase, and only that phase.

Rules:
- You coordinate. Delegate each task to the agent named in PLAN.md (architect,
  builder or helper). Do not do architect-level work yourself.
- Per task: write tests first, implement, run pytest, ruff and mypy, and fix
  failures before moving on. Never weaken or delete a test to make it pass.
- Commit after each task with a clear message, and tick it in PROGRESS.md.
- If a task needs something you don't have (camera hardware, test videos,
  credentials, a decision from me), build against a mock or interface, record
  it under "Deviations and notes", and continue.
- Do not change the schema, API contract or scope rules without asking me.
- When all tasks in the phase pass, have the architect agent review the
  phase's diff against the design docs, and fix what it finds.

Finish with a summary: what was built, test results, what is untested or
mocked, and what I must check by hand before the next phase.

---

## Prompt 3: Final review

Read CLAUDE.md, /docs and docs/PROGRESS.md. Do a release review of the whole
repository before the pilot. Do not add features.

Check: every rule in docs/scope-v1.md is implemented and tested (warn-and-log
never blocks a scan, fail-open, consent gating of images, retention purge,
dual approval for deletion, audit logging of every evidence and live view);
RBAC is enforced server-side on every endpoint; secrets, encryption and TLS
settings; offline queue behavior under loss and duplication; and everything
listed as mocked or untested.

Write docs/RELEASE_REVIEW.md with findings ranked by severity, fix the
critical and high ones with tests, and list what only a real workstation
test can confirm.
