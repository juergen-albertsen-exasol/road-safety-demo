"""Set up Kafka for the deployment: Redpanda sidecars and the Exasol Kafka connector extension.

1. Adds Redpanda and Redpanda Console as user entries to the deployment's sidecars.yaml (the
   launcher only enables catalog sidecars with `exasol sidecar enable`).
2. Downloads the connector JAR, checks its SHA-256 and copies it into BucketFS. Local deployments
   have no BucketFS HTTP access; the bucket is a directory in the deployment.
3. Records the JAVA script language container if it's missing (active after the next restart).
4. Creates the UDF scripts in KAFKA_EXTENSION (roadsafety/kafka_extension.sql).

Every step is skipped when it's already done. Afterwards it says whether a start or a restart is
needed: new sidecars need `exasol start` (the database keeps running), a new Java container needs
`exasol stop` and `exasol start`.

    .venv/bin/python scripts/setup_kafka.py [--deployment road-safety-demo] [--no-console]

Addresses:
- from the database (Kafka connector UDFs): redpanda:9092
- from the laptop (simulator, loader):      127.0.0.1:19092
- Redpanda Console:                         http://127.0.0.1:18080

Redpanda keeps nothing across `exasol stop`: topics start empty after each start.
Exasol is the system of record (see docs/plan.md, revision 2026-11-09).
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONNECTOR_VERSION = "2.0.1"
CONNECTOR_JAR = f"exasol-kafka-connector-extension-{CONNECTOR_VERSION}.jar"
CONNECTOR_URL = f"https://github.com/exasol/kafka-connector-extension/releases/download/{CONNECTOR_VERSION}/{CONNECTOR_JAR}"
BUCKET_DIR = "local/runtime/exa/bucketfs/bfsdefault/default/kafka"  # /buckets/bfsdefault/default/kafka in SQL

REDPANDA_IMAGE = "docker.io/redpandadata/redpanda:v26.2.6"
CONSOLE_IMAGE = "docker.io/redpandadata/console:v3.13.1"

# Indented to sit under the top-level `sidecars:` key, like the launcher writes it.
REDPANDA = f"""\
    redpanda:
        enabled: true
        container:
            name: redpanda
            image: {REDPANDA_IMAGE}
            imagePullPolicy: IfNotPresent
            args:
                - redpanda
                - start
                - --mode
                - dev-container
                - --smp
                - "1"
                - --memory
                - 1G
                - --default-log-level=warn
                - --kafka-addr
                - internal://0.0.0.0:9092,external://0.0.0.0:19092
                - --advertise-kafka-addr
                - internal://redpanda:9092,external://127.0.0.1:19092
            ports:
                - name: kafka
                  containerPort: 19092
                  hostPort: 19092
                  hostIP: 127.0.0.1
                  protocol: TCP
                - name: kafka-internal
                  containerPort: 9092
                  protocol: TCP
            restartPolicy: Always
            readinessProbe:
                tcpSocket:
                    port: kafka
                periodSeconds: 5
                timeoutSeconds: 1
                failureThreshold: 24
"""

CONSOLE = f"""\
    redpanda-console:
        enabled: true
        container:
            name: redpanda-console
            image: {CONSOLE_IMAGE}
            imagePullPolicy: IfNotPresent
            env:
                - name: KAFKA_BROKERS
                  value: redpanda:9092
            ports:
                - name: http
                  containerPort: 8080
                  hostPort: 18080
                  hostIP: 127.0.0.1
                  protocol: TCP
            restartPolicy: Always
            readinessProbe:
                httpGet:
                    port: http
                    path: /
                periodSeconds: 5
                timeoutSeconds: 1
                failureThreshold: 24
        endpoints:
            - port: http
              scheme: http
              path: /
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment", default="road-safety-demo")
    parser.add_argument("--no-console", action="store_true", help="skip Redpanda Console")
    args = parser.parse_args()

    deployment = Path.home() / ".exasol/personal/deployments" / args.deployment
    added = add_sidecars(deployment / "sidecars.yaml", args.no_console)
    install_jar(deployment / BUCKET_DIR)
    java_new = ensure_java(args.deployment)
    create_scripts(args.deployment)

    if java_new:
        print(f"Next: exasol stop -d {args.deployment} && exasol start -d {args.deployment} (activates Java"
              f"{', starts the new sidecars' if added else ''})")
    elif added:
        print(f"Next: exasol start -d {args.deployment} (starts the new sidecars, the database keeps running)")
    else:
        print("Kafka setup complete.")


def add_sidecars(path: Path, no_console: bool) -> bool:
    text = path.read_text()
    if not re.search(r"^sidecars:\s*$", text, re.M):
        raise SystemExit(f"{path}: no top-level 'sidecars:' key, edit it by hand")
    wanted = [("redpanda", REDPANDA)] + ([] if no_console else [("redpanda-console", CONSOLE)])
    missing = [(name, block) for name, block in wanted if not re.search(rf"^    {name}:\s*$", text, re.M)]
    if not missing:
        print(f"sidecars: {', '.join(n for n, _ in wanted)} already defined")
        return False
    backup = path.with_suffix(f".yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(text.rstrip("\n") + "\n" + "".join(block for _, block in missing))
    print(f"sidecars: added {', '.join(n for n, _ in missing)} to {path} (backup: {backup.name})")
    return True


def install_jar(bucket: Path) -> None:
    target = bucket / CONNECTOR_JAR
    if target.exists():
        print(f"connector: {CONNECTOR_JAR} already in BucketFS")
        return
    with tempfile.TemporaryDirectory() as tmp:
        jar = Path(tmp) / CONNECTOR_JAR
        urllib.request.urlretrieve(CONNECTOR_URL, jar)
        expected = urllib.request.urlopen(CONNECTOR_URL + ".sha256").read().decode().split()[0]
        actual = hashlib.sha256(jar.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"connector: SHA-256 mismatch for {CONNECTOR_JAR} ({actual} != {expected})")
        bucket.mkdir(parents=True, exist_ok=True)
        shutil.copy2(jar, target)
    print(f"connector: {CONNECTOR_JAR} downloaded, checksum OK, copied to BucketFS")


def ensure_java(deployment: str) -> bool:
    out = subprocess.run(["exasol", "slc", "list", "-d", deployment], capture_output=True, text=True).stdout
    row = next((l for l in out.splitlines() if l.startswith("JAVA")), "")
    if row.split()[-1:] == ["yes"]:
        print("java: script language container installed")
        return False
    subprocess.run(["exasol", "slc", "install", "java", "--no-restart", "-d", deployment], check=True,
                   capture_output=True)
    print("java: script language container recorded, active after the next restart")
    return True


def create_scripts(deployment: str) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from kafka_loader import connect  # noqa: E402 - needs pyexasol from the project .venv

    sql = (ROOT / "roadsafety" / "kafka_extension.sql").read_text()
    try:
        db = connect(deployment)
    except Exception as e:  # noqa: BLE001
        print(f"UDF scripts: database not reachable ({e}); run this script again once it's up")
        return
    db.execute("CREATE SCHEMA IF NOT EXISTS KAFKA_EXTENSION")
    scripts = re.findall(r"^CREATE OR REPLACE JAVA .*?^/$", sql, re.M | re.S)
    for script in scripts:
        db.execute(script[:-1].strip())
    print(f"UDF scripts: {len(scripts)} created in KAFKA_EXTENSION")

if __name__ == "__main__":
    main()
