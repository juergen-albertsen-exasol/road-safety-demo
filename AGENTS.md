# AGENTS.md

Standing instructions for an AI coding agent (Claude Code, Codex or another client) working in this repo. Claude Code and Codex read this file automatically when started in the repo folder.

**What this repo is:** a live demo of Data for Road Safety (DFRS) on Exasol, built as a reference implementation a DFRS partner can run on its own machine. Simulated carmakers, a navigation service and a road operator send SRTI events and sensor readings through Kafka; Exasol loads them, combines them with SQL into confirmed incidents and publishes those back as warnings; a Dash dashboard shows it live on a map of Munich. Everything runs as sidecars of one Exasol Personal deployment. Background: [docs/context.md](docs/context.md). Design and history: [docs/plan.md](docs/plan.md). Overview for people: [README.md](README.md).

The user is exploring the demo, or a presenter preparing or giving a talk with it (stage flow: [docs/demo-runbook.md](docs/demo-runbook.md)). Requests are of three kinds: **setting it up**, **exploring and steering it** (questions, triggers), and **building dashboard v1** (an exercise).

The database is the Exasol Personal deployment `default` (no `-d` needed; the scripts take `EXASOL_DEPLOYMENT` for another one). This demo's dashboards are `munich-road-conditions` (v2, prebuilt) and `munich-road-events` (v1, the exercise).

## Ground rules

- **Never print, copy or ask for a password.** The containers get the database connection from the launcher; Dash Server's read-only user gets a new random password on every start. The `sys` password lives in `~/.exasol/personal/deployments/default/secrets.json`: never read it.
- **Analysis SQL is read-only.** Answer questions with `SELECT` through the `exasol-personal` MCP server; when MCP isn't available, `exasol connect -c '<SELECT ...>;'`. Writes happen only through `./setup.sh` and `./demo` (the simulator and its triggers).
- **Every hazard is a visible simulation.** Before each trigger, say in two or three plain lines that this is a simulation, which partners will report what and where, and that it writes no incident: confirming it is left to the SQL. After the trigger, show its SIMULATION banner line.
- **Show your SQL** before you interpret a result. State what the data can't say: the partners, their events and the sensor readings are synthetic; traffic volume follows real counts but is modelled; roadworks are a snapshot.
- **Metric units:** m and km, km/h, seconds and minutes, °C.
- **Don't stop the user's processes.** The user often runs the simulator (`./demo sim run ...`) in a terminal of their own. Before starting a run, check `./demo sim status`: if the latest run is `running`, don't start a second one (the service follows the newest run). `./demo sim stop` stops every run in the tools sidecar, the user's too: use it only for a run you started yourself. Never use `pkill -f`.
- **Ask before installing software** (the Exasol Personal launcher) or changing anything outside this repo and the deployment `default`.

## MCP servers

Use exactly these two, both registered for this repo in `.mcp.json`:

| Server | Address | For |
|---|---|---|
| `exasol-personal` | <http://127.0.0.1:4896/mcp> | Read-only SQL on the database: the `mcp` sidecar of Exasol Personal |
| `dash-server` | <http://127.0.0.1:5100/mcp> | Creating, deploying and inspecting dashboards: the `dash-server` sidecar; Exasol profile `road-safety-demo` (read-only user `DASH_READONLY`) |

The user's client may have other MCP servers connected, some of them for Exasol too: don't use them for this demo, nor document, chat or ticket tools unless the user asks for them by name. If `exasol-personal` or `dash-server` fails with ECONNREFUSED, the database or the sidecar isn't running (it may have been down when the session started): `exasol start`, then tell the user to reconnect with `/mcp` (Claude Code) or restart the session. Until then use `exasol connect -c '<SELECT ...>;'` for SQL.

## Setting up the demo

When the user says "set up the demo" (or similar): check that the launcher exists (`exasol version`). If it doesn't (and `~/.local/bin/exasol` doesn't exist either), ask the user to install it with the one-line installer `curl https://downloads.exasol.com/exasol-personal/installer.sh | sh`: don't install it yourself. Then run `./setup.sh` in the background and report its progress, one line per step. It checks every step and does only what's missing, so the same command repairs a half-finished setup:

