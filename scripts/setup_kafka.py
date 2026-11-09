"""Add the Kafka sidecars (Redpanda and Redpanda Console) to the Exasol Personal deployment.

The launcher only enables catalog sidecars with `exasol sidecar enable`, so this script adds
user entries to the deployment's sidecars.yaml. Run `exasol start -d road-safety-demo` afterwards:
it starts the new sidecars and leaves the running database alone.

    .venv/bin/python scripts/setup_kafka.py [--deployment road-safety-demo] [--no-console]

Addresses:
- from the database (Kafka connector UDFs): redpanda:9092
- from the laptop (simulator, loader):      127.0.0.1:19092
- Redpanda Console:                         http://127.0.0.1:18080

Redpanda keeps nothing across `exasol stop`: topics start empty after each start.
Exasol is the system of record (see docs/plan.md, revision 2026-11-09).
"""

import argparse
import re
import shutil
import time
from pathlib import Path

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

    path = Path.home() / ".exasol/personal/deployments" / args.deployment / "sidecars.yaml"
    text = path.read_text()
    if not re.search(r"^sidecars:\s*$", text, re.M):
        raise SystemExit(f"{path}: no top-level 'sidecars:' key, edit it by hand")

    wanted = [("redpanda", REDPANDA)] + ([] if args.no_console else [("redpanda-console", CONSOLE)])
    missing = [(name, block) for name, block in wanted if not re.search(rf"^    {name}:\s*$", text, re.M)]
    if not missing:
        print(f"{path}: already has {', '.join(n for n, _ in wanted)}, nothing to do")
        return

    backup = path.with_suffix(f".yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(text.rstrip("\n") + "\n" + "".join(block for _, block in missing))
    print(f"{path}: added {', '.join(n for n, _ in missing)} (backup: {backup.name})")
    print(f"Next: exasol start -d {args.deployment}")


if __name__ == "__main__":
    main()
