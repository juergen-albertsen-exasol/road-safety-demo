#!/usr/bin/env bash
# Set up the road safety demo on Exasol Personal. Safe to run again: every step checks first and
# only does what is missing, so it also repairs a half-finished setup.
#
#   ./setup.sh                  everything
#   ./setup.sh --data <zip>     use a local copy of munich-road-data.zip instead of downloading it
#
# Needs only the Exasol Personal launcher (`exasol`, https://github.com/exasol/exasol-personal; installer:
# curl https://downloads.exasol.com/exasol-personal/installer.sh | sh) and
# what macOS and Linux bring along (bash, curl). Everything else runs as sidecars of the database:
#   kafka, kafka-ui           Apache Kafka and Kafbat UI (http://127.0.0.1:18080)
#   mcp                       the database's MCP server for your AI agent (http://127.0.0.1:4896/mcp)
#   road-safety-service       loads the partners' messages, combines them in SQL, publishes warnings
#   road-safety-tools         runs the simulator and the setup steps (./demo sim ...)
#   dash-server               the dashboards and their MCP server (http://127.0.0.1:5100)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"

DATA_ZIP=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --data) DATA_ZIP="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see ./setup.sh --help)" ;;
    esac
done

CONNECTOR_VERSION="2.0.1"
CONNECTOR_JAR="exasol-kafka-connector-extension-$CONNECTOR_VERSION.jar"
CONNECTOR_URL="https://github.com/exasol/kafka-connector-extension/releases/download/$CONNECTOR_VERSION/$CONNECTOR_JAR"
BUCKET_DIR="$DEP_DIR/local/runtime/exa/bucketfs/bfsdefault/default/kafka"   # /buckets/bfsdefault/default/kafka in SQL

say "${BOLD}Road safety demo $VERSION on Exasol Personal$RESET (deployment '$DEPLOYMENT')"

# ---------------------------------------------------------------------------------------------
step "1/8 Exasol Personal launcher"
if ! command -v exasol >/dev/null; then
    die "the Exasol Personal launcher 'exasol' is not installed. Install it with
         curl https://downloads.exasol.com/exasol-personal/installer.sh | sh
       then run ./setup.sh again."
fi
ok "exasol $(exasol version 2>/dev/null | head -1)"
command -v curl >/dev/null || die "curl is missing"

# ---------------------------------------------------------------------------------------------
step "2/8 Database"
if [[ ! -f "$DEP_DIR/deployment.json" ]]; then
    info "No deployment '$DEPLOYMENT' yet: installing Exasol Personal locally."
    info "This takes several minutes and about 10 GB of disk space; the launcher asks you to accept its EULA."
    exasol install local
fi
db_status() { exa status --json 2>/dev/null | sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1; }
if [[ "$(db_status)" != "database_ready" ]]; then
    info "Starting the database ..."
    exa start >/dev/null
fi
[[ "$(db_status)" == "database_ready" ]] || die "the database is not ready: exasol status -d $DEPLOYMENT"
ok "database running (127.0.0.1:8563)"

# ---------------------------------------------------------------------------------------------
step "3/8 MCP server for your AI agent"
if exa mcp status --json 2>/dev/null | grep -q '"health"[[:space:]]*:[[:space:]]*"serving"'; then
    ok "serving on http://127.0.0.1:4896/mcp"
else
    exa mcp enable >/dev/null
    ok "enabled: http://127.0.0.1:4896/mcp"
fi

# ---------------------------------------------------------------------------------------------
step "4/8 Java for the Kafka connector"
RESTART=""
if exa slc list 2>/dev/null | awk '$1 ~ /^JAVA/ {print $NF}' | grep -qx yes; then
    ok "Java script language container installed"
else
    exa slc install java --no-restart >/dev/null
    RESTART=yes
    ok "Java script language container added (active after a restart, below)"
fi
if [[ -f "$BUCKET_DIR/$CONNECTOR_JAR" ]]; then
    ok "Kafka connector $CONNECTOR_VERSION in BucketFS"
else
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/$CONNECTOR_JAR" "$CONNECTOR_URL"
    expected="$(curl -fsSL "$CONNECTOR_URL.sha256" | awk '{print $1}')"
    if command -v sha256sum >/dev/null; then actual="$(sha256sum "$tmp/$CONNECTOR_JAR" | awk '{print $1}')"
    else actual="$(shasum -a 256 "$tmp/$CONNECTOR_JAR" | awk '{print $1}')"; fi
    [[ -n "$expected" && "$actual" == "$expected" ]] || die "checksum mismatch for $CONNECTOR_JAR"
    mkdir -p "$BUCKET_DIR"
    cp "$tmp/$CONNECTOR_JAR" "$BUCKET_DIR/"
    rm -rf "$tmp"
    ok "Kafka connector $CONNECTOR_VERSION downloaded, checksum OK, copied to BucketFS"
