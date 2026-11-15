# Attention Monitoring System: Finalized Scope and Enterprise Design (v1)

Your answers settle most open decisions. Below I record the assumptions I made, then the enterprise design. Still no code.

---

## 1. Decisions Locked from Your Answers

| # | Your answer | How I interpret it |
|---|---|---|
| 1 | Min 2, max flexible | The system supports unlimited workstations, with a **configurable 1-4 cameras per station** (default 2). Camera 1 is the primary face camera. Camera 2 is a secondary angle or wide view for redundancy and cross-checking. |
| 2 | Robust camera support | Support **USB, IP/RTSP and ONVIF cameras**, and built-in webcams for testing only. An IR/low-light camera is recommended for production. |
| 3 | Config parameter for glasses, masks, etc. | A **per-workstation "PPE profile"** (see 4.3) switches the detection method. |
| 4 | No ERP/MES in release 1 | A built-in barcode scan screen with a **standalone item log**. Design it so an integration adapter can be added in release 2. |
| 5 | Warn and log only | **Soft mode**: never block the scan. The scan always passes, but the system warns and records. A "blocking mode" setting exists but is **disabled by default** for later. |
| 6 | Camera/PC failure: "yes" | This is ambiguous. I assume **continue working (fail-open)**: the line never stops because of the monitoring system. It is a configurable setting, and an alert is raised. Please confirm. |
| 7 | Cloud or on-premise | **Deployment mode is a setting**: on-premise server (default), private cloud, or hybrid. |
| 8 | Retention configurable | **Default is 30 days** for event images, 365 days for event metadata and scores. It is configurable per site. |
| 9 | Consent unsure | I treat it as **mandatory**. The application includes a consent module (see 7). Do not go live without it. |
| 10 | Windows | Windows 10/11 IoT Enterprise at workstations. Windows Server (or Linux server) for the central system. |

---

## 2. Product Definition

**Name (working):** AttentionGuard (rename as you like)

**Purpose:** Monitor worker alertness at barcode scanning workstations, record evidence on events, and give supervisors live visibility, all in warn-and-log mode.

**Detection states (final):**

| State | Meaning | Default trigger |
|---|---|---|
| Attentive | Face visible, eyes open, facing the work area | Default state |
| Distracted | Looking away or down for a prolonged time | Head off-axis for more than 5 s |
| Drowsy | Eyes closing for long periods, yawning, nodding | PERCLOS above 30% over 30 s |
| Sleeping | Eyes closed continuously, head dropped | Eyes closed more than 4 s |
| Absent | No face at the station | No face for more than 10 s |
| Unknown | Camera blocked, poor light, face unreadable | Quality score below the threshold |
| On Break | Scheduled break | Shift calendar |

All thresholds are configurable per station, per shift and per profile.

---

## 3. Core Modules

### 3.1 Workstation Client (Windows desktop app)
- Full-screen **scan screen**: a big status indicator (green/amber/red), the last scanned item and a message area.
- **Worker login** by badge barcode, PIN, or optional face match.
- **Barcode input**: a keyboard-wedge scanner works out of the box, with serial/HID as options.
- **Edge detection engine** runs locally (no network needed to function).
- Warn-only behavior on a scan while inattentive: the item is accepted, a **warning banner and sound** appear ("Please stay attentive"), an **evidence image is saved** and the event is queued for the server.
- **Offline buffer**: a local encrypted queue syncs to the server when the connection returns.
- Self-protection: runs as a Windows service with auto-restart, so closing the app does not end monitoring.

### 3.2 Central Server
- API and business logic, database, image storage, scheduler (retention, reports), alert engine and device registry.

### 3.3 Admin Web Console
- Camera and workstation configuration with live preview, ROI (region) drawing and a calibration wizard.
- User, role and site management, plus PPE profiles and threshold templates.
- Shift and break calendar, retention and storage settings, and the consent registry.
- System health, audit logs, and backup/restore.

### 3.4 Supervisor Dashboard
- **Live wall**: a grid of stations with status color, the current worker, and counters.
- Per worker: counts of Attentive / Distracted / Drowsy / Sleeping / Absent for the current shift.
- Live view of the camera (only for assigned lines) with every view logged.
- Alert inbox: sleeping, absent, camera offline.
- Event review: view the image, then **Confirm or Mark as false positive**.

