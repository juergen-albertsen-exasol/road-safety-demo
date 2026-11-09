#!/usr/bin/env bash
# Start Dash Server (https://github.com/exasol-labs/dash-server) with the Exasol profile the dashboard uses.
#
#   scripts/start_dash_server.sh [DASH_SERVER_DIR]
#
# DASH_SERVER_DIR is your clone of exasol-labs/dash-server with its .venv installed
# (default: ../dash-server next to this repo, or $DASH_SERVER_DIR). The profile `road-safety-demo`
# connects as dash_readonly (SELECT only); its password comes from secrets/dash_readonly_password,
# which scripts/setup_access.py creates. Dashboards: http://127.0.0.1:5100, MCP: http://127.0.0.1:5100/mcp
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${1:-${DASH_SERVER_DIR:-$ROOT/../dash-server}}"
PWFILE="$ROOT/secrets/dash_readonly_password"

if [ ! -x "$DIR/.venv/bin/dash-server" ]; then
  echo "error: no dash-server in $DIR/.venv; clone and install it first (see README.md)" >&2
  exit 1
fi
if [ ! -s "$PWFILE" ]; then
  echo "error: $PWFILE missing; run .venv/bin/python scripts/setup_access.py first" >&2
  exit 1
fi

EXA_PASSWORD="$(cat "$PWFILE")"
export EXA_PASSWORD
export DASH_SERVER_EXASOL_PROFILE_NAME="road-safety-demo"
export DASH_SERVER_EXASOL_DESCRIPTION="Road safety demo, read-only (ROAD_DATA, ROAD_SAFETY)"
export DASH_SERVER_EXASOL_DSN="127.0.0.1:8563"
export DASH_SERVER_EXASOL_USER="dash_readonly"
export DASH_SERVER_EXASOL_SECRET_ENV_VAR="EXA_PASSWORD"

cd "$DIR"
exec "$DIR/.venv/bin/dash-server"
