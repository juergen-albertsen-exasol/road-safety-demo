"""Load Kafka topics into Exasol with the Exasol Kafka connector extension, every few seconds.

    .venv/bin/python scripts/kafka_loader.py [--interval 3] [--once] [--prefix srti. --prefix datex.]

The database pulls the messages itself (Java UDFs in KAFKA_EXTENSION, see roadsafety/kafka_extension.sql);
this script only decides when and what to import. Every message lands once in
ROAD_SAFETY.INBOUND_MESSAGES (topic, topic id, partition, offset, Kafka timestamp, JSON payload).

How positions survive restarts. The connector resumes from the highest KAFKA_OFFSET per partition
in its target table, read across the whole table. So each topic gets its own staging table in
KAFKA_STAGE, which only ever holds the current topic. Redpanda keeps nothing across
`exasol stop`: topics come back empty, with a new topic id and offsets from 0. When a topic's id
differs from the one stored in KAFKA_STAGE.TOPICS, its staging table is emptied, so the import
starts at 0 again. Everything already loaded is in INBOUND_MESSAGES.

Name resolution. The UDF sandbox has no DNS, so the Java UDFs read `redpanda` from a hosts file
in BucketFS (`jdk.net.hosts.file`). This script writes it with Redpanda's current IP on the
deployment network, at start and whenever an import can't resolve the name.

The sys password is read from the deployment's secrets.json and never printed.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pyexasol
from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition

BROKER_LAPTOP = "127.0.0.1:19092"
BROKER_DATABASE = "redpanda:9092"
GROUP_ID = "exasol-road-safety"
DEPLOYMENTS = Path.home() / ".exasol" / "personal" / "deployments"
HOSTS_FILE_IN_BUCKET = "bucketfs/bfsdefault/default/kafka/hosts"

SETUP_SQL = [
    "CREATE SCHEMA IF NOT EXISTS KAFKA_STAGE",
    """CREATE TABLE IF NOT EXISTS KAFKA_STAGE.TOPICS (
        "topic" VARCHAR(200) NOT NULL, "topic_id" VARCHAR(36) NOT NULL,
        "stage_table" VARCHAR(128) NOT NULL, "since" TIMESTAMP NOT NULL)""",
    "CREATE SCHEMA IF NOT EXISTS ROAD_SAFETY",
    """CREATE TABLE IF NOT EXISTS ROAD_SAFETY.INBOUND_MESSAGES (
        "topic" VARCHAR(200) NOT NULL, "topic_id" VARCHAR(36) NOT NULL,
        "kafka_partition" DECIMAL(18,0) NOT NULL, "kafka_offset" DECIMAL(36,0) NOT NULL,
        "kafka_ts" TIMESTAMP, "loaded_at" TIMESTAMP NOT NULL,
        "payload" VARCHAR(2000000) UTF8)""",
]


def connect(deployment: str) -> pyexasol.ExaConnection:
    secrets = DEPLOYMENTS / deployment / "secrets.json"
    if not secrets.exists():
        sys.exit(f"error: {secrets} not found; is the Exasol Personal deployment '{deployment}' installed?")
    password = json.loads(secrets.read_text())["dbPassword"]
    return pyexasol.connect(dsn="127.0.0.1:8563", user="sys", password=password, encryption=True,
                            websocket_sslopt={"cert_reqs": ssl.CERT_NONE}, autocommit=True)


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def stage_table(topic: str) -> str:
    return "KAFKA_STAGE." + re.sub(r"[^A-Z0-9]+", "_", topic.upper()).strip("_")


def write_hosts_file(deployment: str) -> str:
    """Look up Redpanda's IP on the deployment network and write the hosts file the Java UDFs read."""
    dep_id = json.loads((DEPLOYMENTS / deployment / "deployment.json").read_text())["deploymentId"]
    container = f"exasol-db-{dep_id}-sidecar-redpanda"
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    for engine in ("podman", "docker"):
        r = subprocess.run(["exasol", "shell", "host", "-d", deployment, "--", engine, "inspect", "-f", fmt, container],
                           capture_output=True, text=True, timeout=60)
        ip = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            break
    else:
        sys.exit(f"error: can't find the IP of {container}; is the redpanda sidecar running? "
                 f"(exasol sidecar status redpanda -d {deployment})")
    path = DEPLOYMENTS / deployment / "local/runtime/exa" / HOSTS_FILE_IN_BUCKET
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{ip} redpanda\n")
    return ip


