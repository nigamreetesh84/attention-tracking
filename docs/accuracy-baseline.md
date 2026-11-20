# Accuracy baseline

Written by the replay harness (`python -m ag_workstation.harness`); do not
edit by hand. The numbers the regression check reads are in
`accuracy-baseline.json` next to this file.

- Generated: 2026-11-20T00:00:00+00:00
- Source: trace (9 sessions, 13.7 minutes)
- Matching tolerance: 2.0 s

## Targets

| Target | Result |
|---|---|
| Sleeping recall above 95% | met (100.0%) |
| Fewer than 2 false alarms per 8-hour shift | met (0.00) |

## Event metrics per state

| State | Labelled | Detected | Predicted | Recall | Precision | Mean latency | Max latency |
|---|---|---|---|---|---|---|---|
| sleeping | 8 | 8 | 8 | 100.0% | 100.0% | 5.7 s | 8.2 s |
| absent | 1 | 1 | 1 | 100.0% | 100.0% | 10.2 s | 10.2 s |
| drowsy | 2 | 2 | 2 | 100.0% | 100.0% | 6.0 s | 12.0 s |
| distracted | 1 | 1 | 4 | 100.0% | 25.0% | 5.2 s | 5.2 s |

False alarms: 0 in 0.19 h of attentive footage.

## Fairness slices

A state is flagged when the slice's recall is more than 5 points below the
overall recall.

| Tag | Value | Sessions | sleeping | absent | drowsy | distracted | Flagged |
|---|---|---|---|---|---|---|---|
| glasses | false | 7 | 100.0% | 100.0% | 100.0% | 100.0% | - |
| glasses | true | 2 | 100.0% | n/a | n/a | n/a | - |
| lighting | bright | 1 | 100.0% | n/a | n/a | n/a | - |
| lighting | low | 2 | 100.0% | 100.0% | n/a | 100.0% | - |
| lighting | normal | 6 | 100.0% | n/a | 100.0% | n/a | - |

## Frame-level confusion (rows: labelled, columns: predicted)

| | attentive | distracted | drowsy | sleeping | absent | unknown | on_break |
|---|---|---|---|---|---|---|---|
| attentive | 6532 | 24 | 170 | 194 | 20 | 0 | 0 |
| distracted | 52 | 38 | 0 | 0 | 0 | 0 | 0 |
| drowsy | 120 | 0 | 170 | 0 | 0 | 0 | 0 |
| sleeping | 366 | 90 | 0 | 264 | 0 | 0 | 0 |
| absent | 102 | 0 | 0 | 0 | 48 | 0 | 0 |
