# Security and privacy design (v1)

Applies the scope's section 5 (RBAC) and section 7 (security, privacy,
compliance). Code in these areas is owned by the architect agent.

## 1. Privacy rules that the code must enforce

| Rule | Enforcement |
|---|---|
| Images only on events; no continuous recording | Frames live in a memory ring; disk writes happen only in the evidence capture path |
| No consent means no images | Three gates: capture on the workstation, ingest on the server (`403 consent_missing`), and serving (images of a worker without current consent are not shown) |
| No worker session means no images | Capture gate; event records `images_withheld = no_worker` |
| Live view is not stored | Stills held in memory for 30 s; video relayed, never written |
| Every evidence view and live view is logged | The only code path that decrypts an image or opens a live view writes the audit entry first, in the same transaction or call |
| Detections are review input, not discipline | Events start as `pending`; reports label unreviewed counts; no automated action on a worker exists |
| Workers can see their own data and dispute it | "My record" screen on the workstation |
| Retention is enforced | Scheduled purge with a purge log |
| No biometric identification | No face templates or embeddings in release 1 |

Consent withdrawal: new images stop at the next config pull. Existing images
of that worker are removed by the next purge run with reason
`consent_withdrawal` (see open question Q6).

## 2. Roles and server-side RBAC

Permissions are string keys; each route declares exactly one requirement
through a FastAPI dependency. There is no route without a declared permission
or an explicit `public` marker, and a test walks the route table to prove it.
Default is deny.

| Permission | Admin | Supervisor | Workstation user |
|---|---|---|---|
| `config.manage` (cameras, stations, thresholds, profiles, shifts) | yes | no | no |
| `users.manage` | yes | no | no |
| `consent.manage` | yes | no | no |
| `live.view` | yes | assigned lines | no |
| `reports.view` | yes | assigned lines | own only, on the workstation |
| `events.review` | yes | assigned lines | no |
| `evidence.view` | yes | assigned lines | no |
| `evidence.delete.request` / `evidence.delete.approve` | yes (two different Admins) | no | no |
| `alerts.manage` | yes | assigned lines | no |
| `workers.assign` (assign a worker to a station) | yes | assigned lines | no |
| `audit.view` | yes | no | no |
| `system.manage` (health, backup, releases) | yes | no | no |
| `scan` | yes | no | yes |

Scoping: every query goes through a scope filter built from the session:
site (`user.site_id`, null means all sites for Admins) and, for Supervisors,
the set of assigned lines. Object lookups by id apply the same filter, so an
out-of-scope id returns 404. UI hiding is cosmetic; the server check is the
control.

Workstation users have no console login. Their "own only" data is served from
the workstation's local store and from the device API, limited to the worker
of the active session.

## 3. Authentication

**Console (Admin, Supervisor).**

- Local accounts. Passwords hashed with argon2id. Policy: at least 12
  characters, checked against a small common-password list, no reuse of the
  last 5, lockout for 15 minutes after 5 failures.
- TOTP MFA is mandatory for Admins and optional for Supervisors. The secret is
  encrypted at rest. Recovery codes are single-use and hashed.
- Server-side sessions. Cookie: `HttpOnly`, `Secure`, `SameSite=Strict`. Idle
  timeout 15 minutes, absolute 8 hours, both configurable per site. Session id
  rotates at login and MFA.
- CSRF token on every state-changing request (HTMX sends it as a header).
- An `AuthProvider` interface isolates credential checks so LDAP/AD/SSO can be
  added in release 2 without touching sessions or RBAC.

**Workers (workstation).**

- Employee ID + PIN typed on a keypad or touchscreen. PIN is 4-6 digits,
  hashed with argon2id, verified offline against the cached roster.
- 5 failures lock that worker on that station for 5 minutes. Failures and
  lockouts are queued as events and audit-logged on the server.
- Fallback: a Supervisor assigns a worker to a station from the dashboard
  (`workers.assign`), delivered as a heartbeat command and audit-logged.
- `LoginMethod` is a plug-in interface. Face match can be added later as an
  optional method with its own consent; nothing for it is built now.
- A PIN is a weak secret. It identifies who is at the station; it grants no
  access to other people's data.

**Devices.** See `api-contract.md` section 3: one-time enrollment token,
device secret in DPAPI, short-lived JWT, rotation and revocation.

## 4. Encryption

