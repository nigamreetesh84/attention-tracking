# server

Central server, package `ag_server` (created in task 1.1, built in phase 3).

Planned layout:

```
server/
  ag_server/
    api/device/     workstation-facing API (docs/api-contract.md)
    web/            console routes, Jinja2 templates, static (vendored HTMX)
    auth/           console auth, sessions, MFA, RBAC and scoping
    audit/          append-only audit log
    models/         SQLAlchemy models (docs/schema.md)
    services/       ingest, config, alerts, evidence, consent, reports, retention
    jobs/           scheduler entry point (purge, reports, alerts)
  alembic/          migrations
  alembic.ini
```
