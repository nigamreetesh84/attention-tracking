# Secrets (files in this folder are git-ignored except this note)

Create these files before `docker compose up`. Each holds one value, no quotes.
The app refuses to start in production with missing, placeholder or short (<32
characters) secrets.

- `ag_db_password`: PostgreSQL password for role `attentionguard`.
- `ag_database_url`: `postgresql+psycopg://attentionguard:<same password>@postgres:5432/attentionguard`
- `ag_kek`: key-encryption key, at least 32 random characters
  (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- `ag_jwt_signing_key`: a different random value of the same kind.
