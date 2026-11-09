#!/usr/bin/env python3
"""Copy the SQL templates the dashboard shares with the service into the dashboard's query folder.

The service (scripts/road_safety_service.py) and the dashboard run the same combination SQL
(roadsafety/combine.sql): the service with the default rule, the dashboard with its sliders.

    .venv/bin/python scripts/build_dashboard_queries.py <app folder>/queries
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "roadsafety"


# The dashboard shows its SQL; comment lines about the simulator stay in the templates, not on the page.
SIM_WORDS = re.compile(r"simulat|SCENARIO_LOG|inject|ground truth", re.I)


def no_sim(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not (l.lstrip().startswith("--") and SIM_WORDS.search(l)))


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "queries")
    out.mkdir(parents=True, exist_ok=True)
    head = "-- Generated from roadsafety/{src}. Don't edit here.\n"
    files = {
        "incidents_live.sql": ("combine.sql", (SQL / "combine.sql").read_text()),
    }
    for name, (src, text) in files.items():
        (out / name).write_text(head.format(src=src) + no_sim(text).strip() + "\n")
        print(f"  {out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
