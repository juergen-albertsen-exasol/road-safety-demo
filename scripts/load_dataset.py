#!/usr/bin/env python3
"""Load the open data (the release asset munich-road-data.zip) into the schema ROAD_DATA.

    python scripts/load_dataset.py <zip> [--force]

Runs in the road-safety-tools sidecar (`./setup.sh` copies the zip in and calls it). Creates the
tables from the zip's schema.sql, imports one Parquet file per table and checks every row count
against manifest.json. Tables whose row count already matches are skipped unless --force: running
it again repairs a half-finished load.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402

SOURCE_SCHEMA = "ROAD_DATA"  # as written in schema.sql


def statements(schema_sql: str, schema: str) -> dict[str, str]:
    """CREATE TABLE statements by table name, from schema.sql, for the target schema."""
    out = {}
    for stmt in schema_sql.split(";\n"):
        m = re.search(rf"CREATE OR REPLACE TABLE {SOURCE_SCHEMA}\.(\w+)", stmt)
        if m:
            out[m.group(1)] = stmt.strip().rstrip(";").replace(f"TABLE {SOURCE_SCHEMA}.", f"TABLE {schema}.", 1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("zip", type=Path)
    ap.add_argument("--force", action="store_true", help="reload every table")
    ap.add_argument("--deployment", default=db.DEPLOYMENT)
    ap.add_argument("--schema", default=SOURCE_SCHEMA, help=argparse.SUPPRESS)  # for testing the zip
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(args.zip) as z:
            z.extractall(tmp)
        folder = next(Path(tmp).glob("*/manifest.json")).parent
        manifest = json.loads((folder / "manifest.json").read_text())
        SCHEMA = args.schema
        creates = statements((folder / "schema.sql").read_text(), SCHEMA)

        c = db.connect(args.deployment, compression=True)
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        have = {r[0]: r[1] for r in c.execute(
            "SELECT TABLE_NAME, TABLE_ROW_COUNT FROM SYS.EXA_ALL_TABLES WHERE TABLE_SCHEMA = {s}", {"s": SCHEMA})}
        loaded = skipped = 0
        for table, info in manifest["tables"].items():
            if not args.force and have.get(table) == info["rows"]:
                skipped += 1
                continue
            t0 = time.time()
            c.execute(creates[table])
            c.import_from_parquet(folder / info["file"], (SCHEMA, table))
            rows = c.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}").fetchval()
            if rows != info["rows"]:
                sys.exit(f"{table}: {rows:,} rows loaded, {info['rows']:,} expected")
            print(f"{SCHEMA}.{table}: {rows:,} rows ({time.time() - t0:.1f} s)", flush=True)
            loaded += 1
        c.close()
    print(f"open data: {loaded} tables loaded, {skipped} already complete")


if __name__ == "__main__":
    main()