---

## 4. Detailed Behavior

### 4.1 Scan flow (warn-and-log)
1. The worker scans the barcode, and the system records it **immediately and always accepts it**.
2. The engine reads the rolling window state (last 5 s) at the scan time.
3. **Attentive:** green tick, the item is logged as "Attentive scan".
4. **Not attentive:** the item still passes, a **warning is displayed and an alert sound plays**, and the system saves **an image plus a short pre/post frame burst**, the state, confidence and barcode. The event is flagged for supervisor review.
5. Repeated warnings within X minutes escalate to a supervisor notification.

### 4.2 Multi-camera logic
- Camera 1 is the primary decision source and Camera 2 a secondary.
- **Fusion rule:** if the primary is Unknown or low quality, use the secondary. If both disagree, take the more conservative state and mark the confidence as low.
- If one camera fails, continue on the other and raise a degraded-health alert.

### 4.3 PPE / face-gear profiles (your config parameter)

| Profile | Detection method |
|---|---|
| Open face | Full: eyes, blink rate, PERCLOS, head pose, yawning |
| Clear glasses | Eye detection with glare tolerance, plus head pose |
| Sunglasses / tinted | Head pose, posture and motion only (eyes not reliable) |
| Mask | Eyes and upper-face landmarks; yawning detection off |
| Helmet / face shield | Head pose and body posture; reduced eye-closure weight |
| Custom | Admin selects which signals are enabled and their weights |

Each station is assigned a profile, and each worker can override it if needed.

### 4.4 Failure handling (fail-open default)

| Failure | Behavior |
|---|---|
| Camera offline | Status becomes "Unknown", scans continue, and an admin/supervisor alert is raised. |
| Both cameras offline | Same, plus a "Monitoring unavailable" banner. |
| Server unreachable | The workstation runs standalone and buffers events. |
| Detection engine crash | The service auto-restarts within seconds; the scan screen is unaffected. |
| Disk full | Oldest images are purged first and an alert is raised. |
| Clock drift | Time sync is enforced so evidence timestamps are trustworthy. |

---

## 5. Roles and Permissions (enterprise RBAC)

| Capability | Admin | Supervisor | Workstation User |
|---|---|---|---|
| Configure cameras, stations, thresholds | ✅ | ❌ | ❌ |
| Manage users and roles | ✅ | ❌ | ❌ |
| Live feeds | ✅ | ✅ (assigned lines) | ❌ |
| Attention counts and reports | ✅ | ✅ | Own only |
| Review/confirm events | ✅ | ✅ | ❌ |
| Delete evidence | ✅ (dual approval) | ❌ | ❌ |
| Audit logs | ✅ | ❌ | ❌ |
| Scan barcodes | ✅ | ❌ | ✅ |

Enterprise additions: **SSO/Active Directory/LDAP** option, password policy, MFA for admin, session timeout, and **site/plant-level scoping** (multi-plant ready). "Admin has all access" is kept, but every action is audit-logged and evidence deletion needs a second approver.

---

## 6. Data and Reporting

**Core entities:** Site, Line, Workstation, Camera, User, Role, Shift, Break, PPE Profile, Threshold Profile, Attention Event, Evidence Image, Scan Record, Alert, Consent Record, Audit Log, Device Health.

**Reports:**
- Attention score per worker, shift, line and day.
- Counts of Sleeping/Drowsy/Distracted/Absent events with trends.
- Scan quality (attentive vs warned scans).
- Camera uptime and false-positive rates.
- Exports to Excel and PDF, with scheduled email delivery.

**Retention:** images 30 days (configurable), metadata 365 days, audit logs at least 1 year, with automatic purge and a purge log.

---

## 7. Security, Privacy and Compliance (do not skip)

