-- Runs once, when the PostgreSQL volume is first initialised (as the owner role).
-- Creates the limited login role the app and worker use. Migrations (run as the
-- owner by the `migrate` service) grant it DML on ordinary tables and only
-- INSERT and SELECT on audit_log. The password is read from the Docker secret
-- by psql, so it is never in this repository or on a command line.
\set app_password `cat /run/secrets/ag_app_db_password`
CREATE ROLE attentionguard_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
\unset app_password
