#!/usr/bin/env bash
# Load the CSVs from prepare_road_data.py into schema ROAD_DATA with exapump.
# One table per file, named after the file (road_segments.csv -> ROAD_DATA.ROAD_SEGMENTS).
# Existing tables are dropped and reloaded, so the script can be run again after a new prepare run.
#
#   scripts/load_road_data.sh [PROFILE] [FOLDER]
#   scripts/load_road_data.sh road-safety-demo ~/road-data
#
# PROFILE is an exapump connection profile (exapump profile list) with write access.
set -euo pipefail

PROFILE="${1:-road-safety-demo}"
FOLDER="${2:-$HOME/road-data}"
SCHEMA="ROAD_DATA"

shopt -s nullglob
files=("$FOLDER"/*.csv)
if [ ${#files[@]} -eq 0 ]; then
  echo "error: no CSV files in $FOLDER; run scripts/prepare_road_data.py first" >&2
  exit 1
fi

exapump sql -p "$PROFILE" "CREATE SCHEMA IF NOT EXISTS $SCHEMA" >/dev/null

for f in "${files[@]}"; do
  table="$SCHEMA.$(basename "$f" .csv | tr '[:lower:]' '[:upper:]')"
  echo "== $(basename "$f") -> $table"
  exapump sql -p "$PROFILE" "DROP TABLE IF EXISTS $table" >/dev/null
  exapump upload -p "$PROFILE" -t "$table" "$f"
done

echo "== row counts"
exapump sql -p "$PROFILE" "SELECT TABLE_NAME, TABLE_ROW_COUNT FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = '$SCHEMA' ORDER BY TABLE_NAME"
