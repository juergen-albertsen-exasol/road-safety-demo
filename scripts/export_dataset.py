#!/usr/bin/env python3
"""Package the open data in ROAD_DATA as the release asset `munich-road-data.zip` (maintainers only).

    .venv/bin/python scripts/export_dataset.py [--out dist]

Run it on a deployment where ROAD_DATA was built by prepare_road_data.py and load_road_data.sh.
The zip holds one Parquet file per table, `schema.sql` (the CREATE TABLE statements, so the loader
recreates the exact column types), `manifest.json` (row counts, checked after loading) and the
attribution and licenses of the sources. Users never run this: `./setup.sh` downloads the zip from
the GitHub release and `scripts/load_dataset.py` loads it.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.csv as pa_csv  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

SCHEMA = "ROAD_DATA"
NAME = "munich-road-data"

LICENSE_TEXT = """\
Munich road data for the road safety demo (https://github.com/exasol-labs/road-safety-demo)

This data set is derived from open data. Each table's source, license and attribution are listed in
the table ROAD_DATA_SOURCES (road_data_sources.parquet) and below.

- ROAD_SEGMENTS, ROAD_AREAS, PLACES: road network, forests, parks and place names
  © OpenStreetMap contributors, Open Database License 1.0 (https://opendatacommons.org/licenses/odbl/1-0/).
  These tables are a derived database and are shared under the ODbL 1.0.
- DISTRICTS, ROADWORKS: Landeshauptstadt München, Datenlizenz Deutschland – Namensnennung 2.0
  (https://www.govdata.de/dl-de/by-2-0). Roadworks are a snapshot taken on 2026-10-21.
- MOTORWAY_EVENTS: © Die Autobahn GmbH des Bundes, GeoNutzV.
- WEATHER_10MIN, WEATHER_VISIBILITY, WEATHER_STATIONS: Deutscher Wetterdienst, CC BY 4.0.
- BAST_STATIONS, TRAFFIC_PROFILE, SEGMENT_TRAFFIC: traffic volumes modelled from counts of the
  Bundesanstalt für Straßenwesen (CC BY 4.0).
- ACCIDENTS: Statistische Ämter des Bundes und der Länder, Unfallatlas, dl-de/by-2-0
  (www.govdata.de/dl-de/by-2-0); clipped to the Munich area and matched to road segments.
- TRAFFIC_HOTSPOTS: researched for this demo, with the source of each entry.

Changes: clipped to Munich and its motorway ring, split into road segments, joined and aggregated
as described in the repository (scripts/prepare_road_data.py, research/road-data-sources.md).
"""


def columns(c, table: str) -> list[tuple[str, str]]:
    return c.execute(
        "SELECT COLUMN_NAME, COLUMN_TYPE FROM SYS.EXA_ALL_COLUMNS WHERE COLUMN_SCHEMA = {s} AND COLUMN_TABLE = {t} "
        "ORDER BY COLUMN_ORDINAL_POSITION", {"s": SCHEMA, "t": table}).fetchall()


def ddl(table: str, cols: list[tuple[str, str]]) -> str:
    body = ",\n".join(f'    "{name}" {ctype.replace(" UTF8", "")}' for name, ctype in cols)
    return f"CREATE OR REPLACE TABLE {SCHEMA}.{table} (\n{body}\n);\n"


def arrow_type(ctype: str):
    """The Parquet type for an Exasol column type: explicit, so empty leading values can't mislead."""
    if ctype.startswith("DECIMAL(") and ctype.endswith(",0)"):
        return pa.int64()
    if ctype.startswith("DECIMAL") or ctype.startswith("DOUBLE"):
        return pa.float64()
    if ctype == "DATE":
        return pa.date32()
    if ctype.startswith("TIMESTAMP"):
        return pa.timestamp("us")
    if ctype == "BOOLEAN":
        return pa.bool_()
    return pa.string()


def export_parquet(c, table: str, cols: list[tuple[str, str]], target: Path) -> None:
    types = {name: arrow_type(ctype) for name, ctype in cols}

    def write(pipe, dst, **_):
        t = pa_csv.read_csv(pipe, convert_options=pa_csv.ConvertOptions(
            column_types=types, strings_can_be_null=True))  # default parser: ISO 8601
        pq.write_table(t, dst, compression="zstd")

    c.export_to_callback(write, target, f"SELECT * FROM {SCHEMA}.{table}", export_params={"with_column_names": True})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--deployment", default=db.DEPLOYMENT)
    args = ap.parse_args()

    c = db.connect(args.deployment, compression=True)
    tables = [r[0] for r in c.execute(
        "SELECT TABLE_NAME FROM SYS.EXA_ALL_TABLES WHERE TABLE_SCHEMA = {s} ORDER BY TABLE_NAME", {"s": SCHEMA})]
    if not tables:
        sys.exit(f"no tables in {SCHEMA}; build it first (prepare_road_data.py, load_road_data.sh)")

    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{NAME}.zip"
    manifest = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tables": {}}
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        schema_sql = [f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};\n"]
        for table in tables:
            cols = columns(c, table)
            f = Path(tmp) / f"{table.lower()}.parquet"
            export_parquet(c, table, cols, f)
            rows = c.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}").fetchval()
            written = pq.read_metadata(f).num_rows
            if written != rows:
                sys.exit(f"{table}: {written} rows written, {rows} in the table")
            manifest["tables"][table] = {"file": f.name, "rows": rows}
            schema_sql.append(ddl(table, cols))
            z.write(f, f"{NAME}/{f.name}", compress_type=zipfile.ZIP_STORED)  # Parquet is compressed already
            print(f"{table}: {rows:,} rows, {f.stat().st_size / 1e6:.1f} MB")
        z.writestr(f"{NAME}/schema.sql", "\n".join(schema_sql))
        z.writestr(f"{NAME}/manifest.json", json.dumps(manifest, indent=2) + "\n")
        z.writestr(f"{NAME}/LICENSE.txt", LICENSE_TEXT)
    c.close()
    print(f"wrote {target} ({target.stat().st_size / 1e6:.1f} MB, {len(tables)} tables)")


if __name__ == "__main__":
    main()
