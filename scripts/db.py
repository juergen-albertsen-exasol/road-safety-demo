"""Connections and small helpers shared by the service, the loader and the simulator.

Where the database is:
- on the laptop: 127.0.0.1:8563 as `sys`, the password read from the deployment's secrets.json;
- in a sidecar: EXASOL_DB_HOST, EXASOL_DB_PORT, EXASOL_DB_USER and EXASOL_DB_PASSWORD, which the
  launcher fills from its `exasol-database` secret when it creates the container.

Where Kafka is: KAFKA_BOOTSTRAP (`kafka:9092` in a sidecar), else 127.0.0.1:19092. The database's
Java UDFs have no DNS (exasol/exasol-personal#464), so they get Kafka's IP on the deployment
network: from DNS in a sidecar, from `podman inspect` in the VM on the laptop. Kafka advertises
that IP (see scripts/setup_kafka.py), so it's all a client in the UDFs needs.

Passwords are read, never printed.
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pyexasol

BERLIN = ZoneInfo("Europe/Berlin")
DEPLOYMENTS = Path.home() / ".exasol" / "personal" / "deployments"
# The Exasol Personal deployment: the launcher's default one, unless EXASOL_DEPLOYMENT names another
DEPLOYMENT = os.environ.get("EXASOL_DEPLOYMENT", "default")
IN_SIDECAR = bool(os.environ.get("EXASOL_DB_HOST"))
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "127.0.0.1:19092")
KAFKA_INTERNAL_PORT = 9092


def connect(deployment: str, dict_rows: bool = False, compression: bool = False) -> pyexasol.ExaConnection:
    if IN_SIDECAR:
        dsn = f"{os.environ['EXASOL_DB_HOST']}:{os.environ.get('EXASOL_DB_PORT', '8563')}"
        user, password = os.environ.get("EXASOL_DB_USER", "sys"), os.environ["EXASOL_DB_PASSWORD"]
    else:
        secrets = DEPLOYMENTS / deployment / "secrets.json"
        if not secrets.exists():
            sys.exit(f"error: {secrets} not found; is the Exasol Personal deployment '{deployment}' installed?")
        dsn, user, password = "127.0.0.1:8563", "sys", json.loads(secrets.read_text())["dbPassword"]
    opts = {"fetch_dict": True, "fetch_mapper": pyexasol.exasol_mapper} if dict_rows else {}
    return pyexasol.connect(dsn=dsn, user=user, password=password, encryption=True,
                            websocket_sslopt={"cert_reqs": ssl.CERT_NONE}, compression=compression,
                            autocommit=True, **opts)


def deployment_id(deployment: str) -> str:
    return json.loads((DEPLOYMENTS / deployment / "deployment.json").read_text())["deploymentId"]


def container_name(deployment: str, sidecar: str) -> str:
    return f"exasol-db-{deployment_id(deployment)}-sidecar-{sidecar}"


def kafka_for_udfs(deployment: str) -> str:
    """host:port of Kafka as the database's UDFs reach it: its IP on the deployment network."""
    if IN_SIDECAR:
        return f"{socket.gethostbyname('kafka')}:{KAFKA_INTERNAL_PORT}"
    container = container_name(deployment, "kafka")
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    r = subprocess.run(["exasol", "shell", "host", "-d", deployment, "--", "podman", "inspect", "-f", fmt, container],
                       capture_output=True, text=True, timeout=60)
    ip = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
        raise RuntimeError(f"can't find the IP of {container}; is the kafka sidecar running? "
                           f"(exasol sidecar status kafka -d {deployment})")
    return f"{ip}:{KAFKA_INTERNAL_PORT}"


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "'"
    return "'" + str(v).replace("'", "''") + "'"


def insert(c, table: str, rows: list[tuple], chunk: int = 4000) -> None:
    for i in range(0, len(rows), chunk):
        values = ",".join("(" + ",".join(lit(v) for v in r) + ")" for r in rows[i:i + chunk])
        c.execute(f"INSERT INTO ROAD_SAFETY.{table} VALUES {values}")


def wall_now() -> datetime:
    return datetime.now(BERLIN).replace(tzinfo=None)
