# deploy

Deployment assets.

Planned contents:

- `docker-compose.yml`: postgres, migrate, app, worker (scheduler), Caddy TLS
  proxy. Only the proxy publishes ports (80, 443).
- `Dockerfile`, `Caddyfile`, `.env.example`.
- `secrets/` (git-ignored; see `secrets/README.md` for the files to create).
- Backup and restore scripts (phase 5).
- Workstation build, MSI installer and release signing scripts (phase 7).

## Database migrations

Schema changes ship only as Alembic migrations (`server/ag_server/migrations`).
On every `docker compose up` the one-shot `migrate` service runs

    alembic -c server/alembic.ini upgrade head

with the same `ag_database_url` secret as the app, and exits. `app` and
`worker` wait for it (`service_completed_successfully`), so if the migration fails
they do not start against an old or half-migrated schema; check
`docker compose logs migrate`. Each migration runs in its own transaction. The initial migration also seeds
the three system roles and the built-in PPE profiles.

To review the DDL before applying it, render it without a database:

    alembic -c server/alembic.ini upgrade head --sql

Outside Docker (development), set `AG_DATABASE_URL` and run the same command.
