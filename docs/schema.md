# Database schema (v1)

PostgreSQL 15+. SQLAlchemy 2.x models, Alembic migrations. This document is the
source of truth for tables and relationships; changing it needs the owner's
approval (see CLAUDE.md).

## Conventions

- Primary keys are UUIDs. Rows created on a workstation (`worker_session`,
  `scan_record`, `attention_event`, `evidence_image`, `dispute`) use the
  **client-generated UUIDv7** as the primary key, which is what makes ingest
  idempotent. `audit_log` and `purge_log` use `bigserial` to keep strict order.
- All timestamps are `timestamptz` in UTC. Device-originated rows carry both
  `occurred_at` (device clock, corrected by the server offset) and
  `received_at` (server clock).
- Every site-owned table has `site_id` (directly or through its parent) and all
  queries are filtered by the caller's site scope.
- Enums are PostgreSQL enums, named `<table>_<column>`.
- Soft delete is not used. Evidence is removed only by the retention purge or
  the dual-approval deletion flow, and both leave a log row.
- JSONB is used for settings and signal snapshots whose shape is validated by
  pydantic models in `/shared`.

## ER summary

```
site 1─* line 1─* workstation 1─* camera
site 1─* user *─1 role
user *─* line                      (supervisor_line: assigned lines)
site 1─* shift 1─* shift_break
site 1─* ppe_profile, threshold_profile, consent_policy
workstation *─1 ppe_profile, *─0..1 threshold_profile
user *─0..1 ppe_profile            (per-worker override)
user 1─* consent_record *─1 consent_policy
workstation 1─* worker_session *─1 user
worker_session 1─* scan_record
scan_record 0..1─0..1 attention_event   (scan warnings)
workstation 1─* attention_event 1─* evidence_image
attention_event 1─* dispute
workstation 1─* attention_bucket        (per-minute state seconds)
workstation 1─* device_health, alert, workstation_config, station_assignment
alert 1─* alert_delivery
evidence_deletion_request 1─* evidence_deletion_item *─1 attention_event
user 1─* web_session, live_view_session
site 1─* report_schedule 1─* report_run
audit_log, purge_log: append-only, reference other rows by type + id (no FK)
```

## Tables

### Organization

**site**: `id`, `name`, `timezone` (IANA), `default_threshold_profile_id`,
`settings` JSONB, `created_at`. Settings keys (validated by `SiteSettings`):

| Key | Default | Notes |
|---|---|---|
| `retention_image_days` | 30 | |
| `retention_metadata_days` | 365 | events, scans, buckets, health |
| `retention_audit_days` | 365 | minimum 365, enforced |
| `fail_open` | true | release 1 rejects `false` |
| `blocking_mode` | false | reserved; release 1 rejects `true` |
| `live_video_enabled` | false | stills are always available |
| `pin_required` | true | |
| `escalation_count` / `escalation_window_min` | 3 / 15 | repeated warnings |
| `deployment_mode` | `on_premise` | `on_premise`, `private_cloud`, `hybrid` |
| `default_language` | `en` | |

**line**: `id`, `site_id`, `name`, `is_active`. Unique (`site_id`, `name`).

**workstation**: `id`, `line_id`, `name`, `ppe_profile_id`,
`threshold_profile_id` (nullable), `camera_count` (1-4, default 2),
`device_secret_hash`, `enrollment_token_hash`, `enrollment_expires_at`,
`status` (`pending`, `active`, `revoked`), `enrolled_at`, `last_seen_at`,
`app_version`, `target_app_version`, `applied_config_version`,
`current_state`, `current_user_id`, `current_state_at` (denormalized live
status written by heartbeat).

**camera**: `id`, `workstation_id`, `slot` (1-4), `role` (`primary`,
`secondary`), `kind` (`usb`, `rtsp`, `onvif`, `builtin`), `uri_encrypted`
(may hold credentials), `roi` JSONB (polygon, normalized coordinates),
`calibration` JSONB (neutral head pose, exposure notes), `is_enabled`.
Unique (`workstation_id`, `slot`); exactly one `primary` per workstation
(partial unique index).

### People and access

**role**: `id`, `name` (`admin`, `supervisor`, `workstation_user`),
`permissions` JSONB (list of permission keys), `is_system`. The three system
roles are seeded and cannot be edited in release 1.

