#!/bin/sh
# Entrypoint of the road safety demo's Dash Server sidecar (see Containerfile).
# The database connection comes from the launcher (secretKeyRef to exasol-database): EXASOL_DB_HOST,
# EXASOL_DB_PORT, EXASOL_DB_USER, EXASOL_DB_PASSWORD. No password is printed or kept on disk.
set -eu

BOOT=/opt/road-safety/bootstrap.py
PY=/opt/dash-server/venv/bin/python

# 1. The read-only user gets a new password; it reaches Dash Server only through EXA_PASSWORD.
umask 077
PWFILE=$(mktemp)
"$PY" "$BOOT" prepare "$PWFILE"
EXA_PASSWORD=$(cat "$PWFILE")
rm -f "$PWFILE"
export EXA_PASSWORD
export DASH_SERVER_EXASOL_DSN="${EXASOL_DB_HOST}:${EXASOL_DB_PORT}"

# 3. In the background: once the demo's schemas exist, grant SELECT on them to the read-only user
#    and deploy dashboard v2 over Dash Server's MCP endpoint.
env -u EXA_PASSWORD "$PY" "$BOOT" deploy &

# 2. Dash Server itself, in the foreground (the image's own entrypoint), without the admin login.
unset EXASOL_DB_PASSWORD EXASOL_DB_USER
exec /opt/dash-server/entrypoint.sh
