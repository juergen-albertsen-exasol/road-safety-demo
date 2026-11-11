#!/usr/bin/env bash
# Build the demo's two images locally, in the Exasol Personal deployment's container engine (on macOS:
# the database VM), tagged exactly as the published ones (maintainers only).
#
#   scripts/build_images.sh [road-safety|dash-server|all] [--apply]
#
# --apply replaces the running sidecars that use a rebuilt image (same tag, so `exasol start` alone
# would keep the old containers).
#
# setup.sh's sidecars use imagePullPolicy IfNotPresent, so locally built images take the place of the
# published ones: this is how a change is tested before .github/workflows/images.yml publishes it.
# Users never run this.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
source "$ROOT/scripts/lib.sh"

what="all"; apply=""
for arg in "$@"; do
    case "$arg" in
        --apply) apply=yes ;;
        road-safety|dash-server|all) what="$arg" ;;
        *) die "unknown argument: $arg" ;;
    esac
done
ctx_name="road-safety-build"
if on_vm; then
    ctx_local="$VM_SHARE/$ctx_name"        # on the laptop ...
    ctx_engine="$VM_MOUNT/$ctx_name"       # ... and the same folder as the VM sees it
else
    ctx_local="$(mktemp -d)/$ctx_name"
    ctx_engine="$ctx_local"
fi

stage_context() {
    rm -rf "$ctx_local"
    mkdir -p "$ctx_local"
    (cd "$ROOT" && tar -cf - --exclude '__pycache__' VERSION scripts roadsafety dashboard containers) | tar -xf - -C "$ctx_local"
}

build_road_safety() {
    say "image: building $IMAGE_ROAD_SAFETY"
    engine build --quiet -t "$IMAGE_ROAD_SAFETY" -f "$ctx_engine/containers/road-safety/Containerfile" "$ctx_engine" >/dev/null
}

build_dash_server() {
    local base="localhost/dash-server-base:${DASH_SERVER_REF:0:12}"
    if engine image exists "$base" 2>/dev/null; then
        say "image: $base already built"
    else
        say "image: building $base from exasol-labs/dash-server@${DASH_SERVER_REF:0:12}"
        rm -rf "$ctx_local/dash-server-src" && mkdir -p "$ctx_local/dash-server-src"
        curl -fsSL "https://github.com/exasol-labs/dash-server/archive/$DASH_SERVER_REF.tar.gz" \
            | tar -xzf - -C "$ctx_local/dash-server-src" --strip-components 1
        engine build --quiet -t "$base" "$ctx_engine/dash-server-src" >/dev/null
    fi
    say "image: building $IMAGE_DASH_SERVER"
    engine build --quiet -t "$IMAGE_DASH_SERVER" --build-arg "DASH_SERVER_IMAGE=$base" \
        -f "$ctx_engine/containers/dash-server/Containerfile" "$ctx_engine" >/dev/null
}

stage_context
case "$what" in
    road-safety) build_road_safety ;;
    dash-server) build_dash_server ;;
    all)         build_road_safety; build_dash_server ;;
    *)           die "unknown image: $what (road-safety, dash-server or all)" ;;
esac
rm -rf "$ctx_local"
if [[ -n "$apply" ]]; then
    case "$what" in
        road-safety) sidecars="road-safety-service road-safety-tools" ;;
        dash-server) sidecars="dash-server" ;;
        all)         sidecars="road-safety-service road-safety-tools dash-server" ;;
    esac
    for s in $sidecars; do engine rm -f "$(container "$s")" >/dev/null 2>&1 || true; done
    say "sidecars: replacing $sidecars (exasol start)"
    exa start >/dev/null
else
    say "Done. To replace the running sidecars: scripts/build_images.sh $what --apply"
fi