fi

# ---------------------------------------------------------------------------------------------
step "5/8 Sidecars"
blocks="$(mktemp -d)"
db_env() {   # the database connection, filled in by the launcher; never written to a file
    local key
    for key in "EXASOL_DB_HOST host" "EXASOL_DB_PORT port" "EXASOL_DB_USER username" "EXASOL_DB_PASSWORD password"; do
        set -- $key
        printf '                - name: %s\n                  valueFrom:\n                    secretKeyRef:\n                        name: exasol-database\n                        key: %s\n' "$1" "$2"
    done
}

# Kafka advertises its own IP on the deployment network, not the name `kafka`: the database's UDFs
# have no DNS (exasol/exasol-personal#464). `set --` instead of `$(...)`: the launcher expands $(NAME).
cat > "$blocks/kafka" <<'EOF'
    kafka:
        enabled: true
        container:
            name: kafka
            image: docker.io/apache/kafka:4.4.0
            imagePullPolicy: IfNotPresent
            command:
                - sh
                - -c
                - 'set -- `hostname -i`; export KAFKA_ADVERTISED_LISTENERS="INTERNAL://$1:9092,EXTERNAL://127.0.0.1:19092"; exec /__cacert_entrypoint.sh /etc/kafka/docker/run'
            env:
                - name: CLUSTER_ID
                  value: "lCta9CuTTLqZ1cPFipnIlw"
                - name: KAFKA_NODE_ID
                  value: "1"
                - name: KAFKA_PROCESS_ROLES
                  value: "broker,controller"
                - name: KAFKA_CONTROLLER_QUORUM_VOTERS
                  value: "1@localhost:9093"
                - name: KAFKA_LISTENERS
                  value: "INTERNAL://0.0.0.0:9092,EXTERNAL://0.0.0.0:19092,CONTROLLER://localhost:9093"
                - name: KAFKA_LISTENER_SECURITY_PROTOCOL_MAP
                  value: "INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT"
                - name: KAFKA_CONTROLLER_LISTENER_NAMES
                  value: "CONTROLLER"
                - name: KAFKA_INTER_BROKER_LISTENER_NAME
                  value: "INTERNAL"
                - name: KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR
                  value: "1"
                - name: KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR
                  value: "1"
                - name: KAFKA_TRANSACTION_STATE_LOG_MIN_ISR
                  value: "1"
                - name: KAFKA_SHARE_COORDINATOR_STATE_TOPIC_REPLICATION_FACTOR
                  value: "1"
                - name: KAFKA_SHARE_COORDINATOR_STATE_TOPIC_MIN_ISR
                  value: "1"
                - name: KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS
                  value: "0"
                - name: KAFKA_AUTO_CREATE_TOPICS_ENABLE
                  value: "true"
                - name: KAFKA_HEAP_OPTS
                  value: "-Xms256m -Xmx512m"
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
EOF

cat > "$blocks/kafka-ui" <<'EOF'
    kafka-ui:
        enabled: true
        container:
            name: kafka-ui
            image: docker.io/kafbat/kafka-ui:v1.5.0
            imagePullPolicy: IfNotPresent
            env:
                - name: KAFKA_CLUSTERS_0_NAME
                  value: "road-safety-demo"
                - name: KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS
                  value: "kafka:9092"
                - name: DYNAMIC_CONFIG_ENABLED
                  value: "false"
                - name: JAVA_OPTS
                  value: "-Xmx256m"
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
EOF

{
cat <<EOF
    road-safety-service:
        enabled: true
        container:
            name: road-safety-service
            image: $IMAGE_ROAD_SAFETY
            imagePullPolicy: IfNotPresent
            env:
EOF
db_env
cat <<'EOF'
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
EOF
} > "$blocks/road-safety-service"

{
cat <<EOF
    road-safety-tools:
        enabled: true
        container:
            name: road-safety-tools
            image: $IMAGE_ROAD_SAFETY
            imagePullPolicy: IfNotPresent
            command:
                - sleep
                - infinity
            env:
EOF
db_env
cat <<'EOF'
            restartPolicy: Always
EOF
} > "$blocks/road-safety-tools"

