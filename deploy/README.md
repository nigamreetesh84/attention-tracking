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

as the schema owner (secret `ag_migrate_database_url`), and exits. `app` and
`worker` wait for it (`service_completed_successfully`), so if the migration fails
they do not start against an old or half-migrated schema; check
`docker compose logs migrate`. Each migration runs in its own transaction. The initial migration also seeds
the three system roles and the built-in PPE profiles.

To review the DDL before applying it, render it without a database:

    alembic -c server/alembic.ini upgrade head --sql

Outside Docker (development), set `AG_DATABASE_URL` and run the same command.

## Database roles and the audit log

The app and worker connect as `attentionguard_app` (secret `ag_database_url`),
which `initdb/10-app-role.sql` creates the first time the database volume is
initialised. Migrations run as the owner `attentionguard` (secret
`ag_migrate_database_url`) and grant the app role DML on ordinary tables but
only INSERT and SELECT on `audit_log`; a trigger also rejects UPDATE, DELETE
and TRUNCATE on it for every role. Set `AG_DB_APP_ROLE` on `migrate` if you
name the app role differently. If the role is created after the migrations
ran, apply the grants once as the owner: run the SQL that
`ag_server.migrations.grants.grant_app_role("attentionguard_app")` returns.

Check the audit hash chains at any time (exit code 1 means tampering):

    docker compose -f deploy/docker-compose.yml exec app python -m ag_server.audit verify

It prints each chain's head hash; recording those outside the database also
detects rows removed from the newest end, which the chain alone cannot.
