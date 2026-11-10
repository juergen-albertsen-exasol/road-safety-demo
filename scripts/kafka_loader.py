"""Load Kafka topics into Exasol with the Exasol Kafka connector extension, every few seconds.

    .venv/bin/python scripts/kafka_loader.py [--interval 3] [--once] [--prefix srti. --prefix datex.]

The database pulls the messages itself (Java UDFs in KAFKA_EXTENSION, see roadsafety/kafka_extension.sql);
this script only decides when and what to import. Every message lands once in
ROAD_SAFETY.INBOUND_MESSAGES (topic, topic id, partition, offset, Kafka timestamp, JSON payload).

How positions survive restarts. The connector resumes from the highest KAFKA_OFFSET per partition
in its target table, read across the whole table. So each topic gets its own staging table in
KAFKA_STAGE, which only ever holds the current topic. Kafka keeps nothing across
`exasol stop`: topics come back empty, with a new topic id and offsets from 0. When a topic's id
differs from the one stored in KAFKA_STAGE.TOPICS, its staging table is emptied, so the import
starts at 0 again. Everything already loaded is in INBOUND_MESSAGES.

Addresses. The UDF sandbox has no DNS (exasol/exasol-personal#464), so the imports name Kafka by
its IP on the deployment network, looked up again whenever an import fails (the IP changes when
the sidecar is recreated). Kafka advertises that IP too (scripts/setup_kafka.py). This script
itself reaches Kafka at KAFKA_BOOTSTRAP: 127.0.0.1:19092 on the laptop, kafka:9092 in a sidecar.

Runs on the laptop or inside the service sidecar; see scripts/db.py for the database connection.
The password is read, never printed.
"""

from __future__ import annotations

import argparse
import queue
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pyexasol
from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition

from db import KAFKA_BOOTSTRAP, connect, kafka_for_udfs

GROUP_ID = "exasol-road-safety"

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


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def stage_table(topic: str) -> str:
    return "KAFKA_STAGE." + re.sub(r"[^A-Z0-9]+", "_", topic.upper()).strip("_")


class Loader:
    def __init__(self, args):
        self.args = args
        self.db = connect(args.deployment)
        for sql in SETUP_SQL:
            self.db.execute(sql)
        self.broker_udf = kafka_for_udfs(args.deployment)
        self.admin = KafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP, client_id="road-safety-loader")
        self.consumer = KafkaConsumer(bootstrap_servers=KAFKA_BOOTSTRAP, enable_auto_commit=False)
        self.pool = ThreadPoolExecutor(max_workers=args.parallel)
        self.conns: queue.Queue = queue.Queue()     # each import borrows one: a connection serves one thread at a time
        for _ in range(args.parallel):
            self.conns.put(connect(args.deployment))
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
    def import_topic(self, topic: str) -> tuple[str, float]:
        """Runs in parallel, on a connection of its own. Writes only the topic's own staging table."""
        conn = self.conns.get()
        try:
            return self._import(conn, topic)
        finally:
            self.conns.put(conn)

    def _import(self, conn, topic: str) -> tuple[str, float]:
        table = stage_table(topic)
        t0 = time.time()
        conn.execute(f"""IMPORT INTO {table} FROM SCRIPT KAFKA_EXTENSION.KAFKA_CONSUMER WITH
            BOOTSTRAP_SERVERS = {q(self.broker_udf)} TOPIC_NAME = {q(topic)} TABLE_NAME = {q(table)}
            GROUP_ID = {q(GROUP_ID)} RECORD_VALUE_FORMAT = 'string' RECORD_FIELDS = 'value,timestamp'
            POLL_TIMEOUT_MS = '1000' CONSUME_ALL_OFFSETS = 'true'""")
        return topic, time.time() - t0

    def copy(self, topic: str, topic_id: str) -> int:
        """Serially, on the main connection: move what's new into INBOUND_MESSAGES, then keep only the last
        row per partition (all the connector needs to resume). Parallel sessions reading and writing
        INBOUND_MESSAGES at once would make Exasol wait or roll back."""
        table = stage_table(topic)
        n = self.db.execute(f"""INSERT INTO ROAD_SAFETY.INBOUND_MESSAGES
            SELECT {q(topic)}, {q(topic_id)}, s.KAFKA_PARTITION, s.KAFKA_OFFSET, s.KAFKA_TS, CURRENT_TIMESTAMP, s.PAYLOAD
            FROM {table} s
            LEFT JOIN (SELECT "kafka_partition" p, MAX("kafka_offset") o FROM ROAD_SAFETY.INBOUND_MESSAGES
                       WHERE "topic_id" = {q(topic_id)} GROUP BY 1) m ON m.p = s.KAFKA_PARTITION
            WHERE s.KAFKA_OFFSET > COALESCE(m.o, -1)""").rowcount()
        self.db.execute(f"""DELETE FROM {table} s WHERE s.KAFKA_OFFSET <
            (SELECT MAX(x.KAFKA_OFFSET) FROM {table} x WHERE x.KAFKA_PARTITION = s.KAFKA_PARTITION)""")
        return n

    def cycle(self) -> None:
        topics = self.topics()
        for t, tid in topics.items():
            self.sync_topic(t, tid)
        todo = {t: n for t, n in self.lag(list(topics)).items() if n > 0}
        if not todo:
            return
        futures = [self.pool.submit(self.import_topic, t) for t in todo]
        failed = False
        for f in futures:
            try:
                topic, secs = f.result()
            except pyexasol.ExaQueryError as e:
                print(f"{now()} import failed: {one_line(getattr(e, 'message', '') or str(e))}", flush=True)
                failed = True
                continue
            n = self.copy(topic, topics[topic])
            if getattr(self.args, "verbose", True):
                print(f"{now()} {topic}: +{n} messages ({secs:.1f} s)", flush=True)
        if failed:                         # Kafka may have a new IP after a restart of its sidecar
            ip = kafka_for_udfs(self.args.deployment)
            if ip != self.broker_udf:
                print(f"{now()} kafka moved to {ip} (was {self.broker_udf})", flush=True)
                self.broker_udf = ip

    def run(self) -> None:
        while True:
            t0 = time.time()
            self.cycle()
            if self.args.once:
                return
            time.sleep(max(0.2, self.args.interval - (time.time() - t0)))


def one_line(text: str) -> str:
    return " ".join(str(text).split())[:300]


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
    # Survive `exasol stop`/`start`: on any failure wait and reconnect to both.
    while True:
        try:
            loader = Loader(args)
            print(f"{now()} connected (kafka at {loader.broker_udf} for the UDFs)", flush=True)
            loader.run()
            return
        except KeyboardInterrupt:
            return
        except (SystemExit, Exception) as e:  # noqa: BLE001 - the loader must keep going
            if args.once:
                raise
            print(f"{now()} waiting for the database and Kafka: {type(e).__name__}: {one_line(e)}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
