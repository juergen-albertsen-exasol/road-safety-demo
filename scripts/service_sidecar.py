"""Run the road safety service as a sidecar of the Exasol Personal deployment.

    ./demo sidecar install [--min-providers 2] [--interval 1.5]   build the image, enable, start
    ./demo sidecar logs [-f] [--tail 50]                          what the service prints
    ./demo sidecar status
    ./demo sidecar disable | enable                               stop it / start it again

`install` builds the image from service/Containerfile in the deployment's own container engine
(on macOS: in the database VM), tagged with a hash of the code, adds the entry
`road-safety-service` to the deployment's sidecars.yaml and runs `exasol start`, which starts or
replaces the container while the database keeps running. Run it again after changing the service,
the loader or the SQL: a new hash means a new image, and `exasol start` replaces the container.

From then on the service starts and stops with the database: `exasol start` brings up Kafka,
Kafbat UI, the MCP server and the service together. The container gets the database connection
from the launcher (`secretKeyRef` to `exasol-database`); no password is written anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from db import DEPLOYMENTS, container_name  # noqa: E402
from setup_kafka import set_sidecars  # noqa: E402

NAME = "road-safety-service"
IMAGE = f"localhost/{NAME}"
CONTEXT = [ROOT / "service" / "Containerfile",
           *(ROOT / "scripts" / f for f in ("road_safety_service.py", "kafka_loader.py", "db.py", "srti_events.py")),
           *sorted((ROOT / "roadsafety").glob("*.sql"))]
VM_SHARE = Path("local/runtime/vm-shared")   # in the deployment dir; /mnt/host inside the VM
VM_MOUNT = "/mnt/host"


def entry(tag: str, extra_args: list[str]) -> str:
    def secret(env: str, key: str) -> str:
        return (f"                - name: {env}\n                  valueFrom:\n                    secretKeyRef:\n"
                f"                        name: exasol-database\n                        key: {key}\n")
    args = "".join(f"                - \"{a}\"\n" for a in extra_args)
    return (f"""\
    {NAME}:
        enabled: true
        container:
            name: {NAME}
            image: {IMAGE}:{tag}
            imagePullPolicy: Never
""" + (f"            args:\n{args}" if extra_args else "") + "            env:\n"
            + secret("EXASOL_DB_HOST", "host") + secret("EXASOL_DB_PORT", "port")
            + secret("EXASOL_DB_USER", "username") + secret("EXASOL_DB_PASSWORD", "password")
            + """\
                - name: KAFKA_BOOTSTRAP
                  value: "kafka:9092"
            restartPolicy: Always
            readinessProbe:
                exec:
                    command:
                        - sh
                        - -c
                        - find /tmp/road-safety-service.alive -mmin -1 | grep -q .
                periodSeconds: 5
                timeoutSeconds: 2
                failureThreshold: 24
""")


def host(deployment: str, *cmd: str, capture: bool = False) -> subprocess.CompletedProcess:
    """A command on the machine that runs the containers (the VM on macOS)."""
    return subprocess.run(["exasol", "shell", "host", "-d", deployment, "--", *cmd],
                          capture_output=capture, text=True)


def code_hash() -> str:
    h = hashlib.sha256()
    for f in CONTEXT:
        h.update(str(f.relative_to(ROOT)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:12]


def laptop_services() -> list[str]:
    r = subprocess.run(["pgrep", "-fl", "road_safety_service.py"], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.strip()]


def install(args) -> None:
    dep_dir = DEPLOYMENTS / args.deployment
    tag = code_hash()
    # 1. Build context in the folder the VM shares with the laptop
    share = dep_dir / VM_SHARE
    on_vm = share.is_dir()
    ctx = (share if on_vm else dep_dir) / "build" / NAME
    shutil.rmtree(ctx, ignore_errors=True)
    for f in CONTEXT:
        target = ctx / ("Containerfile" if f.name == "Containerfile" else f.relative_to(ROOT))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    ctx_in_engine = f"{VM_MOUNT}/build/{NAME}" if on_vm else str(ctx)
    # 2. Build the image unless this code is already built
    have = host(args.deployment, "podman", "image", "exists", f"{IMAGE}:{tag}", capture=True).returncode == 0
    if have:
        print(f"image: {IMAGE}:{tag} already built")
    else:
        print(f"image: building {IMAGE}:{tag} (the first build pulls Python and installs the packages)", flush=True)
        r = host(args.deployment, "podman", "build", "--quiet", "-t", f"{IMAGE}:{tag}",
                 "-f", f"{ctx_in_engine}/Containerfile", ctx_in_engine)
        if r.returncode:
            sys.exit("image: build failed (output above)")
    shutil.rmtree(ctx, ignore_errors=True)
    # 3. The sidecar entry, then let the launcher apply it
    running = laptop_services()
    if running:
        sys.exit("The service is still running on this laptop; stop it first (Ctrl-C in its terminal), "
                 "or both would publish every warning:\n  " + "\n  ".join(running))
    extra = []
    if args.min_providers is not None:
        extra += ["--min-providers", str(args.min_providers)]
    if args.interval is not None:
        extra += ["--interval", str(args.interval)]
    set_sidecars(dep_dir / "sidecars.yaml", [(NAME, entry(tag, extra))])
    print(f"exasol start -d {args.deployment} (the database keeps running) ...", flush=True)
    if subprocess.run(["exasol", "start", "-d", args.deployment]).returncode:
        sys.exit(f"exasol start failed; see `exasol sidecar status {NAME} -d {args.deployment}`")
    # 4. Old images of the service are no longer needed
    r = host(args.deployment, "podman", "images", "--format", "{{.Repository}}:{{.Tag}}", IMAGE, capture=True)
    for old in r.stdout.split():
        if old.startswith(IMAGE + ":") and old != f"{IMAGE}:{tag}":
            host(args.deployment, "podman", "rmi", old, capture=True)
    print(f"Done. Follow the service with: ./demo sidecar logs -f")


def logs(args) -> None:
    cmd = ["podman", "logs", "--tail", str(args.tail)] + (["-f"] if args.follow else [])
    try:
        host(args.deployment, *cmd, container_name(args.deployment, NAME))
    except KeyboardInterrupt:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--deployment", default="road-safety-demo")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="build the image, add the sidecar, exasol start")
    i.add_argument("--min-providers", type=int, help="different providers needed (service default 2)")
    i.add_argument("--interval", type=float, help="seconds between rounds (service default 1.5)")
    lg = sub.add_parser("logs", help="what the service prints")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("--tail", type=int, default=50)
    for name in ("status", "disable", "enable"):
        sub.add_parser(name)
    args = p.parse_args()
    if args.cmd == "install":
        install(args)
    elif args.cmd == "logs":
        logs(args)
    else:
        sys.exit(subprocess.run(["exasol", "sidecar", args.cmd, NAME, "-d", args.deployment]).returncode)


if __name__ == "__main__":
    main()
