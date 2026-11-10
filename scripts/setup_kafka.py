"""Set up Kafka for the deployment: Apache Kafka sidecars and the Exasol Kafka connector extension.

1. Adds Apache Kafka (KRaft mode, one node) and Kafbat UI as user entries to the deployment's
   sidecars.yaml (the launcher only enables catalog sidecars with `exasol sidecar enable`).
   Removes the Redpanda entries an earlier version of this script added.
2. Downloads the connector JAR, checks its SHA-256 and copies it into BucketFS. Local deployments
   have no BucketFS HTTP access; the bucket is a directory in the deployment.
3. Records the JAVA script language container if it's missing (active after the next restart).
4. Creates the UDF scripts in KAFKA_EXTENSION (roadsafety/kafka_extension.sql).

Every step is skipped when it's already done. Afterwards it says whether a start or a restart is
needed: changed sidecars need `exasol start` (the database keeps running), a new Java container
needs `exasol stop` and `exasol start`.

    .venv/bin/python scripts/setup_kafka.py [--deployment default] [--no-ui]

Addresses:
- from the database (Kafka connector UDFs): Kafka's IP on the deployment network, port 9092
- from other sidecars (the service, Kafbat UI): kafka:9092
- from the laptop (simulator, loader):      127.0.0.1:19092
- Kafbat UI:                                http://127.0.0.1:18080

Both are Apache 2.0 and send nothing home. Kafka keeps nothing across `exasol stop` (sidecars have
no volumes): topics start empty after each start. Exasol is the system of record (see
docs/plan.md, revision 2026-11-09).
"""

import argparse
import hashlib
import os
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

KAFKA_IMAGE = "docker.io/apache/kafka:4.4.0"
KAFKA_UI_IMAGE = "docker.io/kafbat/kafka-ui:v1.5.0"
LEGACY = ("redpanda", "redpanda-console")  # added by earlier versions of this script


def env(**kv: str) -> str:
    return "".join(f"                - name: {k}\n                  value: \"{v}\"\n" for k, v in kv.items())


# Indented to sit under the top-level `sidecars:` key, like the launcher writes it.
# The image takes its whole configuration from KAFKA_* variables once any is set.
# Kafka advertises its own IP on the deployment network, not the name `kafka`: the database's UDFs
# have no DNS (exasol/exasol-personal#464), and a client follows the advertised address. The IP is
# known only inside the container, so the command sets it before starting the image's own entrypoint.
# (`set --` instead of `$(...)`: the launcher expands `$(NAME)` in commands.)
ADVERTISE = ('set -- `hostname -i`; '
             'export KAFKA_ADVERTISED_LISTENERS="INTERNAL://$1:9092,EXTERNAL://127.0.0.1:19092"; '
             'exec /__cacert_entrypoint.sh /etc/kafka/docker/run')
KAFKA = f"""\
    kafka:
        enabled: true
        container:
            name: kafka
            image: {KAFKA_IMAGE}
            imagePullPolicy: IfNotPresent
            command:
                - sh
                - -c
                - '{ADVERTISE}'
            env:
{env(CLUSTER_ID="lCta9CuTTLqZ1cPFipnIlw",
     KAFKA_NODE_ID="1",
     KAFKA_PROCESS_ROLES="broker,controller",
     KAFKA_CONTROLLER_QUORUM_VOTERS="1@localhost:9093",
     KAFKA_LISTENERS="INTERNAL://0.0.0.0:9092,EXTERNAL://0.0.0.0:19092,CONTROLLER://localhost:9093",
     KAFKA_LISTENER_SECURITY_PROTOCOL_MAP="INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT",
     KAFKA_CONTROLLER_LISTENER_NAMES="CONTROLLER",
     KAFKA_INTER_BROKER_LISTENER_NAME="INTERNAL",
     KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR="1",
     KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR="1",
     KAFKA_TRANSACTION_STATE_LOG_MIN_ISR="1",
     KAFKA_SHARE_COORDINATOR_STATE_TOPIC_REPLICATION_FACTOR="1",
     KAFKA_SHARE_COORDINATOR_STATE_TOPIC_MIN_ISR="1",
     KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS="0",
     KAFKA_AUTO_CREATE_TOPICS_ENABLE="true",
     KAFKA_HEAP_OPTS="-Xms256m -Xmx512m")}\
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
                periodSeconds: 2
                timeoutSeconds: 1
                failureThreshold: 60
"""

