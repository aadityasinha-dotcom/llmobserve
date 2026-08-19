#!/bin/bash
# Runs once, on first initialisation of the postgres data volume.
#
# Creates the role the API connects as. This role is deliberately NOT a
# superuser and NOT the owner of any table: Postgres exempts superusers from
# row-level security unconditionally, and exempts table owners unless the
# table is marked FORCE ROW LEVEL SECURITY. Connecting the API as the
# restricted role is what makes the tenant isolation in the migration real
# rather than decorative.
#
# Object-level grants are issued by the Alembic migration, not here, because
# the tables do not exist yet at this point.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_DB_USER}') THEN
            CREATE ROLE ${APP_DB_USER} LOGIN PASSWORD '${APP_DB_PASSWORD}';
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${APP_DB_USER};
    GRANT USAGE ON SCHEMA public TO ${APP_DB_USER};
EOSQL
