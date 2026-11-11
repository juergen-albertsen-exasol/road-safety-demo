"""Create the Exasol Kafka connector's UDF scripts in KAFKA_EXTENSION (roadsafety/kafka_extension.sql).

    python scripts/setup_kafka.py          # setup.sh runs it in the road-safety-tools sidecar

The rest of the Kafka setup is in setup.sh: the sidecars `kafka` (Apache Kafka in KRaft mode, one
node) and `kafka-ui` (Kafbat UI), the connector JAR in BucketFS and the Java script language
container. Kafka keeps nothing across `exasol stop` (sidecars have no volumes): topics start empty
after each start; Exasol is the system of record (see docs/plan.md, revision 2026-11-09).

Addresses:
- from the database (Kafka connector UDFs): Kafka's IP on the deployment network, port 9092. The UDF
  sandbox has no DNS (exasol/exasol-personal#464), so Kafka advertises its IP, not its name.
- from other sidecars (the service, the simulator, Kafbat UI): kafka:9092
- from the laptop:                          127.0.0.1:19092
- Kafbat UI:                                http://127.0.0.1:18080
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402


def create_scripts(deployment: str) -> None:
    sql = (ROOT / "roadsafety" / "kafka_extension.sql").read_text()
    c = db.connect(deployment)
    c.execute("CREATE SCHEMA IF NOT EXISTS KAFKA_EXTENSION")
    scripts = re.findall(r"^CREATE OR REPLACE JAVA .*?^/$", sql, re.M | re.S)
    for script in scripts:
        # Send the closing "/" too: without it the database drops the ";" ending the last option
        # (`%jar ...;`), and the Java UDF fails with "Error parsing script options".
        c.execute(script)
    c.close()
    print(f"UDF scripts: {len(scripts)} created in KAFKA_EXTENSION")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment", default=db.DEPLOYMENT)
    create_scripts(parser.parse_args().deployment)


if __name__ == "__main__":
    main()
