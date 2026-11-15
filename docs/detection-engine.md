# Detection engine design (v1)

The engine runs on the workstation, needs no network, and turns camera frames
into one station state. It is advisory: it never blocks or delays a scan.

## 1. Pipeline

```
CameraSource (1-4)  ->  LandmarkProvider  ->  SignalExtractor  ->  CameraEstimator
   frames + ts           face/pose landmarks    numeric signals      per-camera state
                                                                          |
                         EvidenceRing (JPEG frames in memory)             v
                                                                   Fusion (multi-camera)
                                                                          |
                                                                          v
                                           StationStateMachine (timers, hysteresis, breaks)
                                                                          |
                                     StatePublisher: current state, 5 s window, buckets, events
```

Layering rules:

- `CameraSource` and `LandmarkProvider` are the only layers that import OpenCV
  or MediaPipe. Everything after them is pure Python over typed data, takes an
  injected clock, and is deterministic. That is where the unit tests and the
  80% coverage target apply.
- Each camera runs in its own worker thread. A camera that hangs or throws is
  restarted with backoff and reports `offline`; it cannot stall the others.
- The engine runs inside the Windows service process tree as a supervised
  child. If it crashes it is restarted within 5 s. While it is down the state
  is `Unknown` and scans continue.
- Target 10-15 FPS per camera. If the CPU cannot keep up, frames are dropped
  (latest-frame-wins); the engine never queues stale frames.

## 2. Signals

Extracted per camera per frame. Each has a value and a validity flag.

| Signal | Source | Notes |
|---|---|---|
| `face_present` | Face landmarker detection inside the ROI | Largest face in ROI wins |
| `quality` (0-1) | Detection confidence, face size, brightness, blur, landmark stability | Below threshold means the frame is unusable |
| `ear` | Eye aspect ratio from eye landmarks, both eyes | Normalized against a per-session open-eye baseline |
| `eyes_closed` | `ear` below threshold (or blink blendshape) | |
| `perclos` | Share of valid frames with eyes closed over 30 s | Needs at least 50% valid frames in the window |
| `blink_rate` | Closed-open transitions per minute | Informational, feeds drowsy confidence |
| `mar` / `yawn` | Mouth aspect ratio above threshold for more than 1.5 s | Disabled for mask profiles |
| `head_yaw`, `head_pitch`, `head_roll` | Head pose from the landmarker transform, relative to calibrated neutral | Calibration wizard stores neutral per camera |
| `head_nod` | Pitch drop and recovery pattern | Drowsiness cue |
| `head_dropped` | Pitch below the drop angle | Sleeping cue when eyes are unreliable |
| `posture` | Shoulder and head position from the pose model | Used by sunglasses and helmet profiles |
| `motion` | Landmark and upper-body movement energy | Stillness supports sleeping; movement contradicts it |

No face templates, embeddings or identity features are computed or stored.
Raw frames exist only in memory, except evidence frames written on an event
for a consenting, logged-in worker.

## 3. PPE profiles

A profile enables signals and sets their weights. Disabled signals are not
computed.

| Profile | Eyes (EAR, PERCLOS) | Yawn | Head pose | Posture, motion | Sleeping rule |
|---|---|---|---|---|---|
| Open face | on, 1.0 | on | on | off | eyes closed > 4 s |
| Clear glasses | on, 0.8, glare tolerance | on | on | off | eyes closed > 4 s, glare frames count as invalid |
| Sunglasses / tinted | off | on | on | on | head dropped and still > 8 s |
| Mask | on, 1.0 | off | on | off | eyes closed > 4 s |
| Helmet / face shield | on, 0.5 | off | on | on | eyes closed > 4 s with head drop, or head dropped and still > 8 s |
| Custom | admin chooses | | | | derived from enabled signals |

Glare tolerance: eye frames with specular highlights over the eye region are
marked invalid instead of open or closed. When too few eye frames are valid,
the estimator falls back to head pose rules and lowers confidence.

The worker's profile override (if any) replaces the station profile when that
worker's session starts.

## 4. Thresholds (defaults from the scope)

| Key | Default |
|---|---|
| `distracted_yaw_deg` / `distracted_pitch_deg` | 30 / 25 |
| `distracted_seconds` | 5 |
| `perclos_threshold` / `perclos_window_s` | 0.30 / 30 |
| `yawns_for_drowsy` / `yawn_window_s` | 3 / 300 |
| `sleeping_eyes_closed_s` | 4 |
| `sleeping_head_drop_s` (eyes unreliable) | 8 |
| `absent_seconds` | 10 |
| `quality_min` / `unknown_seconds` | 0.5 / 2 |
| `recovery_seconds` (back to Attentive) | 2 |
| `gap_tolerance_s` (brief interruptions do not reset a timer) | 0.5 |
| `scan_window_s` | 5 |

All are configurable per station, per shift and per profile. Resolution order
is in `schema.md` (threshold_profile).

## 5. State machine

States: Attentive, Distracted, Drowsy, Sleeping, Absent, Unknown, On Break.

Per camera, the estimator produces a candidate state and confidence from the
signals and timers:

| Candidate | Condition |
|---|---|
| Unknown | No frames, or `quality` below `quality_min` for `unknown_seconds` |
| Absent | Good-quality frames but no face for `absent_seconds` |
| Sleeping | Profile's sleeping rule holds |
| Drowsy | `perclos` above threshold, or yawn count reached, or repeated head nods |
| Distracted | Head yaw or pitch off-axis for `distracted_seconds` |
| Attentive | None of the above |

Station state machine (after fusion):

- Severity order, most to least: Sleeping, Absent, Drowsy, Distracted,
  Attentive. Unknown and On Break are outside the order.
