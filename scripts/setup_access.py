#!/usr/bin/env python3
"""Set up the logins the demo needs on the Exasol Personal deployment. Safe to run again.

    .venv/bin/python scripts/setup_access.py            # after load_road_data.sh and simulate_road_safety.py setup

1. exapump profile `road-safety-demo` (user sys) in ~/.exapump/config.toml, for loading and ad-hoc SQL.
   Skipped if the profile exists.
2. Database user `dash_readonly` with SELECT on ROAD_DATA and ROAD_SAFETY only, for the dashboard.
   Its password is generated once into secrets/dash_readonly_password (mode 600) and reused afterwards.

The sys password is read from the deployment's secrets.json. No password is ever printed.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import ssl
import subprocess
import sys
from pathlib import Path

import pyexasol

ROOT = Path(__file__).resolve().parent.parent
PASSWORD_FILE = ROOT / "secrets" / "dash_readonly_password"
DSN_HOST, DSN_PORT = "127.0.0.1", 8563
SCHEMAS = ("ROAD_DATA", "ROAD_SAFETY")
DASH_USER = "DASH_READONLY"


def sys_password(deployment: str) -> str:
    path = Path.home() / ".exasol" / "personal" / "deployments" / deployment / "secrets.json"
    if not path.exists():
        sys.exit(f"error: {path} not found; install the deployment first: exasol install local -d {deployment}")
    return json.loads(path.read_text())["dbPassword"]


def exapump_profile(name: str, password: str) -> None:
    shown = subprocess.run(["exapump", "profile", "show", name], capture_output=True, text=True)
    if shown.returncode == 0:
        print(f"exapump profile '{name}': exists, left as it is")
        return
    config = Path.home() / ".exapump" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    block = (f"\n[{name}]\nhost = \"{DSN_HOST}\"\nport = {DSN_PORT}\nuser = \"sys\"\n"
             f"password = {json.dumps(password)}\ntls = true\nvalidate_certificate = false\n")
    old_umask = os.umask(0o077)
    try:
        with open(config, "a", encoding="utf-8") as f:
            f.write(block)
    finally:
        os.umask(old_umask)
    os.chmod(config, 0o600)
    print(f"exapump profile '{name}': created (user sys, {DSN_HOST}:{DSN_PORT})")


def dash_password() -> str:
    if PASSWORD_FILE.exists() and PASSWORD_FILE.read_text().strip():
        return PASSWORD_FILE.read_text().strip()
    PASSWORD_FILE.parent.mkdir(exist_ok=True)
    pw = pysecrets.token_urlsafe(24)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw)
    print(f"generated the dashboard password into {PASSWORD_FILE.relative_to(ROOT)}")
    return pw


def dash_user(password_sys: str) -> None:
    c = pyexasol.connect(dsn=f"{DSN_HOST}:{DSN_PORT}", user="sys", password=password_sys, encryption=True,
                         websocket_sslopt={"cert_reqs": ssl.CERT_NONE}, autocommit=True)
    pw = dash_password().replace('"', '""')
    exists = c.execute(f"SELECT COUNT(*) FROM EXA_DBA_USERS WHERE USER_NAME = '{DASH_USER}'").fetchval()
    c.execute(f'{"ALTER" if exists else "CREATE"} USER {DASH_USER} IDENTIFIED BY "{pw}"')
    c.execute(f"GRANT CREATE SESSION TO {DASH_USER}")
    present = {r[0] for r in c.execute("SELECT SCHEMA_NAME FROM EXA_SCHEMAS").fetchall()}
    for schema in SCHEMAS:
        if schema in present:
            c.execute(f"GRANT SELECT ON SCHEMA {schema} TO {DASH_USER}")
            print(f"{DASH_USER.lower()}: SELECT on {schema}")
        else:
            print(f"warning: schema {schema} doesn't exist yet; run this script again after creating it")
    c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deployment", default=os.environ.get("EXASOL_DEPLOYMENT", "default"), help="Exasol Personal deployment name")
    ap.add_argument("--profile", default="road-safety-demo", help="exapump profile name")
    args = ap.parse_args()
    pw = sys_password(args.deployment)
    exapump_profile(args.profile, pw)
    dash_user(pw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
