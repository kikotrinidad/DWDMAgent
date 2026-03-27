#!/usr/bin/env bash
# =============================================================================
# setup_db.sh — Create the aiexperts PostgreSQL database and apply schema.
#
# Run this ONCE as a user with PostgreSQL superuser access, e.g.:
#   sudo -u postgres bash /opt/AIExperts/DWDMAgent/setup_db.sh
#
# After this script, the noc-monitor runtime only needs the aiexpert role.
# =============================================================================
set -euo pipefail

DB_NAME="aiexperts"
DB_USER="aiexpert"
SCHEMA_FILE="$(dirname "$0")/schema.sql"

echo "=== AIExperts DB Setup ==="

# ---------------------------------------------------------------------------
# 1. Load the password from .env so we don't prompt or hardcode it here
# ---------------------------------------------------------------------------
ENV_FILE="$(dirname "$0")/../.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env not found at $ENV_FILE"
    exit 1
fi

DB_PASS=$(grep '^DWDM_DB_PASSWORD=' "$ENV_FILE" | cut -d'=' -f2- | tr -d "'\"")
if [[ -z "$DB_PASS" ]]; then
    echo "ERROR: DWDM_DB_PASSWORD not set in $ENV_FILE"
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Create role if it doesn't exist
# ---------------------------------------------------------------------------
if psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
    echo "Role '${DB_USER}' already exists — skipping create."
else
    psql -c "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';"
    echo "Role '${DB_USER}' created."
fi

# ---------------------------------------------------------------------------
# 3. Create database if it doesn't exist
# ---------------------------------------------------------------------------
if psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    echo "Database '${DB_NAME}' already exists — skipping create."
else
    psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
    echo "Database '${DB_NAME}' created."
fi

# ---------------------------------------------------------------------------
# 4. Apply schema (idempotent — uses CREATE TABLE IF NOT EXISTS)
# ---------------------------------------------------------------------------
echo "Applying schema from ${SCHEMA_FILE} ..."
psql -d "${DB_NAME}" -f "${SCHEMA_FILE}"

# ---------------------------------------------------------------------------
# 5. Grant privileges on all tables and sequences
# ---------------------------------------------------------------------------
psql -d "${DB_NAME}" <<SQL
GRANT CONNECT ON DATABASE ${DB_NAME} TO ${DB_USER};
GRANT USAGE  ON SCHEMA public TO ${DB_USER};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO ${DB_USER};
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO ${DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO ${DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT                  ON SEQUENCES TO ${DB_USER};
SQL

echo ""
echo "=== Setup complete ==="
echo "  Database : ${DB_NAME}"
echo "  User     : ${DB_USER}"
echo "  Tables   : devices, raw_scrapes, ai_briefings,"
echo "             dwdm_topology, dwdm_inventory, dwdm_alarms,"
echo "             dwdm_conditions, dwdm_optical_channels"