class Loader:
    def __init__(self, args):
        self.args = args
        self.db = connect(args.deployment)
        for sql in SETUP_SQL:
            self.db.execute(sql)
        self.admin = KafkaAdminClient(bootstrap_servers=BROKER_LAPTOP, client_id="road-safety-loader")
        self.consumer = KafkaConsumer(bootstrap_servers=BROKER_LAPTOP, enable_auto_commit=False)
        self.pool = ThreadPoolExecutor(max_workers=args.parallel)
        self.conns = [connect(args.deployment) for _ in range(args.parallel)]
        self.known: dict[str, str] = {r[0]: r[1] for r in self.db.execute(
            'SELECT "topic", "topic_id" FROM KAFKA_STAGE.TOPICS').fetchall()}

    # ------------------------------------------------------------------ topics
    def topics(self) -> dict[str, str]:
        names = sorted(t for t in self.admin.list_topics()
                       if t.startswith(tuple(self.args.prefix)) and t not in self.args.exclude)
        if not names:
            return {}
        return {d["name"]: d["topic_id"] for d in self.admin.describe_topics(names) if d["error_code"] == 0}

    def sync_topic(self, topic: str, topic_id: str) -> None:
        """Create the staging table; empty it when the topic was recreated (new id)."""
        if self.known.get(topic) == topic_id:
            return
        table = stage_table(topic)
        self.db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            PAYLOAD VARCHAR(2000000) UTF8, KAFKA_TS TIMESTAMP,
            KAFKA_PARTITION DECIMAL(18,0), KAFKA_OFFSET DECIMAL(36,0))""")
        self.db.execute(f"TRUNCATE TABLE {table}")
        self.db.execute(f'DELETE FROM KAFKA_STAGE.TOPICS WHERE "topic" = {q(topic)}')
        self.db.execute(f"INSERT INTO KAFKA_STAGE.TOPICS VALUES ({q(topic)}, {q(topic_id)}, {q(table)}, CURRENT_TIMESTAMP)")
        if topic in self.known:
            print(f"{now()} {topic}: recreated (new topic id), loading from offset 0 again", flush=True)
        self.known[topic] = topic_id

    def lag(self, topics: list[str]) -> dict[str, int]:
        """Messages per topic not yet in its staging table (end offsets from Kafka, positions from Exasol)."""
        parts = [TopicPartition(t, p) for t in topics for p in (self.consumer.partitions_for_topic(t) or ())]
        if not parts:
            return {}
        ends = self.consumer.end_offsets(parts)
        loaded: dict[tuple[str, int], int] = {}
        for t in topics:
            for p, o in self.db.execute(
                    f"SELECT KAFKA_PARTITION, MAX(KAFKA_OFFSET) FROM {stage_table(t)} GROUP BY 1").fetchall():
                loaded[(t, int(p))] = int(o)
        out: dict[str, int] = {}
        for tp, end in ends.items():
            out[tp.topic] = out.get(tp.topic, 0) + max(0, end - 1 - loaded.get((tp.topic, tp.partition), -1))
        return out

    # ------------------------------------------------------------------ import
    def import_topic(self, i: int, topic: str, topic_id: str) -> tuple[str, int, float]:
        conn = self.conns[i % len(self.conns)]
        table = stage_table(topic)
        t0 = time.time()
        conn.execute(f"""IMPORT INTO {table} FROM SCRIPT KAFKA_EXTENSION.KAFKA_CONSUMER WITH
            BOOTSTRAP_SERVERS = {q(BROKER_DATABASE)} TOPIC_NAME = {q(topic)} TABLE_NAME = {q(table)}
            GROUP_ID = {q(GROUP_ID)} RECORD_VALUE_FORMAT = 'string' RECORD_FIELDS = 'value,timestamp'
            POLL_TIMEOUT_MS = '1000' CONSUME_ALL_OFFSETS = 'true'""")
        # Copy what's new into the permanent table, then keep only the last row per partition
        # (all the connector needs to resume).
        n = conn.execute(f"""INSERT INTO ROAD_SAFETY.INBOUND_MESSAGES
            SELECT {q(topic)}, {q(topic_id)}, s.KAFKA_PARTITION, s.KAFKA_OFFSET, s.KAFKA_TS, CURRENT_TIMESTAMP, s.PAYLOAD
            FROM {table} s
            LEFT JOIN (SELECT "kafka_partition" p, MAX("kafka_offset") o FROM ROAD_SAFETY.INBOUND_MESSAGES
                       WHERE "topic_id" = {q(topic_id)} GROUP BY 1) m ON m.p = s.KAFKA_PARTITION
            WHERE s.KAFKA_OFFSET > COALESCE(m.o, -1)""").rowcount()
        conn.execute(f"""DELETE FROM {table} s WHERE s.KAFKA_OFFSET <
            (SELECT MAX(x.KAFKA_OFFSET) FROM {table} x WHERE x.KAFKA_PARTITION = s.KAFKA_PARTITION)""")
        return topic, n, time.time() - t0

    def cycle(self) -> None:
        topics = self.topics()
        for t, tid in topics.items():
            self.sync_topic(t, tid)
        todo = {t: n for t, n in self.lag(list(topics)).items() if n > 0}
        if not todo:
            return
        futures = [self.pool.submit(self.import_topic, i, t, topics[t]) for i, t in enumerate(todo)]
        for f in futures:
            try:
                topic, n, secs = f.result()
                print(f"{now()} {topic}: +{n} messages ({secs:.1f} s)", flush=True)
            except pyexasol.ExaQueryError as e:
                msg = str(e)
                if "resolvable" in msg or "UnknownHost" in msg:
                    ip = write_hosts_file(self.args.deployment)
                    print(f"{now()} redpanda not resolvable in the UDFs, hosts file rewritten ({ip})", flush=True)
                else:
                    print(f"{now()} import failed: {msg.splitlines()[0][:300]}", flush=True)

    def run(self) -> None:
        while True:
            t0 = time.time()
            self.cycle()
            if self.args.once:
                return
            time.sleep(max(0.2, self.args.interval - (time.time() - t0)))


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--deployment", default="road-safety-demo")
    p.add_argument("--interval", type=float, default=3.0, help="seconds between import rounds (default 3)")
    p.add_argument("--parallel", type=int, default=4, help="topics imported at the same time (default 4)")
    p.add_argument("--prefix", action="append", help="topic prefixes to load (default: srti. and datex.)")
    p.add_argument("--exclude", action="append", default=[], help="topics to skip")
    p.add_argument("--once", action="store_true", help="one import round, then exit")
    args = p.parse_args()
    args.prefix = args.prefix or ["srti.", "datex."]
    args.exclude = set(args.exclude) | {"srti.confirmed"}  # our own outbound topic
    print(f"{now()} loading topics {', '.join(a + '*' for a in args.prefix)} every {args.interval:g} s", flush=True)
    # Survive `exasol stop`/`start`: on any failure wait, look up Redpanda's IP again (it can change
    # when the sidecar is recreated) and reconnect to both.
    while True:
        try:
            ip = write_hosts_file(args.deployment)
            loader = Loader(args)
            print(f"{now()} connected (redpanda at {ip} for the UDFs)", flush=True)
            loader.run()
            return
        except KeyboardInterrupt:
            return
        except (SystemExit, Exception) as e:  # noqa: BLE001 - the loader must keep going
            if args.once:
                raise
            print(f"{now()} waiting for the database and Redpanda: {str(e).splitlines()[0][:200] if str(e) else type(e).__name__}",
                  flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
