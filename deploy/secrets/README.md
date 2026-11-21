# Secrets (files in this folder are git-ignored except this note)

Create these files before `docker compose up`. Each holds one value, no quotes.
The app refuses to start in production with missing, placeholder or short (<32
characters) secrets.

Two database roles: `attentionguard` owns the schema and runs migrations;
`attentionguard_app` (created on the first start of the database volume by
`../initdb/10-app-role.sql`) is what the app and worker use. It cannot
change or delete audit log rows.

- `ag_db_password`: PostgreSQL password for the owner role `attentionguard`.
- `ag_migrate_database_url`: `postgresql+psycopg://attentionguard:<ag_db_password>@postgres:5432/attentionguard`
  (only the `migrate` service gets it).
- `ag_app_db_password`: a different password, for the app role `attentionguard_app`.
- `ag_database_url`: `postgresql+psycopg://attentionguard_app:<ag_app_db_password>@postgres:5432/attentionguard`
- `ag_kek`: key-encryption key, at least 32 random characters
  (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- `ag_jwt_signing_key`: a different random value of the same kind.