1. the launcher, 2. the database (`exasol install local` if there's none: several minutes, about 10 GB, its EULA; then `exasol start`), 3. the `mcp` sidecar, 4. the Java script language container and the Kafka connector JAR in BucketFS, 5. the sidecars `kafka`, `kafka-ui`, `road-safety-service`, `road-safety-tools`, `dash-server` (images from `ghcr.io/juergen-albertsen-exasol/road-safety-demo`), 6. the Kafka connector's UDF scripts, 7. the open data (`munich-road-data.zip` from the GitHub release, 17 MB) into `ROAD_DATA`, 8. the simulation's partners and sensors, the view `V_INCIDENTS_LIVE`, and dashboard v2.

If a step fails twice, stop and explain: `./demo logs <sidecar>` shows what a sidecar prints, `exasol sidecar status` their state. At the end, if the `exasol-personal` and `dash-server` MCP tools aren't available in this session, ask the user to reconnect them (they're loaded at session start). Then give the URL <http://127.0.0.1:5100/apps/munich-road-conditions> and say how to start the simulation.

**Dashboard v2 deploys itself:** the `dash-server` sidecar deploys `munich-road-conditions` from `dashboard/` (baked into its image) on every start, once the demo's tables exist. Sidecars keep no files, so after `exasol stop` Dash Server starts empty and v2 comes back by itself; a v1 built in the exercise doesn't (persistent sidecar storage is coming to Exasol Personal soon). To try a change to v2 without a new image: `app_put_files` with the changed files from `dashboard/`, then `app_deploy_draft{name: "munich-road-conditions", deployment_target: "live", auto_rollback_on_health_failure: true}`. Keep `dashboard/` as the source of truth. `queries/incidents_live.sql` is generated from `roadsafety/combine.sql`: after changing that, run `./demo queries` (maintainers, see [docs/maintainers.md](docs/maintainers.md)).

## Running the demo

**Start:** check that the database and its sidecars run (`exasol sidecar status`: `kafka`, `kafka-ui`, `mcp`, `road-safety-service`, `road-safety-tools`, `dash-server`), then start the simulator, in the background:

```bash
./demo sim run --start 07:30
```

`./demo` runs everything in the sidecars: `./demo sim ...` is `scripts/simulate_road_safety.py` in `road-safety-tools`; `./demo logs service -f` shows what the service prints (one line per published warning); `./demo help` lists the rest. While `./demo sim run` or `./demo sql` runs, the launcher's `exasol start`, `stop` and `sidecar` commands wait for the deployment lock ("Deployment directory is locked"); `./demo sim trigger` and the other `./demo` commands still work.

The simulator plays the partners: six carmakers and a navigation service send SRTI events (one Kafka topic each, `srti.oem-a` ... `srti.nav`), the road operator sends its sensor readings (`datex.road-operator`). The service (`road-safety-service`) is what a partner would run: every 1.5 seconds it loads the topics into `INBOUND_MESSAGES`, parses them into `SRTI_EVENTS` and `SENSOR_READINGS`, combines them (`roadsafety/combine.sql`), keeps `INCIDENTS` and publishes every new, changed or cleared incident to the topic `srti.confirmed` (logged in `OUTBOUND_MESSAGES`). **The consumers of the warnings** are simulated too (`scripts/warning_consumers.py`, started by every `run`; `--no-consumers` leaves them out): OEM A–F and the navigation service each read `srti.confirmed` in a consumer group of their own (`warning-consumer.oem-a` …, visible in Kafbat UI under Consumers) and warn their share of the vehicles that pass the spot (fleet shares in `DATA_PROVIDERS`); the road operator switches a message sign about 1.5 km before the incident on major roads, and off when it clears. Each sends a receipt to `srti.receipts`, which the service loads into `WARNING_RECEIPTS`. The service survives `exasol stop`/`start` and publishes the active incidents again. `./demo sim status` shows the run, the simulated time and the messages per topic; `./demo sim speed <factor>` changes the speed while it runs. The user usually runs it in a terminal of their own with `--log events`, which prints every SRTI event sent (red: from a triggered scenario; yellow, marked `[false alarm]`: a single report nobody confirms), a cyan line per warning with who received it, one short grey line per second with the road operator's readings of that second (each of the 158 sensors reports once a minute at its own second: about 2.6 messages per second), a red line for every sensor alert, and a banner for every incident Exasol confirms or clears.