**user**: `id`, `site_id` (null = all sites, Admin only), `role_id`,
`employee_id`, `display_name`, `username` (console logins only), `email`,
`password_hash` (argon2id, null for workers), `pin_hash` (argon2id),
`totp_secret_encrypted`, `mfa_enabled`, `ppe_profile_override_id`,
`preferred_language`, `is_active`, `failed_attempts`, `locked_until`,
`password_changed_at`, `created_at`. Unique (`site_id`, `employee_id`);
unique `username`.

**supervisor_line**: `user_id`, `line_id`. Defines "assigned lines".

**web_session**: `id` (random 256-bit, stored hashed), `user_id`,
`created_at`, `last_seen_at`, `expires_at`, `mfa_passed`, `ip`, `user_agent`,
`revoked_at`.

**consent_policy**: `id`, `site_id`, `version`, `language`, `body`,
`effective_from`, `created_by`. Unique (`site_id`, `version`, `language`).

**consent_record**: `id`, `user_id`, `consent_policy_id`, `status`
(`granted`, `withdrawn`), `method` (`digital_signature`,
`captured_acknowledgment`), `artifact_key` (encrypted signature image or
scanned form, optional), `granted_at`, `withdrawn_at`, `recorded_by`.
A worker "has consent" when their latest record is `granted` for the site's
current policy version.

### Configuration

**ppe_profile**: `id`, `site_id` (null = built-in), `name`, `kind`
(`open_face`, `clear_glasses`, `sunglasses`, `mask`, `helmet_shield`,
`custom`), `signals` JSONB (enabled flag and weight per signal),
`threshold_profile_id` (nullable), `is_builtin`.

**threshold_profile**: `id`, `site_id`, `name`, `values` JSONB (partial
`Thresholds` model; only overridden keys). Resolution order, later wins per
key: built-in defaults -> site default -> PPE profile -> shift -> workstation.

**shift**: `id`, `site_id`, `line_id` (null = whole site), `name`,
`start_time`, `end_time` (local time, may cross midnight), `days_of_week`,
`threshold_profile_id` (nullable).

**shift_break**: `id`, `shift_id`, `name`, `start_time`, `end_time`.

**workstation_config**: `workstation_id`, `version` (int, increasing),
`payload` JSONB (the full bundle sent to the device), `created_by`,
`created_at`, `note`. PK (`workstation_id`, `version`). Old versions are kept
for rollback.

**station_assignment**: `id`, `workstation_id`, `user_id`, `assigned_by`,
`assigned_at`, `valid_until` (shift end), `status` (`pending`, `applied`,
`superseded`, `expired`). Supervisor fallback for worker login.

### Operational data

**worker_session**: `id` (client), `workstation_id`, `user_id`, `method`
(`employee_id_pin`, `supervisor_assigned`), `station_assignment_id`
(nullable), `started_at`, `ended_at`, `end_reason` (`logout`, `timeout`,
`shift_end`, `replaced`, `assignment_expired`, `supervisor_ended`),
`received_at`.

**scan_record**: `id` (client), `workstation_id`, `worker_session_id`
(nullable: unattributed scan), `user_id` (nullable), `barcode`, `scanned_at`,
`state_at_scan`, `confidence`, `outcome` (`attentive`, `warned`,
`unmonitored`), `received_at`. Index (`workstation_id`, `scanned_at`),
(`user_id`, `scanned_at`).

**attention_event**: `id` (client), `workstation_id`, `user_id` (nullable),
`worker_session_id` (nullable), `kind` (`scan_warning`, `state_episode`),
`state`, `confidence`, `low_confidence` (fusion disagreement),
`started_at`, `ended_at` (null for scan warnings), `scan_record_id`
(nullable, unique), `signals` JSONB (signal snapshot), `camera_states` JSONB
(per-camera state and quality), `engine_version`, `config_version`,
`images_withheld` (`none`, `no_consent`, `no_worker`, `on_break`,
`disk_full`),
`review_status` (`pending`, `confirmed`, `false_positive`), `reviewed_by`,
`reviewed_at`, `review_note`, `escalated`, `received_at`.
Index (`workstation_id`, `started_at`), (`user_id`, `started_at`),
(`review_status`).

**evidence_image**: `id` (client), `attention_event_id`, `camera_id`,
`seq`, `offset_ms` (relative to the event; negative = pre-frame),
`captured_at`, `storage_key`, `sha256`, `size_bytes`, `wrapped_dek`, `nonce`,
`kek_id`, `received_at`, `removed_at`, `removed_reason` (`retention`,
`approved_deletion`, `consent_withdrawal`). The row stays after removal; the
file and key material do not.

