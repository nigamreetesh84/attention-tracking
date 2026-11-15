# workstation

Workstation client, package `ag_workstation` (created in task 1.1; engine in
phase 1, app in phase 2).

Planned layout:

```
workstation/
  ag_workstation/
    engine/
      capture/      CameraSource implementations (only place that imports OpenCV)
      landmarks/    LandmarkProvider (only place that imports MediaPipe)
      signals/      pure signal extractors
      estimator.py  per-camera state
      fusion.py     multi-camera fusion
      state.py      station state machine and scan decision
      evidence.py   in-memory frame ring and burst capture
      runtime.py    threaded pipeline
      eval/         replay harness, metrics, trace recorder
    service/        Windows service, engine supervisor, IPC
    store/          encrypted queue, local evidence store, key providers
    login/          LoginMethod plug-ins, sessions
    scan/           barcode input and scan flow
    sync/           API client, uploader, config, heartbeat
    ui/             PySide6 views and presenters
```