{
cat <<EOF
    dash-server:
        enabled: true
        container:
            name: dash-server
            image: $IMAGE_DASH_SERVER
            imagePullPolicy: IfNotPresent
            env:
EOF
db_env
cat <<'EOF'
            ports:
                - name: http
                  containerPort: 5100
                  hostPort: 5100
                  hostIP: 127.0.0.1
                  protocol: TCP
            restartPolicy: Always
            readinessProbe:
                httpGet:
                    port: http
                    path: /
                periodSeconds: 5
                timeoutSeconds: 2
                failureThreshold: 60
        endpoints:
            - port: http
              scheme: http
              path: /
EOF
} > "$blocks/dash-server"

CHANGED=""
for name in redpanda redpanda-console; do   # added by earlier versions of the demo
    [[ -n "$(remove_sidecar "$name")" ]] && CHANGED=yes && info "removed $name"
done
for name in kafka kafka-ui road-safety-service road-safety-tools dash-server; do
    if [[ -n "$(set_sidecar "$name" "$blocks/$name")" ]]; then CHANGED=yes; ok "$name: added or updated"
    else ok "$name: up to date"; fi
done
rm -rf "$blocks"

if [[ -n "$RESTART" ]]; then
    info "Restarting the database to activate Java (about a minute) ..."
    exa stop >/dev/null
    exa start >/dev/null
elif [[ -n "$CHANGED" ]] || ! sidecar_running road-safety-tools || ! sidecar_running dash-server; then
    info "Starting the sidecars (the database keeps running; the first start pulls the images) ..."
    exa start >/dev/null
fi
for i in $(seq 1 60); do sidecar_running road-safety-tools && break; sleep 5; done
sidecar_running road-safety-tools || die "the sidecar road-safety-tools does not run: exasol sidecar status road-safety-tools"
ok "sidecars running"

# ---------------------------------------------------------------------------------------------
step "6/8 Kafka connector scripts"
state="$(tools scripts/check_setup.py)"
has() { grep -qx "$1=yes" <<<"$state"; }
if has kafka_scripts; then ok "KAFKA_EXTENSION has its UDF scripts"
else tools scripts/setup_kafka.py | sed 's/^/   /'; fi

# ---------------------------------------------------------------------------------------------
step "7/8 Open data (ROAD_DATA)"
if has road_data; then
    ok "ROAD_DATA complete"
else
    if on_vm; then stage="$VM_SHARE"; stage_engine="$VM_MOUNT"; else stage="$(mktemp -d)"; stage_engine="$stage"; fi
    if [[ -n "$DATA_ZIP" ]]; then
        cp "$DATA_ZIP" "$stage/munich-road-data.zip"
    else
        info "Downloading $DATASET_URL"
        curl -fL --progress-bar -o "$stage/munich-road-data.zip" "$DATASET_URL"
    fi
    engine cp "$stage_engine/munich-road-data.zip" "$(container road-safety-tools):/tmp/munich-road-data.zip"
    rm -f "$stage/munich-road-data.zip"
    tools scripts/load_dataset.py /tmp/munich-road-data.zip | sed 's/^/   /'
fi

# ---------------------------------------------------------------------------------------------
step "8/8 Simulation and dashboards"
if has simulation; then ok "partners and road operator sensors in ROAD_SAFETY"
else tools scripts/simulate_road_safety.py setup | sed 's/^/   /'; fi
tools scripts/simulate_road_safety.py views | sed 's/^/   /'
info "Waiting for the service and dashboard v2 (Dash Server deploys it by itself) ..."
for i in $(seq 1 90); do
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5100/apps/munich-road-conditions || true)"
    [[ "$code" == "200" ]] && break
    sleep 5
done
if [[ "$code" == "200" ]]; then ok "dashboard v2: http://127.0.0.1:5100/apps/munich-road-conditions"
else info "dashboard v2 isn't up yet; follow it with: ./demo logs dash"; fi

cat <<EOF

${BOLD}${GREEN}Ready.$RESET
  Start the simulated partners:   ./demo sim run --start 07:30 --log events
  Dashboard v2:                   http://127.0.0.1:5100/apps/munich-road-conditions
  Kafka topics:                   http://127.0.0.1:18080
  Then open your AI agent in this folder (it finds the MCP servers in .mcp.json) and say
  "an oil spill on the A9 near Freimann, only OEM A sees it". More in README.md.
EOF
