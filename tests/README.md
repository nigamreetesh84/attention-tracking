# tests

```
tests/
  shared/        ag_shared models
  workstation/   engine, queue, login, scan flow, sync, UI presenters
  server/        API, auth, RBAC, ingest, jobs, console
  integration/   workstation client against the real server app
  fixtures/      synthetic landmark sets, signal traces, label files
```

Markers: `video` (needs `AG_TEST_VIDEOS`), `slow` (load, soak, chaos),
`windows` (real service or DPAPI). Videos and images are never committed.