- **Consent module:** the worker acknowledges the policy at onboarding (digital signature or a captured acknowledgment), and a consent registry is stored. If consent is missing, the worker's images are not saved (metadata only).
- **Privacy notices** at the workstation (a visible "monitoring in progress" label).
- **Encryption** in transit (TLS) and at rest (images and database), with images stored in an access-controlled store.
- **Access to images is role-limited and logged.**
- **Data minimization:** images only on events, no continuous video recording, and the live view is not stored.
- **Worker rights:** self-view of their own data, a dispute/false-positive request process, and deletion per the retention policy.
- Legal basis under the **DPDP Act 2023**: get HR/legal sign-off on the policy before the pilot.
- Policy rule: **detections are an input to review, not automatic discipline.**

---

## 8. Recommended Technology Stack (for discussion)

| Layer | Recommendation | Why |
|---|---|---|
| Workstation client | Python or C# (.NET) desktop app with a Windows service | Runs on Windows and is robust on a PC |
| Detection | MediaPipe/OpenCV face landmarks, plus an optional ONNX-based custom classifier | Fast on CPU; the GPU is optional |
| Backend | FastAPI (Python) or ASP.NET Core | Enterprise-grade APIs |
| Database | PostgreSQL | Reliable, with strong audit and reporting support |
| Image storage | Encrypted file store, or MinIO/S3-compatible | Works on-premise and in cloud |
| Web UI | React + a component library | Admin and supervisor consoles |
| Live view/alerts | WebSockets or WebRTC | Low-latency dashboard |
| Deployment | Docker for the server, MSI installer for workstations | Repeatable rollout |
| Monitoring | Health endpoints, logs and an alerting channel | Operations readiness |

**Workstation spec (starting point):** a modern i5/Ryzen 5 class CPU, 16 GB RAM, SSD, with an optional low-power GPU. Test the frame rate before you finalize (target 10-15 FPS per camera).

---

## 9. Enterprise Quality Requirements

- **Performance:** state update latency under 1 s, scan decision under 200 ms.
- **Availability:** the workstation works fully offline, with server uptime of 99.5% or better.
- **Scalability:** a design target of 100+ workstations per site, then multi-site.
- **Testing:** unit tests, a recorded video test set with ground-truth labels, and accuracy targets (for example, sleeping detection recall above 95% and a false-alarm rate below 2 per shift).
- **Fairness testing** across your actual workforce (ages, skin tones, glasses, etc.).
- **Operations:** a silent installer, remote configuration push, auto-update with rollback, and backups.
- **Documentation:** admin manual, supervisor guide, installation runbook, and troubleshooting.

---

## 10. Delivery Plan

| Phase | Deliverable | Duration (indicative) |
|---|---|---|
| 0. Foundation | Legal/consent sign-off, hardware choice, final thresholds, detailed screens | 2 weeks |
| 1. Detection prototype | Single-station engine with labeled test videos, accuracy baseline | 3-4 weeks |
| 2. Workstation app | Scan screen, login, warn-and-log, offline queue, evidence capture | 3-4 weeks |
| 3. Server and admin | Database, APIs, camera configuration, users and roles, audit | 4 weeks |
| 4. Supervisor dashboard | Live wall, counters, alerts, event review | 3 weeks |
| 5. Reports and retention | Reports, exports, purge jobs, health monitoring | 2 weeks |
| 6. Pilot | 2 real workstations for 2-4 weeks, with threshold tuning | 4 weeks |
| 7. Rollout | Installer, training, phased deployment | Ongoing |

---

## 11. Items I Need You to Confirm

1. **Fail-open on camera or PC failure?** I assumed the line continues with an alert.
2. **Cameras per station:** is 2 the default (primary + secondary), or do you want a minimum of 2 workstations for the pilot instead?
3. **Pilot size:** how many workstations will actually exist at launch?
4. **Is the supervisor live view the camera video itself, or only status and still images?** The privacy impact differs.
5. **Who approves evidence deletion** (the second approver)?
6. **Languages for the UI** (English, Hindi, Telugu, etc.) and any worker-facing voice alerts?
7. **Alert channels:** dashboard only, or also email/SMS/WhatsApp/buzzer?
8. **Do you want me to prepare the full Software Requirements Document (SRD) with screen designs and the database structure as a Word/PDF file next?**

Once you confirm these, the next step is the detailed design (screen flows, database schema, detection thresholds), and then we start building in the phase order above.