**Two dashboards.**

- **v1, the exercise:** when the user asks for a simple dashboard of the live events ("build a simple dashboard, v1, that shows the road events coming in"), build it: explore the tables with SQL, write the queries and a Dash page, and deploy it as `munich-road-events` (title "Munich Road Events") with `app_create_from_files` (`template: "exasol-analytics"`, `data_sources: {"primary": {"kind": "exasol", "profile": "road-safety-demo"}}`, `start_immediately: false`, your files with their contents), then `app_deploy_draft{name: "munich-road-events", deployment_target: "live", auto_rollback_on_health_failure: true}`. Keep the SQL in `queries/*.sql` and load it through Dash Server's helper `dash_server_exasol` (the template adds it); "now" is `SIM_STATE."sim_now"` of the latest run, not the wall clock. Explain in a few lines how it works while it deploys, then give <http://127.0.0.1:5100/apps/munich-road-events>. Ignore info notes about SQL files "not referenced by any .py file": the helper loads them. A reference solution is in `dashboard-v1/` (three SQL files, `app.py`): use it when the user asks for the prepared version (send its files' contents with `app_create_from_files`; keep the server's `dash-app.json`), or to compare. If `munich-road-events` already exists, say so in one line and ask whether to replace it (`app_delete`, then create it again).
- **v2, prebuilt:** `munich-road-conditions` (from `dashboard/`), with incidents, evidence, reach, suggested diversions, who received each warning (on the card; on the map the warned stretch before the incident and the road operator's message signs), the published warnings and the rule sliders. Diversions (`dashboard/diversions.py`) are computed on the page for accidents, blocked roads and obstacles: the fastest route around the blocked segment on the OSM road network (speed limits, one-way streets, a penalty for small roads), without live traffic or turn restrictions. If asked how it was built: beforehand, over several iterations, because it takes longer than a demo slot.

**Reset after a rehearsal:** delete the v1 app (`app_delete` for `munich-road-events`, after the user confirms) so it can be built again, and run `./demo sim stop-all`. A new `run` closes everything else.

**False alarms are part of the show:** about one per simulated minute by default (`--false-alarms 60` per hour, a demo rate, higher than in reality), one provider reports a hazard where nothing happened. They must never be confirmed. The simulator keeps each one out of the grouping radius of the active false alarms and triggered hazards of its category (`FALSE_ALARM_SPACING_M`, the radii of `roadsafety/combine.sql`; change both together). If one is confirmed anyway, check that spacing first.

**Runs don't resume.** Every `run` starts a new run (new `run_id`) at its `--start` time and first closes every event that is still open in `SCENARIO_LOG` (it sets `ended_at` to the start time). So:
- After the simulator has stopped, there is nothing to clean up: an event without `ended_at` from an earlier run is not active, and it won't come back. Don't tell the user it will.
- To continue a scenario after a restart, trigger it again in the new run.
- While no run is active, the dashboard shows the last state of the stopped run, frozen. It switches to the new run once one starts.
- To check whether anything is really active, look at the `status` of the latest run in `SIM_STATE` first, then at the open events of that `run_id`.

**A trigger request** ("a wrong-way driver on the A9 southbound near Garching", "oil on Fürstenrieder Straße near Laimer Platz"):

1. Map the words to one event, road, place and direction. Event types and aliases: `wrong_way_driver` (wrong_way), `accident` (crash), `slippery_road` (ice), `oil_spill` (oil), `debris` (obstacle), `animal`, `roadworks` (works), `fog` (visibility), `storm` (weather). `./demo sim scenarios` lists where each fits.
2. If the wording leaves the spot open, run with `--dry-run` first and say where it lands ("Fürstenrieder Straße southbound near Laimer Platz, 4 road segments"). If the command says the road doesn't pass the place or the event doesn't fit the road, propose something close instead of forcing it.
3. Announce the simulation (see ground rules), then run `./demo sim trigger <event> --road <road> --near <place> [--direction <dir>]` and show the banner. It says who will report the event and after how many seconds. By default the providers report at a stage pace: the first after 5 s, the second after 12 s, the third after 20 s (the road operator's sensor after 8 s), so an incident is confirmed about 15 to 20 s after the trigger. The dashboard shows the triggered spot at once as a grey circle marked SIMULATION (display only: the combination never reads it).
4. Stop there: don't wait or poll for the confirmation. The user watches it on the dashboard and in the simulator's `--log`. Only when asked ("was it confirmed?", "what happened to the accident?") look it up in `ROAD_SAFETY.INCIDENTS` (current run) and report category, cause, the rule that confirmed it, providers and sensors, the time from the first report to the confirmation (`first_report_at`, `confirmed_at`), confidence and the reach. If it isn't confirmed yet, say why: the providers' delays (in the banner), or only one provider (`--providers`), so it stays a single report.

`./demo sim trigger <event> --stop` ends one event, `./demo sim stop-all` ends all of them. The providers then withdraw their events (`cancel`).

**Steering who reports**, to show the combination rule at work. Map plain requests to these options of `./demo sim trigger`:

| Request | Command |
|---|---|
| "only OEM A sees the oil" (for a new event) | `trigger oil_spill --road ... --near ... --providers OEM_A` |
| "now a second carmaker reports it" | `trigger oil_spill --providers OEM_A,OEM_D` (no place: changes the running event; delays count from now) |
| "the road operator's sensor sees it too" / "without the sensor" | `trigger oil_spill --sensor` / `--no-sensor` |
| "OEM D only after a minute" | `--delay OEM_D=60` (or `--delay 30` for all) |
| "as slowly as in reality" | `--delay realistic` (each scenario's own delays, 5 to 90 s) |
| "with low confidence" | `--confidence 0.5` (or `OEM_A=0.9`) |

Not every provider can report everything (`scripts/srti_events.py`): the navigation service has no airbag or ESC data (obstacles and road works only), OEM E and F have no environment sensors (no fog or storm), road works come from the road operator's warning trailer. The command refuses an impossible combination and lists who can; propose one of those.

**Questions** ("why was it confirmed?", "who gets warned?", "what was passed on?", "only count incidents that 3 providers confirm"): answer with SQL. `INCIDENT_EVIDENCE` lists the reports behind an incident (join `SRTI_EVENTS` on `event_id` = `report_id` for the details); `V_INCIDENTS_LIVE` is the combination with the default rule right now, including single reports that are not confirmed; `OUTBOUND_MESSAGES` is everything published, with its payload; `WARNING_RECEIPTS` who received it ("who got the warning about the accident?"). For a stricter or looser rule, render `roadsafety/combine.sql` with other parameters (`min_providers`, `sensor_replaces`); the dashboard has the same as sliders (what-if: the service keeps publishing with the default rule).

## The data

| Schema | Content |
|---|---|
| `ROAD_DATA` | Real open data, loaded from `munich-road-data.zip`: `ROAD_SEGMENTS` (OSM roads split into segments, with class, one-way, speed limit, bridge, tunnel), `ROAD_AREAS`, `PLACES` (805 named places for `--near`), `DISTRICTS`, `ROADWORKS`, `MOTORWAY_EVENTS`, `ACCIDENTS`, `WEATHER_*`, `TRAFFIC_PROFILE`, `BAST_STATIONS`, `TRAFFIC_HOTSPOTS`, `SEGMENT_TRAFFIC`, `ROAD_DATA_SOURCES` (source and license per table) |
| `ROAD_SAFETY` | The partners: `DATA_PROVIDERS` (with their topic), `INFRA_SENSORS`. What arrives: `INBOUND_MESSAGES` (every message as received from Kafka, raw JSON), parsed into `SRTI_EVENTS` (every version of every event) and `SENSOR_READINGS`. What the service makes of it: `INCIDENTS` (stable id, lifecycle, rule, providers, reach), `INCIDENT_EVIDENCE`, `OUTBOUND_MESSAGES` (what was published to `srti.confirmed`), `WARNING_RECEIPTS` (who received each warning, when, and what they did: vehicles warned, message sign switched), `DATA_OFFERS` and `CONTRACTS` (data space metadata, not enforced), the view `V_INCIDENTS_LIVE`. The simulation: `SCENARIO_LOG` (what was triggered and who was told to report it: the ground truth), `SIM_STATE` (the simulated "now") |
| `KAFKA_EXTENSION`, `KAFKA_STAGE` | The Kafka connector's UDF scripts, and the loader's staging table per topic (only the last row per partition, for the connector's position) |

Sources, licenses and limits: [research/road-data-sources.md](research/road-data-sources.md).

**The status of an SRTI event** (`SRTI_EVENTS."status"`): an event (`event_id`) is one provider's report; every message about it has a new `version`. `new`: the first report (every false alarm is only a `new`). `update`: the same report again, about every 30 s (wrong-way driver 15 s, fog 60 s, road works 120 s), with the current position, more `observations`, usually higher confidence and a longer validity. `cancel`: the provider withdraws it (the hazard was stopped, the provider was taken out with `--providers`, or the wrong-way driver left the road). Expiry sends nothing: the report runs out at `valid_until` (false alarms after 10 minutes). The combination counts only the latest version of each event, unless it's a `cancel` or expired. The road operator's minute-by-minute readings are in `SENSOR_READINGS`, not here.

**SQL quirks:**
- Column names are lowercase and must be double-quoted (`"srti_category"`); schema and table names are upper case and unquoted.
- Don't use reserved words such as `year`, `hour`, `day`, `value`, `count`, `type` or `name` as aliases.
- "Now" is the simulated time in `SIM_STATE`, not the wall clock.
- The combination SQL must never read `SCENARIO_LOG`, and the messages never carry a scenario id. Comparing incidents with `SCENARIO_LOG` afterwards is fine, and a good way to show what was injected and what was confirmed.
- `roadsafety/combine.sql` uses pyexasol placeholders (`{min_providers!d}`); the service and `./demo sim views` fill them with the defaults.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The `exasol-personal` or `dash-server` MCP server fails with ECONNREFUSED | The database or the sidecar isn't running: `exasol start`, then `exasol sidecar status`; reconnect with `/mcp` |
| "Deployment directory is locked by another operation" | `./demo sim run`, `./demo sql`, `./demo logs -f` or `exasol connect` holds the launcher's lock while it runs; `exasol start`, `stop` and `sidecar` wait for it. Stop that first (Ctrl-C), or wait |
| The dashboard is empty or frozen | The simulator isn't running (`./demo sim status` shows the run and the messages per topic), or the database is stopped |
| Dashboard v2 is missing (404) | Dash Server deploys it once the demo's tables exist: `./demo logs dash` says what it waits for or why it failed. `./setup.sh` creates what's missing |
| Events arrive but nothing is confirmed | Is the service serving? `exasol sidecar status road-safety-service`; `./demo logs service` shows a line per published incident. Was the event triggered with one provider only (`--providers`)? Compare `V_INCIDENTS_LIVE` (single reports have `confirmed` false) |
| The dashboard page shows an error, while the healthcheck is fine | The healthcheck doesn't run callbacks: read `app_tail_logs` |
| "license limit of 20 parallel connections" | Wait a few seconds and retry once |
| A trigger's events don't arrive | Check that the simulator and the service run. Its banner and `--log events` show who reports it and when; `INBOUND_MESSAGES` shows what arrived (filter `"payload"` on the provider or category) |
| A sidecar fails with `toomanyrequests: You have reached your unauthenticated pull rate limit` | Docker Hub limits anonymous pulls. Wait a while and run `exasol start` again |
| A Kafka import fails with `No resolvable bootstrap urls`, or times out | The UDF sandbox has no DNS (exasol/exasol-personal#464), so the imports name Kafka by its IP, and Kafka must advertise that IP: the `kafka` entry in `sidecars.yaml` needs the `command` that `./setup.sh` writes (run it again). The loader looks the IP up again after a failed import |
| The service sidecar is `failing` or keeps restarting | `./demo logs service` shows why; it retries the database and Kafka every 10 s by itself |
| A Kafka import fails with `Error parsing script options` | A script in `KAFKA_EXTENSION` lost the `;` after its last option. `./demo sim ...` can't fix it; recreate the scripts with `exasol shell host -- podman exec <road-safety-tools container> python scripts/setup_kafka.py` (ask the user first), which sends each script with its closing `/` and so keeps the `;` |
