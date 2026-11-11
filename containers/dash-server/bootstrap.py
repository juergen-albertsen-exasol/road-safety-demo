"""Bootstrap of the road safety demo's Dash Server sidecar, called by start.sh.

    bootstrap.py prepare <file>   give DASH_READONLY a new random password, write it to <file>
    bootstrap.py deploy           when the demo's schemas exist: grant SELECT on them to DASH_READONLY,
                                  then deploy dashboard v2 (munich-road-conditions) over MCP

The admin connection comes from EXASOL_DB_HOST/PORT/USER/PASSWORD (the launcher's exasol-database
secret). Passwords are never printed.
"""
from __future__ import annotations

import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pyexasol

USER = "DASH_READONLY"
SCHEMAS = ("ROAD_DATA", "ROAD_SAFETY")
# What dashboard v2 reads; the service creates INCIDENTS and the rest when it first starts.
NEEDED = [("ROAD_DATA", "ROAD_SEGMENTS"), ("ROAD_SAFETY", "SIM_STATE"), ("ROAD_SAFETY", "V_INCIDENTS_LIVE"),
          ("ROAD_SAFETY", "INCIDENTS"), ("ROAD_SAFETY", "OUTBOUND_MESSAGES"), ("ROAD_SAFETY", "WARNING_RECEIPTS")]
APP = "munich-road-conditions"
TITLE = "Munich Road Conditions"
DASHBOARD = Path("/opt/road-safety/dashboard")
MCP = f"http://127.0.0.1:{os.environ.get('DASH_SERVER_PORT', '5100')}/mcp"


def log(msg: str) -> None:
    print(f"[road-safety bootstrap] {msg}", flush=True)


def connect(retry_for: float = 600):
    """Admin connection; the database may still be starting when the sidecar starts."""
    deadline = time.time() + retry_for
    while True:
        try:
            return pyexasol.connect(
                dsn=f"{os.environ['EXASOL_DB_HOST']}:{os.environ['EXASOL_DB_PORT']}",
                user=os.environ["EXASOL_DB_USER"], password=os.environ["EXASOL_DB_PASSWORD"],
                encryption=True, websocket_sslopt={"cert_reqs": ssl.CERT_NONE}, autocommit=True)
        except Exception as e:  # noqa: BLE001
            if time.time() > deadline:
                raise
            log(f"database not reachable yet ({type(e).__name__}), retrying in 10 s")
            time.sleep(10)


def prepare(target: str) -> None:
    c = connect()
    pw = secrets.token_urlsafe(24)
    exists = c.execute("SELECT COUNT(*) FROM EXA_DBA_USERS WHERE USER_NAME = {u}", {"u": USER}).fetchval()
    c.execute(f'{"ALTER" if exists else "CREATE"} USER {USER} IDENTIFIED BY "{pw}"')
    c.execute(f"GRANT CREATE SESSION TO {USER}")
    c.close()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw)
    log(f"{USER}: {'new password' if exists else 'created'} (read-only user of the dashboards)")


def missing(c) -> list[str]:
    have = {(s, n) for s, n in c.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM SYS.EXA_ALL_TABLES UNION ALL "
        "SELECT VIEW_SCHEMA, VIEW_NAME FROM SYS.EXA_ALL_VIEWS")}
    return [f"{s}.{n}" for s, n in NEEDED if (s, n) not in have]


def mcp(tool: str, arguments: dict, timeout: float = 600) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()
    req = urllib.request.Request(MCP, body, {"Content-Type": "application/json",
                                             "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        msg = json.loads(r.read())
    if "error" in msg:
        raise RuntimeError(f"{tool}: {msg['error']}")
    res = msg["result"]
    data = res.get("structuredContent")
    if data is None:
        text = "".join(p.get("text", "") for p in res.get("content", []))
        data = json.loads(text) if text.strip().startswith("{") else {"text": text}
    if res.get("isError"):
        raise RuntimeError(f"{tool}: {json.dumps(data)[:500]}")
    return data


def wait_for_server(timeout: float = 300) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            mcp("apps_list", {}, timeout=10)
            return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if time.time() > deadline:
                raise
            time.sleep(2)


def deploy() -> None:
    c = connect()
    waited = 0
    while gaps := missing(c):
        if waited % 60 == 0:
            log(f"waiting for the demo's tables before deploying {APP}: {', '.join(gaps)} (run ./setup.sh)")
        time.sleep(10)
        waited += 10
    for schema in SCHEMAS:
        c.execute(f"GRANT SELECT ON SCHEMA {schema} TO {USER}")
    c.close()
    log(f"{USER}: SELECT on {', '.join(SCHEMAS)}")

    wait_for_server()
    apps = mcp("apps_list", {}).get("apps", [])
    if any(a.get("name") == APP and a.get("mounted") for a in apps):
        log(f"{APP}: already deployed")
        return
    files = [{"path": str(p.relative_to(DASHBOARD)), "content": p.read_text()}
             for p in sorted(DASHBOARD.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
    if not any(a.get("name") == APP for a in apps):
        mcp("app_create_from_files", {"name": APP, "title": TITLE, "template": "exasol-analytics",
                                      "start_immediately": False, "files": files})
    else:
        mcp("app_put_files", {"name": APP, "files": files})
    t0 = time.time()
    result = mcp("app_deploy_draft", {"name": APP, "deployment_target": "live",
                                      "auto_rollback_on_health_failure": True})
    health = (result.get("health") or {}).get("health", {}).get("status")
    log(f"{APP}: deployed in {time.time() - t0:.0f} s, health {health}: "
        f"http://127.0.0.1:{os.environ.get('DASH_SERVER_PORT', '5100')}/apps/{APP}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["prepare"] and len(sys.argv) == 3:
        prepare(sys.argv[2])
    elif sys.argv[1:] == ["deploy"]:
        try:
            deploy()
        except Exception as e:  # noqa: BLE001 - keep Dash Server running; the log says what failed
            log(f"deploying {APP} failed: {e}")
            sys.exit(1)
    else:
        sys.exit(__doc__)
