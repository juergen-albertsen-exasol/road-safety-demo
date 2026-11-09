#!/usr/bin/env python3
"""Compose the dashboard's query files from the SQL templates in roadsafety/.

The views (simulate_road_safety.py views) and the dashboard run the same SQL. Templates that read another
template's result ({traffic_sql!r}, {hazards_sql!r}) get it inlined here, so each dashboard query is one
statement with plain pyexasol placeholders.

    .venv/bin/python scripts/build_dashboard_queries.py <app folder>/queries
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "roadsafety"


def body(name: str) -> str:
    text = (SQL / f"{name}.sql").read_text().strip().rstrip(";")
    return "\n".join("    " + line if line else line for line in text.splitlines())


def inline(name: str, placeholder: str, inner: str) -> str:
    text = (SQL / f"{name}.sql").read_text()
    text = "\n".join(l for l in text.splitlines() if not l.startswith(f"--   {{{placeholder}!r}}"))
    return text.replace(f"({{{placeholder}!r}})", "(\n" + body(inner) + "\n    )")


def after_hazards(name: str) -> str:
    """hazards.sql up to its output, followed by a template that reads its CTEs (evidence.sql)."""
    return compose_after_hazards((SQL / "hazards.sql").read_text(), (SQL / f"{name}.sql").read_text())


def compose_after_hazards(hazards: str, tail: str) -> str:
    head = hazards[:hazards.index("-- @@ output")].rstrip()
    comments = "\n".join(l for l in tail.splitlines() if l.startswith("--"))
    code = "\n".join(l for l in tail.splitlines() if not l.startswith("--"))
    return comments + "\n" + head + "\n" + code.strip()


# The dashboard shows its SQL; comment lines about the simulator stay in the templates, not on the page.
SIM_WORDS = re.compile(r"simulat|SCENARIO_LOG|inject|ground truth", re.I)


def no_sim(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not (l.lstrip().startswith("--") and SIM_WORDS.search(l)))


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "queries")
    out.mkdir(parents=True, exist_ok=True)
    head = "-- Generated from roadsafety/{src}. Don't edit here.\n"
    files = {
        "traffic_state.sql": ("traffic.sql", (SQL / "traffic.sql").read_text()),
        "congestion.sql": ("congestion.sql + traffic.sql", inline("congestion", "traffic_sql", "traffic")),
        "diversions.sql": ("diversions.sql + traffic.sql", inline("diversions", "traffic_sql", "traffic")),
        "hazards_live.sql": ("reach.sql + hazards.sql", inline("reach", "hazards_sql", "hazards")),
        "hazard_evidence.sql": ("hazards.sql + evidence.sql", after_hazards("evidence")),
    }
    for name, (src, text) in files.items():
        (out / name).write_text(head.format(src=src) + no_sim(text).strip() + "\n")
        print(f"  {out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
