#!/usr/bin/env python3
"""What of the demo exists in the database: one `name=yes|no` line per part, for setup.sh.

    python scripts/check_setup.py

Runs in the road-safety-tools sidecar. Read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402

ROAD_DATA_TABLES = 15


def main() -> None:
    c = db.connect(db.DEPLOYMENT)

    def count(sql: str, **params) -> int:
        try:
            return c.execute(sql, params).fetchval() or 0
        except Exception:  # noqa: BLE001 - a missing table counts as nothing
            return 0

    checks = {
        "road_data": count("SELECT COUNT(*) FROM SYS.EXA_ALL_TABLES WHERE TABLE_SCHEMA = 'ROAD_DATA' "
                           "AND TABLE_ROW_COUNT > 0") >= ROAD_DATA_TABLES,
        "kafka_scripts": count("SELECT COUNT(*) FROM SYS.EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = 'KAFKA_EXTENSION'") >= 3,
        "simulation": count("SELECT COUNT(*) FROM ROAD_SAFETY.INFRA_SENSORS") >= 158
                      and count("SELECT COUNT(*) FROM ROAD_SAFETY.DATA_PROVIDERS") >= 8,
        "views": count("SELECT COUNT(*) FROM SYS.EXA_ALL_VIEWS WHERE VIEW_SCHEMA = 'ROAD_SAFETY' "
                       "AND VIEW_NAME = 'V_INCIDENTS_LIVE'") == 1,
        "service_tables": count("SELECT COUNT(*) FROM SYS.EXA_ALL_TABLES WHERE TABLE_SCHEMA = 'ROAD_SAFETY' "
                                "AND TABLE_NAME = 'INCIDENTS'") == 1,
    }
    c.close()
    for name, done in checks.items():
        print(f"{name}={'yes' if done else 'no'}")


if __name__ == "__main__":
    main()
