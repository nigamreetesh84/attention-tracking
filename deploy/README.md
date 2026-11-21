# deploy

Deployment assets.

Planned contents:

- `docker-compose.yml`: postgres, app, worker (scheduler), Caddy TLS proxy.
  Only the proxy publishes ports (80, 443).
- `Dockerfile`, `Caddyfile`, `.env.example`.
- `secrets/` (git-ignored; see `secrets/README.md` for the files to create).
- Backup and restore scripts (phase 5).
- Workstation build, MSI installer and release signing scripts (phase 7).
