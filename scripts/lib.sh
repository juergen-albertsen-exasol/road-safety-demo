# Shared helpers for setup.sh, ./demo and scripts/build_images.sh. Bash only: the one thing a
# user installs is the Exasol Personal launcher (`exasol`); everything else runs in containers.
# shellcheck shell=bash

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
REGISTRY="${ROAD_SAFETY_REGISTRY:-ghcr.io/juergen-albertsen-exasol/road-safety-demo}"
IMAGE_ROAD_SAFETY="$REGISTRY/road-safety:$VERSION"
IMAGE_DASH_SERVER="$REGISTRY/dash-server:$VERSION"
# Dash Server is built from this commit of https://github.com/exasol-labs/dash-server (v0.2.1 plus its
# container image, branch feat/sidecar-container-image). Keep in sync with .github/workflows/images.yml.
DASH_SERVER_REF="c6e9f91d5a315fc04c53b8389891821890af1d79"
DATASET_URL="${ROAD_SAFETY_DATASET_URL:-https://github.com/juergen-albertsen-exasol/road-safety-demo/releases/download/v$VERSION/munich-road-data.zip}"

# The launcher's installer puts `exasol` into ~/.local/bin, which may not be on the PATH yet.
if ! command -v exasol >/dev/null && [[ -x "$HOME/.local/bin/exasol" ]]; then
    PATH="$HOME/.local/bin:$PATH"
fi

DEPLOYMENT="${EXASOL_DEPLOYMENT:-default}"
DEP_DIR="$HOME/.exasol/personal/deployments/$DEPLOYMENT"
VM_SHARE="$DEP_DIR/local/runtime/vm-shared"   # macOS: shared with the database VM ...
VM_MOUNT="/mnt/host"                           # ... where it is mounted here

if [[ -t 1 ]]; then BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'; RESET=$'\e[0m'
else BOLD=""; DIM=""; RED=""; GREEN=""; RESET=""; fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s== %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '   %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# The launcher, quietly: it prints "Using ... deployment directory" on stderr for every command, and
# `exasol start` warns that a running database "is already ready".
quiet() { grep -v -e '^Using ' -e '"level":"INFO"' -e 'is already ready to accept connections' -e 'instance IP, DNS name may have changed' >&2; }
exa() { exasol "$@" -d "$DEPLOYMENT" 2> >(quiet); }

on_vm() { [[ -d "$VM_SHARE" ]]; }

# A command of the container engine that runs the deployment's containers: podman in the database
# VM on macOS, the local podman (or docker) on Linux.
engine() {
    if on_vm; then
        exasol shell host -d "$DEPLOYMENT" -- podman "$@" 2> >(quiet)
    elif command -v podman >/dev/null; then
        podman "$@"
    else
        docker "$@"
    fi
}

# Same, with a terminal (for the simulator's coloured log and Ctrl-C).
engine_tty() {
    if on_vm; then
        exasol shell host -d "$DEPLOYMENT" -- podman "$@" 2> >(quiet)
    elif command -v podman >/dev/null; then
        podman "$@"
    else
        docker "$@"
    fi
}

deployment_id() {
    sed -n 's/.*"deploymentId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$DEP_DIR/deployment.json" | head -1
}

container() { printf 'exasol-db-%s-sidecar-%s' "$(deployment_id)" "$1"; }

sidecar_running() {
    exa sidecar status "$1" --json 2>/dev/null | grep -q '"running"[[:space:]]*:[[:space:]]*true'
}

sidecar_serving() {
    exa sidecar status "$1" --json 2>/dev/null | grep -q '"health"[[:space:]]*:[[:space:]]*"serving"'
}

# Run a script in the road-safety-tools sidecar: ./demo and setup.sh do everything through this.
tools() {
    local t=()
    [[ -t 0 && -t 1 ]] && t=(-it)
    # ${t[@]+...}: an empty array is "unbound" under set -u in bash 3.2 (macOS)
    engine_tty exec ${t[@]+"${t[@]}"} "$(container road-safety-tools)" python "$@"
}

# Make the deployment's sidecars.yaml hold this entry (added, or replaced when it differs); keeps all
# others. Arguments: the sidecar's name and a file with its block, indented to sit under `sidecars:`.
# Prints "changed" when it wrote the file. A backup of the previous file is kept beside it.
set_sidecar() {
    local name="$1" block="$2" file="$DEP_DIR/sidecars.yaml" new
    grep -q '^sidecars:[[:space:]]*$' "$file" || die "$file has no top-level 'sidecars:' key; edit it by hand"
    if awk '/^sidecars:/{s=1;next} s && /^[^ #]/{bad=1} END{exit !bad}' "$file"; then
        die "$file has keys after 'sidecars:'; edit it by hand"
    fi
    # the entry as it is now: from its key to the next key at the same indentation
    if awk -v name="$name" '
        $0 ~ "^    " name ":[ \t]*$" { keep=1; print; next }
        keep && (/^    [^ ]/ || /^[^ ]/) { keep=0 }
        keep { print }
    ' "$file" | cmp -s - "$block"; then
        return 0
    fi
    new="$(mktemp)"
    awk -v name="$name" '
        $0 ~ "^    " name ":[ \t]*$" { skip=1; next }
        skip && /^    [^ ]/ { skip=0 }
        skip && /^[^ ]/ { skip=0 }
        !skip { print }
    ' "$file" > "$new"
    cat "$block" >> "$new"
    cp "$file" "$file.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$new" "$file"
    echo changed
}

remove_sidecar() {
    local name="$1" file="$DEP_DIR/sidecars.yaml" new
    grep -q "^    $name:[[:space:]]*$" "$file" || return 0
    new="$(mktemp)"
    awk -v name="$name" '
        $0 ~ "^    " name ":[ \t]*$" { skip=1; next }
        skip && /^    [^ ]/ { skip=0 }
        skip && /^[^ ]/ { skip=0 }
        !skip { print }
    ' "$file" > "$new"
    cp "$file" "$file.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$new" "$file"
    echo changed
}
