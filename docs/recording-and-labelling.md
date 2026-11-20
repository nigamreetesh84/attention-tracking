# Recording and labelling test videos

The accuracy targets (sleeping recall above 95%, fewer than 2 false alarms
per 8-hour shift) can only be shown on real, labelled footage. This guide
says how to record it, label it and run the harness. Videos and images are
never committed to git; only label files and numeric signal traces are.

## Before recording

- Every person filmed signs the consent form for test recordings. Recordings
  are kept on an access-controlled share, not on laptops or in chat tools.
- Record on the pilot hardware: the same cameras, mounts, lighting and PPE as
  the stations. Note the camera model and whether it is IR.
- Calibrate each camera first (neutral head pose) as the console will.

## What to record

Aim for at least 2 hours of footage per PPE profile in use, from several
people, covering:

| Scenario | How | Label |
|---|---|---|
| Normal work | Scan items at normal pace for 10+ minutes | attentive |
| Long blink, short glance away | 2-3 s eyes closed; 2-3 s looking at a colleague | attentive (should not alarm) |
| Looking away | Head turned or down at a phone for 6-15 s | distracted |
| Drowsiness | Heavy eyelids, slow blinks; three or more yawns within 5 minutes; nodding | drowsy |
| Sleeping | Eyes closed 6-20 s; head dropped and still 10-20 s | sleeping |
| Leaving | Walk away from the station for 15-60 s | absent |
| Bad image | Hand over the lens; lights off | unknown |

Sleeping and drowsy are acted. Ask for natural, varied acting (head tilt,
slump, hand on chin) rather than one pose.

Cover the fairness groups the workforce actually has: age bands, skin tone
groups, glasses (clear and tinted), masks, helmets and face shields, and low
and bright lighting. A slice with fewer than 5 sessions gives noisy numbers.

For two-camera stations, record both cameras at the same time and start
them together; give both label files the same `session` key.

## File layout

```
AG_TEST_VIDEOS/
  ws1_0007/
    ws1_cam1_0007.mp4
    ws1_cam1_0007.labels.json
    ws1_cam2_0007.mp4
    ws1_cam2_0007.labels.json
```

## Label files

One JSON file per video, named `<video stem>.labels.json`. See
`tests/fixtures/labels/` for examples.

```json
{
  "video": "ws1_cam1_0007.mp4",
  "fps": 15,
  "camera_role": "primary",
  "ppe_profile": "open_face",
  "session": "ws1_0007",
  "tags": {"glasses": false, "lighting": "low", "age_band": "30-45",
           "skin_tone_group": "IV-VI"},
  "intervals": [
    {"start_s": 0.0, "end_s": 41.2, "state": "attentive"},
    {"start_s": 41.2, "end_s": 50.0, "state": "sleeping"}
  ]
}
```

Rules:

- `video` is a plain file name next to the label file.
- `fps` is the frame rate the video was recorded at; it sets frame times.
- Intervals are in order, do not overlap and cover the whole video.
- States: `attentive`, `distracted`, `drowsy`, `sleeping`, `absent`,
  `unknown`. Cut breaks out of the video instead of labelling them.
- Label what is happening, not what the system should say: an episode starts
  when the eyes close or the head turns, not when the timer would fire. The
  harness measures the delay as latency.
- Tags are coarse groups only. Never put a name, employee ID or any other
  identifier in a label; unknown fields are rejected.
- Two people label each video independently; differences over 1 s are
  resolved together.

## Running the harness

```powershell
$env:AG_TEST_VIDEOS = "D:\ag-test-videos"
$env:AG_MODEL_DIR = "D:\ag-models"          # face_landmarker.task, pose_landmarker_lite.task
.venv\Scripts\python -m ag_workstation.harness run $env:AG_TEST_VIDEOS
```

First run on the real set: write the baseline into `docs/` and commit it.

```powershell
.venv\Scripts\python -m ag_workstation.harness run $env:AG_TEST_VIDEOS --write-baseline docs
```

After that, `pytest -m video` replays the set and fails on a regression
against `docs/accuracy-baseline.json`.

Until the real set exists, the committed baseline comes from the synthetic
traces in `tests/fixtures/traces/` (regenerate with
`python -m ag_workstation.harness.fixtures`). Those numbers only show the
harness and the engine logic work; they say nothing about real cameras.

## Signal traces

A trace holds the numeric signals of a recording, no images. Traces can be
committed and replayed without the videos, which makes them the way to share
pilot data for threshold tuning (task 6.2) and to pin regressions. Production
trace recording is task 6.1.