**attention_bucket**: `workstation_id`, `user_id` (nullable),
`bucket_start` (minute), `seconds` JSONB (seconds per state, sums to <= 60),
`received_at`. PK (`workstation_id`, `bucket_start`, `user_id`). Feeds
counters, scores and reports without storing a per-frame log.

Attention score = attentive seconds / (attentive + distracted + drowsy +
sleeping + absent seconds) x 100. `unknown` and `on_break` are excluded.

**dispute**: `id` (client), `attention_event_id`, `raised_by` (the worker),
`raised_at`, `message`, `reason` (nullable: `was_attentive`, `camera_view`,
`on_break`, `other_task`, `other`), `status` (`open`, `upheld`, `rejected`),
`resolved_by`, `resolved_at`, `resolution_note`.

**alert**: `id`, `site_id`, `workstation_id` (nullable), `user_id`
(nullable), `kind` (`sleeping`, `absent`, `repeated_warnings`,
`camera_offline`, `monitoring_unavailable`, `workstation_offline`,
`engine_restart`, `disk_full`, `clock_drift`, `server_health`), `severity`,
`status` (`open`, `acknowledged`, `closed`), `dedupe_key`, `opened_at`,
`acknowledged_by`, `acknowledged_at`, `closed_at`, `payload` JSONB.
Partial unique index on `dedupe_key` where `status <> 'closed'`.

**alert_delivery**: `id`, `alert_id`, `channel` (`dashboard`, `email`,
`sms`, `whatsapp`), `recipient`, `status` (`pending`, `sent`, `failed`),
`attempts`, `last_error`, `sent_at`.

**device_health**: `id`, `workstation_id`, `at`, `cpu_pct`, `mem_pct`,
`disk_free_mb`, `cameras` JSONB (status and fps per slot), `engine_status`,
`engine_restarts`, `queue_depth`, `clock_offset_ms`, `app_version`. One row
per minute per workstation (heartbeats in between only update `workstation`).

**live_view_session**: `id`, `user_id`, `workstation_id`, `camera_id`,
`mode` (`still`, `video`), `started_at`, `ended_at`, `frames_served`.
Every row also produces an `audit_log` entry.

### Governance

**evidence_deletion_request**: `id`, `site_id`, `requested_by`, `reason`,
`status` (`pending`, `approved`, `rejected`, `executed`, `cancelled`),
`decided_by`, `decided_at`, `decision_note`, `executed_at`. CHECK
(`decided_by <> requested_by`). Both must hold the Admin role at decision
time (enforced in the service and tested).

**evidence_deletion_item**: `request_id`, `attention_event_id`.

**audit_log**: `id` bigserial, `at`, `actor_user_id`, `actor_workstation_id`,
`site_id`, `action` (dotted key, for example `evidence.view`), `object_type`,
`object_id`, `ip`, `detail` JSONB, `prev_hash`, `hash`. Append-only: the
application DB role has INSERT and SELECT only, and a trigger rejects UPDATE
and DELETE. `hash = sha256(prev_hash || canonical_row)` gives tamper evidence.
The retention purge removes rows older than `retention_audit_days` through a
separate privileged job and records the chain anchor in `purge_log`.

**purge_log**: `id` bigserial, `run_at`, `site_id`, `kind` (`images`,
`metadata`, `audit`, `consent_withdrawal`), `cutoff`, `items_removed`,
`bytes_freed`, `status`, `detail` JSONB.

**ingest_batch**: `workstation_id`, `batch_id`, `received_at`, `item_count`,
`result` JSONB. PK (`workstation_id`, `batch_id`). Kept 30 days; lets a
retried batch return the original response.

**report_schedule**: `id`, `site_id`, `report_kind`, `params` JSONB,
`format` (`xlsx`, `pdf`), `cron`, `recipients`, `created_by`, `is_active`.

**report_run**: `id`, `report_schedule_id` (nullable for ad hoc), `run_at`,
`status`, `file_key`, `error`.

**app_release**: `version`, `channel` (`pilot`, `stable`), `sha256`,
`signature`, `file_key`, `created_at`, `notes`. Used by auto-update (phase 7).

## Scope entity mapping

| Scope entity | Table(s) |
|---|---|
| Site, Line, Workstation, Camera | same names |
| User, Role | `user`, `role`, `supervisor_line` |
| Shift, Break | `shift`, `shift_break` |
| PPE Profile, Threshold Profile | same names |
| Attention Event, Evidence Image, Scan Record | same names, plus `attention_bucket` |
| Alert | `alert`, `alert_delivery` |
| Consent Record | `consent_record`, `consent_policy` |
| Audit Log | `audit_log`, `purge_log` |
| Device Health | `device_health` |
