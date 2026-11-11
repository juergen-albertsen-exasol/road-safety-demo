# Maintainers: data, images, releases

Users need only the Exasol Personal launcher and `./setup.sh`. This page is for changing the demo: rebuilding the data set, the images, and publishing a release.

## Development environment

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Python 3.10–3.14 (pyexasol 3 doesn't support 3.15 yet). The scripts connect as `sys` on 127.0.0.1:8563 with the password from the deployment's `secrets.json`; inside a sidecar they take the connection from `EXASOL_DB_*`. pyexasol's telemetry is switched off in `scripts/db.py` and in the images.

## The open data (`munich-road-data.zip`)

Rebuilding `ROAD_DATA` from its sources (about 600 MB of downloads into `~/.cache/munich-road-data`):

```bash
./demo prepare                        # scripts/prepare_road_data.py: writes data/road-data/*.csv
scripts/load_road_data.sh             # loads them with exapump (https://github.com/exasol-labs/exapump), profile road-safety-demo
./demo export-data                    # scripts/export_dataset.py: dist/munich-road-data.zip
```

The zip holds one Parquet file per table, `schema.sql` (the exact column types), `manifest.json` (row counts, checked by the loader) and `LICENSE.txt` (sources and licenses; the OSM-derived tables are ODbL). `scripts/load_dataset.py` loads it in the `road-safety-tools` sidecar; `./setup.sh --data dist/munich-road-data.zip` uses a local copy instead of the release asset. Sources and licenses: [research/road-data-sources.md](../research/road-data-sources.md).

## The images

| Image | Source | Content |
|---|---|---|
| `ghcr.io/exasol-labs/road-safety-demo/road-safety:<VERSION>` | `containers/road-safety/Containerfile` | `scripts/`, `roadsafety/`, Python 3.13 with pyexasol, kafka-python, shapely, numpy, pyarrow. Runs the service (default command) and, as `road-safety-tools`, idles for `./demo` |
| `ghcr.io/exasol-labs/road-safety-demo/dash-server:<VERSION>` | `containers/dash-server/Containerfile` | Dash Server built from [exasol-labs/dash-server](https://github.com/exasol-labs/dash-server) at `DASH_SERVER_REF` (`scripts/lib.sh` and the workflow), plus `dashboard/` and `start.sh` / `bootstrap.py`: the read-only user's password, the Exasol profile `road-safety-demo`, dashboard v2 deployed on every start |

Build and try them locally, in the deployment's container engine (the VM on macOS), with the published tags. The sidecars use `imagePullPolicy: IfNotPresent`, so local images take the place of the published ones:

```bash
./demo build-images all --apply       # scripts/build_images.sh: builds, then replaces the running sidecars
```

Change `dashboard/` or `roadsafety/combine.sql` → rebuild `dash-server` (and `road-safety` for SQL). `dashboard/queries/incidents_live.sql` is generated from `roadsafety/combine.sql`: `./demo queries` after changing it.

## Testing a change like a new user

On a second deployment, so `default` stays as it is (stop it first: both use the same ports):

```bash
exasol stop -d default
exasol install local -d rs-test
EXASOL_DEPLOYMENT=rs-test ./demo build-images all
EXASOL_DEPLOYMENT=rs-test ./setup.sh --data dist/munich-road-data.zip
EXASOL_DEPLOYMENT=rs-test ./setup.sh                       # again: every step "up to date"
EXASOL_DEPLOYMENT=rs-test ./demo sim run --start 07:30 --log events
exasol destroy -d rs-test && exasol remove -d rs-test
```

## A release

1. Set the version in `VERSION` (setup.sh pins the images and the data set to it).
2. Commit, tag and push: `git tag v$(cat VERSION) && git push origin main --tags`. The workflow `images` builds both images for linux/amd64 and linux/arm64 and pushes them to ghcr.io. New packages on ghcr.io start private: make them public once (package settings → visibility), or setup.sh can't pull them.
3. Create the GitHub release with the data set: `gh release create v$(cat VERSION) dist/munich-road-data.zip --title "v$(cat VERSION)" --notes "..."`.
4. Test on a fresh deployment without local images (above, skipping `build-images`).
