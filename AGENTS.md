# AGENTS.md

Standing instructions for an AI coding agent (Claude Code, Codex or another client) working in this repo. Claude Code and Codex read this file automatically when started in the repo folder.

**What this repo is:** a live demo of Data for Road Safety (DFRS) on Exasol. Simulated connected vehicles and road sensors stream signals into a local Exasol database; SQL detects the eight SRTI hazard categories from those signals; a Dash dashboard shows them live on a map of Munich. Background: [docs/context.md](docs/context.md). Design and history: [docs/plan.md](docs/plan.md). Overview for people: [README.md](README.md).

The user is usually a presenter preparing or giving a talk. Two kinds of requests: **setting the demo up** on a fresh machine, and **running it** on stage.

## Ground rules

- **Never print, copy or ask for a password.** The `sys` password lives in `~/.exasol/personal/deployments/road-safety-demo/secrets.json`, the dashboard password in `secrets/dash_readonly_password`. Scripts read them; you don't. Never put a password on a command line or in a file outside those places.
- **Analysis SQL is read-only.** Answer questions with `SELECT` through the `exasol-personal` MCP server (or `exapump sql -p road-safety-demo` when MCP isn't available). Writes happen only through the repo's scripts: loading (`load_road_data.sh`), the simulator (`simulate_road_safety.py`) and `setup_access.py`.
- **Every hazard is a visible simulation.** Before each trigger, say in two or three plain lines that this is a simulation, what it injects (which signals, where) and that it writes no hazard: detection is left to the SQL. After the trigger, show its SIMULATION banner line.
- **Show your SQL** before you interpret a result. State what the data can't say: the vehicles and sensors are synthetic; traffic volume follows real counts but is modelled; roadworks are a snapshot.
- **Metric units:** m and km, km/h, seconds and minutes, °C.
- **Ask before installing software** (the Exasol Personal launcher, exapump, Dash Server) or changing anything outside this repo. Everything inside the repo and the database deployment `road-safety-demo` is yours to set up.

## Setting up the demo

When the user says "set up the demo" (or similar), work through these steps in order. **Check first, act only if needed**: every step can be skipped if its check already passes, so the same procedure also repairs a half-finished setup. After each step, report one line. Stop and explain if a step fails twice.

| # | Check | If it fails |
|---|---|---|
| 1 | `exasol version` works | Ask to install the Exasol Personal launcher: <https://github.com/exasol/exasol-personal> (release binary for the platform, on the PATH) |
| 2 | `exasol status -d road-safety-demo` says running | Not installed: `exasol install local -d road-safety-demo` (takes several minutes, needs about 10 GB). Stopped: `exasol start -d road-safety-demo` |
| 3 | `exasol mcp status -d road-safety-demo` serves on port 4896 | `exasol mcp enable -d road-safety-demo` |
| 4 | `exasol sidecar status -d road-safety-demo` shows `redpanda` (127.0.0.1:19092) and `redpanda-console` (<http://127.0.0.1:18080>) serving, `exasol slc list -d road-safety-demo` shows JAVA installed, and `KAFKA_EXTENSION` has 3 scripts (`EXA_ALL_SCRIPTS`) | `.venv/bin/python scripts/setup_kafka.py`: adds the sidecars, puts the Kafka connector JAR into BucketFS, records the Java container, creates the scripts. Then do what it prints: `exasol start` for new sidecars (the database keeps running), `exasol stop` + `exasol start` for Java. Topics are empty after every start: Exasol keeps the data |
| 5 | `exapump --version` works | Ask to install exapump: <https://github.com/exasol-labs/exapump> |
| 6 | `.venv/bin/python -c "import osmium, shapely, pyexasol, kafka"` works | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| 7 | `exapump profile show road-safety-demo` works | `.venv/bin/python scripts/setup_access.py` (creates the profile; warns that the schemas don't exist yet, which is fine here) |
| 8 | `ROAD_DATA` has 15 tables with rows: `SELECT TABLE_NAME, TABLE_ROW_COUNT FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = 'ROAD_DATA'` | `.venv/bin/python scripts/prepare_road_data.py` (downloads about 600 MB into `~/.cache/munich-road-data`, writes `data/road-data/`), then `scripts/load_road_data.sh`. Run it in the background and report progress |
| 9 | `.venv/bin/python scripts/simulate_road_safety.py status` shows vehicles and the views exist (`ROAD_SAFETY.V_HAZARD_REACH`) | `simulate_road_safety.py setup`, then `simulate_road_safety.py views` |
| 10 | User `DASH_READONLY` exists (`EXA_DBA_USERS`) and `secrets/dash_readonly_password` exists | `.venv/bin/python scripts/setup_access.py` |
| 11 | Dash Server answers on <http://127.0.0.1:5100> | Ask to clone and install it next to this repo: `git clone https://github.com/exasol-labs/dash-server ../dash-server && (cd ../dash-server && python3 -m venv .venv && .venv/bin/pip install -e .)`. Then start it in the background: `scripts/start_dash_server.sh` |
| 12 | The `dash-server` and `exasol-personal` MCP tools are available in this session | Both are registered in `.mcp.json`. The client loads them at start: ask the user to restart the session (Claude Code: approve the project MCP servers when asked), then continue from here |
| 13 | `apps_list` shows `munich-road-conditions` | Deploy the dashboard, see below |

At the end, give the URL <http://127.0.0.1:5100/apps/munich-road-conditions> and say how to start the simulation.

### Deploying the dashboard

The source is `dashboard/`: `app.py`, `requirements.txt`, `dash-app.json` (profile `road-safety-demo`) and `queries/` with `sql_smoke.json` (sample parameters for the healthcheck).

1. New app: `app_create_from_files` with name `munich-road-conditions`, title "Munich Road Conditions", template `exasol-analytics`, and every file from `dashboard/` (`dash-app.json` binds the profile `road-safety-demo`). Dash Server adds `dash_server_exasol.py`, the query helper `app.py` imports. Existing app: `app_put_files` with the changed files.
2. `app_deploy_draft{name: "munich-road-conditions", deployment_target: "live", auto_rollback_on_health_failure: true}`. The result includes validation and the healthcheck.
3. If it fails: `app_tail_logs` or `app_inspect_traceback`, fix, deploy again. Warnings about unreferenced SQL files or unbounded queries can be ignored: the app loads its queries through the helper.

The composite queries (`hazards_live.sql`, `hazard_evidence.sql`, `traffic_state.sql`, `congestion.sql`, `diversions.sql`) are generated from `roadsafety/`. After changing a template there, run `.venv/bin/python scripts/build_dashboard_queries.py dashboard/queries` and `simulate_road_safety.py views`, then redeploy. Keep `dashboard/` as the source of truth: when you change the live app, change the repo file too.

## Running the demo

**Start:** check that the database and Dash Server run, then start the simulator in the background:

```bash
.venv/bin/python scripts/simulate_road_safety.py run --start 07:30 --speed 3
```

It writes a batch every 2 seconds and keeps running until stopped. `status` shows the run and the simulated time; `speed <factor>` changes the speed while it runs. The presenter may prefer a second terminal with `--log events`, which prints event signals, sensor alerts and confirmed incidents.

**Runs don't resume.** Every `run` starts a new run (new `run_id`) at its `--start` time and first closes every event that is still open in `SCENARIO_LOG` (it sets `ended_at` to the start time). So:
- After the simulator has stopped, there is nothing to clean up: an event without `ended_at` from an earlier run is not active, and it won't come back. Don't tell the user it will.
- To continue a scenario after a restart, trigger it again in the new run.
- While no run is active, the dashboard shows the last state of the stopped run, frozen. It switches to the new run once one starts.
- To check whether anything is really active, look at the `status` of the latest run in `SIM_STATE` first, then at the open events of that `run_id`.

**A trigger request** ("a wrong-way driver on the A9 southbound near Garching", "oil on Fürstenrieder Straße near Laimer Platz"):

1. Map the words to one event, road, place and direction. Event types and aliases: `wrong_way_driver` (wrong_way), `accident` (crash), `slippery_road` (ice), `oil_spill` (oil), `debris` (obstacle), `animal`, `roadworks` (works), `fog` (visibility), `storm` (weather). `simulate_road_safety.py scenarios` lists where each fits.
2. If the wording leaves the spot open, run with `--dry-run` first and say where it lands ("Fürstenrieder Straße southbound near Laimer Platz, 4 road segments"). If the command says the road doesn't pass the place or the event doesn't fit the road, propose something close instead of forcing it.
3. Announce the simulation (see ground rules), then run `simulate_road_safety.py trigger <event> --road <road> --near <place> [--direction <dir>]` and show the banner.
4. Watch for the confirmation: poll `ROAD_SAFETY.V_HAZARD_REACH` every few seconds in the background (up to about 3 minutes) and report category, cause, time from first signal to confirmation, vehicles, providers, sensors, confidence and the reach. Busy motorways confirm in seconds; quiet streets without a sensor can take a minute or two.

`trigger <event> --stop` ends one event, `stop-all` ends all of them. Hazards then clear one after another as vehicles pass without reporting them.

**Questions during the demo** ("why was it confirmed?", "who gets warned?", "only count hazards that 3 providers confirm"): answer from the views with SQL. `V_HAZARD_EVIDENCE` has every signal and sensor reading behind a hazard. The thresholds are parameters of the templates in `roadsafety/hazards.sql`; the dashboard has sliders for providers and vehicles.

## The data

| Schema | Content |
|---|---|
| `ROAD_DATA` | Real open data: `ROAD_SEGMENTS` (OSM roads split into segments, with class, one-way, speed limit, bridge, tunnel), `ROAD_AREAS`, `PLACES` (805 named places for `--near`), `DISTRICTS`, `ROADWORKS`, `MOTORWAY_EVENTS`, `ACCIDENTS`, `WEATHER_*`, `TRAFFIC_PROFILE`, `BAST_STATIONS`, `TRAFFIC_HOTSPOTS`, `SEGMENT_TRAFFIC`, `ROAD_DATA_SOURCES` (source and license per table) |
| `ROAD_SAFETY` | The simulation: `DATA_PROVIDERS`, `VEHICLES`, `INFRA_SENSORS`, `VEHICLE_SIGNALS`, `SENSOR_READINGS`, `SCENARIO_LOG` (what was triggered: the ground truth), `SIM_STATE` (the simulated "now"); views `V_HAZARDS_LIVE`, `V_HAZARD_REACH`, `V_HAZARD_EVIDENCE` |

Sources, licenses and limits: [research/road-data-sources.md](research/road-data-sources.md).

**SQL quirks:**
- Column names are lowercase and must be double-quoted (`"hazard_type"`); schema and table names are upper case and unquoted.
- Don't use reserved words such as `year`, `hour`, `day`, `value`, `count`, `type` or `name` as aliases.
- "Now" is the simulated time in `SIM_STATE`, not the wall clock.
- The detection SQL must never read `SCENARIO_LOG`. Comparing detections with `SCENARIO_LOG` afterwards is fine, and a good way to show what was injected and what was found.
- The templates in `roadsafety/` use pyexasol placeholders (`{window_min!d}`); `simulate_road_safety.py views` fills them with the defaults.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The `exasol-personal` MCP server fails with ECONNREFUSED | The database or its MCP sidecar isn't running: `exasol start -d road-safety-demo`, then `exasol mcp status -d road-safety-demo` |
| The dashboard is empty or frozen | The simulator isn't running (`simulate_road_safety.py status`), or the database is stopped |
| The dashboard page shows an error, while the healthcheck is fine | The healthcheck doesn't run callbacks: read `app_tail_logs` |
| "license limit of 20 parallel connections", or a git conflict in Dash Server | Wait a few seconds and retry once |
| A trigger is never confirmed | Check that the simulator runs and that vehicles pass the spot (quiet side streets have few). Look at `VEHICLE_SIGNALS` near the spot since the trigger |
| A sidecar fails with `toomanyrequests: You have reached your unauthenticated pull rate limit` | Pull from `docker.io/redpandadata/...`, not `docker.redpanda.com` (that front hits Docker Hub's anonymous limit). `scripts/setup_kafka.py` already does |
| A Kafka import fails with `No resolvable bootstrap urls` | The UDF sandbox has no DNS; the Java scripts read `redpanda` from the hosts file in BucketFS. `scripts/kafka_loader.py` rewrites it at start and on this error. By hand: run the loader once with `--once` |
| Kafka messages don't arrive in `ROAD_SAFETY.INBOUND_MESSAGES` | Is the loader running (`scripts/kafka_loader.py`)? It prints one line per import. The topic must start with `srti.` or `datex.` (or pass `--prefix`) |
| pyexasol bulk import fails on the version string of Exasol Personal | Known; the simulator uses multi-row INSERTs instead. Don't switch it to `import_from_iterable` |