KAFKA_UI = f"""\
    kafka-ui:
        enabled: true
        container:
            name: kafka-ui
            image: {KAFKA_UI_IMAGE}
            imagePullPolicy: IfNotPresent
            env:
{env(KAFKA_CLUSTERS_0_NAME="road-safety-demo",
     KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS="kafka:9092",
     DYNAMIC_CONFIG_ENABLED="false",
     JAVA_OPTS="-Xmx256m")}\
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
                    path: /actuator/health
                periodSeconds: 5
                timeoutSeconds: 2
                failureThreshold: 36
        endpoints:
            - port: http
              scheme: http
              path: /
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment", default=os.environ.get("EXASOL_DEPLOYMENT", "default"))
    parser.add_argument("--no-ui", action="store_true", help="skip Kafbat UI")
    args = parser.parse_args()

    deployment = Path.home() / ".exasol/personal/deployments" / args.deployment
    added = add_sidecars(deployment / "sidecars.yaml", args.no_ui)
    install_jar(deployment / BUCKET_DIR)
    java_new = ensure_java(args.deployment)
    create_scripts(args.deployment)

    if java_new:
        print(f"Next: exasol stop -d {args.deployment} && exasol start -d {args.deployment} (activates Java"
              f"{', applies the sidecar changes' if added else ''})")
    elif added:
        print(f"Next: exasol start -d {args.deployment} (applies the sidecar changes, the database keeps running)")
    else:
        print("Kafka setup complete.")


def entry_re(name: str) -> str:
    """A sidecar entry: from its key to the next key at the same indentation, or the end."""
    return rf"^    {re.escape(name)}:[ \t]*\n(?:(?!    \S).*\n?)*"


def set_sidecars(path: Path, wanted: list[tuple[str, str]], remove: tuple[str, ...] = ()) -> bool:
    """Make sidecars.yaml hold exactly these entries (added, or replaced when they differ) and none
    of `remove`. Keeps every other entry. Writes a backup first. Returns whether anything changed."""
    text = path.read_text()
    if not re.search(r"^sidecars:\s*$", text, re.M):
        raise SystemExit(f"{path}: no top-level 'sidecars:' key, edit it by hand")
    new, changes = text, []
    for name in remove:
        if re.search(entry_re(name), new, re.M):
            new = re.sub(entry_re(name), "", new, flags=re.M)
            changes.append(f"removed {name}")
    for name, block in wanted:
        m = re.search(entry_re(name), new, re.M)
        if m is None:
            new = new.rstrip("\n") + "\n" + block
            changes.append(f"added {name}")
        elif m.group(0).rstrip("\n") != block.rstrip("\n"):
            new = new[:m.start()] + block + new[m.end():]
            changes.append(f"updated {name}")
    if not changes:
        print(f"sidecars: {', '.join(n for n, _ in wanted)} up to date")
        return False
    backup = path.with_suffix(f".yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(new)
    print(f"sidecars: {', '.join(changes)} in {path} (backup: {backup.name})")
    return True


def add_sidecars(path: Path, no_ui: bool) -> bool:
    return set_sidecars(path, [("kafka", KAFKA)] + ([] if no_ui else [("kafka-ui", KAFKA_UI)]), LEGACY)


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
        # Send the closing "/" too: without it the database drops the ";" ending the last option
        # (`%jar ...;`), and the Java UDF fails with "Error parsing script options".
        db.execute(script)
    print(f"UDF scripts: {len(scripts)} created in KAFKA_EXTENSION")

if __name__ == "__main__":
    main()