- Entry uses the timers above. Exit to Attentive needs `recovery_seconds` of
  attentive candidates, so the state does not flicker.
- A more severe state replaces a less severe one immediately once its own
  timer is met. A less severe state replaces a more severe one only after
  recovery.
- **On Break** comes from the shift calendar and overrides everything. During a
  break no events, warnings or images are produced.
- No worker session: the engine still runs (the live wall shows the state),
  but no images are captured.
- Each transition into Distracted, Drowsy, Sleeping or Absent opens a
  `state_episode` event; leaving it closes the event with `ended_at`.
- Per-minute buckets record seconds per state for counters and reports.

### Scan decision

On a scan the workstation asks the engine for the scan state. The engine
answers from memory (no inference on the scan path; budget 200 ms, typical
under 5 ms):

1. If the current state is Sleeping, Drowsy, Distracted or Absent, that is the
   scan state.
2. Otherwise, the non-attentive state that held the largest share of the last
   `scan_window_s`, if that share is at least 40%.
3. Otherwise the current state (Attentive, Unknown or On Break).

Outcome: Attentive gives `attentive`; Distracted, Drowsy, Sleeping or Absent
gives `warned` (banner, sound, evidence, event); Unknown or On Break gives
`unmonitored` (no warning). If the engine does not answer within 150 ms the
state is Unknown. The scan is accepted in every case, before the query.

## 6. Multi-camera fusion

Camera 1 is primary, camera 2 secondary; cameras 3-4 are extra secondaries.

1. Primary usable (not Unknown, quality ok) and no secondary usable: use the
   primary.
2. Primary Unknown or low quality: use the best-quality usable secondary.
3. Primary and a secondary both usable and they agree: that state; confidence
   is the higher of the two.
4. They disagree: take the more severe state and set `low_confidence`.
5. Exception to rule 4 for presence: if any camera sees a face with good
   quality, the station is not Absent. The state comes from the cameras that
   see the face.
6. No usable camera: Unknown. If all cameras are offline the workstation shows
   "Monitoring unavailable".
7. Any camera offline or below quality for more than 30 s raises a degraded
   health alert; the station continues on the remaining cameras.

Fusion is a pure function of the per-camera candidates, so it is tested
exhaustively over the state combinations.

## 7. Evidence capture

- Each camera keeps a ring of recent JPEG frames in memory (default 3 s at
  2 fps). Nothing is written to disk until an event needs it.
- On a warned scan: the key frame at scan time, `pre_frames` (2) and
  `post_frames` (2) from the primary camera, plus the key frame from each
  usable secondary. `state_episode` events for Sleeping and Absent save one key
  frame; other episodes save none by default.
- Gate, checked at capture time: a worker session exists, the worker has
  consent in the roster, and it is not a break. Otherwise no frame is written
  and the event records `images_withheld`.
- Frames are encrypted before they touch the disk.

## 8. Config model

Pydantic models in `/shared`, used by server and workstation:

- `Thresholds`: section 4 keys with range validation.
- `PpeProfile`: `kind`, `signals: {name: {enabled, weight}}`, optional
  thresholds.
- `CameraConfig`: `slot`, `role`, `kind`, `uri`, `roi`, `calibration`.
- `CaptureConfig`: frame counts, interval, JPEG quality, ring length.
- `EngineConfig`: cameras, profile, resolved thresholds, per-shift thresholds,
  shifts and breaks, capture, `scan_window_s`.

The engine receives a complete, validated `EngineConfig`. A config change is
applied between frames without restarting the process; an invalid config is
refused and the old one stays.

## 9. Test method

**Unit tests (CI, no camera, no video).** Signal extractors on synthetic
landmarks; estimator, fusion and state machine on synthetic signal traces with
a fake clock. Every row of the scope's state table and every fusion rule has a
test, including boundaries (4.0 s vs 4.1 s eyes closed).

**Labeled video set.** Stored outside git; the path comes from
`AG_TEST_VIDEOS`. Tests that need it are marked `video` and skip when it is
absent. Each video has a sidecar label file:

```
{ "video": "ws1_cam1_0007.mp4", "fps": 15, "camera_role": "primary",
  "ppe_profile": "open_face",
  "tags": {"glasses": false, "lighting": "low", "age_band": "30-45",
           "skin_tone_group": "IV-VI"},
  "intervals": [ {"start_s": 0.0, "end_s": 41.2, "state": "attentive"},
                 {"start_s": 41.2, "end_s": 50.0, "state": "sleeping"} ] }
```

Two-camera recordings are paired by a shared `session` key so fusion is tested
on real footage.

**Replay harness.** Feeds video files through the full pipeline using frame
timestamps as the clock (deterministic, faster than real time) and writes the
predicted state timeline.

**Metrics.**

- Event-level recall and precision per state (an episode counts as detected if
  the predicted state overlaps it within a tolerance).
- Detection latency per state.
- False alarms per 8-hour shift, extrapolated from attentive footage.
- Frame-level confusion matrix.
- The same metrics per tag slice (fairness), with a flag when any slice is
  more than 5 points below the overall recall.

**Targets.** Sleeping recall above 95%; fewer than 2 false alarms per shift.
The harness writes `docs/accuracy-baseline.md`; a regression against the
stored baseline fails the `video` test run.

**Signal-trace recording.** The engine can record numeric signals (no images)
to a file. Traces are used for threshold tuning in the pilot and as
regression fixtures that can be committed to git.

**Performance tests.** Frames per second per camera on the target hardware,
state latency under 1 s, scan query under 200 ms. These need a real
workstation and are listed as manual checks until one is available.