| Data | Protection |
|---|---|
| In transit | TLS 1.2+ terminated at the reverse proxy; HSTS; internal CA or public certificate; workstation can pin the CA |
| Evidence images (server) | AES-256-GCM, one data key per image, wrapped by a key-encryption key (KEK). `kek_id` stored per image to allow rotation. Files are opaque blobs in a volume only the app can read |
| Evidence images and queue (workstation) | AES-256-GCM; data key protected by Windows DPAPI; directory ACL limited to the service account |
| Database | Encrypted volume (LUKS or BitLocker) is required; sensitive columns (`totp_secret`, camera URIs, consent artifacts) are also encrypted in the application |
| Backups | Encrypted with a separate key before leaving the server |
| Secrets | KEK, JWT signing key, DB password and SMTP password come from Docker secrets or environment files outside git. The app refuses to start in production mode with default or missing secrets |

The evidence store sits behind an `EvidenceStore` interface (filesystem in
release 1; S3/MinIO-compatible later).

## 5. Audit logging

Append-only `audit_log` with a hash chain (see `schema.md`). Logged actions:

- Sign-in, sign-out, failures, lockouts, MFA changes, password and PIN resets.
- Every create, update and delete of configuration, users, roles, line
  assignments, shifts, profiles and site settings (with before and after).
- Every evidence image view and export, every live still and live video
  session, with viewer, workstation, camera and time.
- Event review decisions and dispute resolutions.
- Evidence deletion requests, approvals, rejections and executions.
- Consent grants and withdrawals.
- Worker assignments by supervisors; device enrollment, rotation, revocation.
- Report exports and scheduled deliveries; retention purge runs; backup and
  restore.

Audit entries never contain image data, PINs, passwords or tokens.

## 6. Evidence deletion (dual approval)

1. Admin A creates a request listing events and a reason.
2. Admin B (a different user, Admin role at decision time, same site scope)
   approves or rejects. The database CHECK and the service both enforce
   `decided_by <> requested_by`.
3. On approval a job deletes the image files and key material, marks the
   `evidence_image` rows removed, and writes audit and purge log entries.
   Event metadata remains until metadata retention expires.
4. A site with a single Admin cannot delete evidence manually; retention purge
   still applies. This is intentional.

## 7. Retention

Daily job per site: images older than `retention_image_days` (30), metadata
older than `retention_metadata_days` (365), audit rows older than
`retention_audit_days` (at least 365). Each run writes a `purge_log` row. The
job is idempotent and resumable; a failed run raises a `server_health` alert.
Events with an open dispute or a pending deletion request are held until
resolved (see open question Q7). The workstation purges its local copies after
upload and by local age and disk quota.

## 8. Application security

- Input validation with pydantic on every endpoint; SQLAlchemy only, no string
  SQL.
- Jinja2 autoescape on; HTMX responses are escaped fragments; a strict
  Content-Security-Policy with no inline scripts; all static assets are
  served locally (no CDN).
- Security headers at the proxy and app: HSTS, `X-Content-Type-Options`,
  `frame-ancestors 'none'`, `Referrer-Policy`.
- Rate limiting on login, token and enrollment endpoints.
- Evidence images are served only through the authenticated, audit-logging
  endpoint with `Cache-Control: no-store`; there are no direct file URLs.
- Logs contain ids, never PINs, tokens, image bytes or camera credentials.
- Dependencies are pinned with hashes; `pip-audit` runs in the quality gate.
- Workstation: the service runs under a dedicated low-privilege account; the
  kiosk user cannot stop the service or read its data directory. Auto-update
  packages are signature-verified before install (phase 7).

## 9. Threats considered

| Threat | Mitigation |
|---|---|
| Stolen workstation PC | DPAPI-protected keys, BitLocker in the runbook, device revocation, short local retention |
| Rogue device posting fake events | Enrollment token, per-device secret, device can write only its own data |
| Supervisor browsing other lines | Server-side line scope on every query |
| Admin misuse of images | Every view is logged; audit log is append-only and hash-chained; deletion needs two Admins |
| Worker closes the app | Monitoring runs in a Windows service with auto-restart |
| Tampered evidence | SHA-256 recorded at capture, checked at ingest and on view |
| Replay or duplication of uploads | Idempotent ingest by client UUID and batch id |
| Network interception | TLS, optional CA pinning |
| Roster theft from a workstation | Encrypted at rest; argon2id PIN hashes; PIN grants only station login |

## 10. Compliance notes (DPDP Act 2023)

The software provides consent capture, notice text, purpose limitation,
retention, access logging and worker access to their own data. HR/legal
sign-off on the consent policy and privacy notice is a go-live condition that
software cannot satisfy; it is tracked in `risks-open-questions.md`.
