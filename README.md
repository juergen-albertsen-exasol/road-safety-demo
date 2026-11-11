# Munich Road Safety: a sovereign Data for Road Safety service on Exasol

Carmakers, a navigation service and a road operator report hazards. **Exasol** loads their messages from **Apache Kafka**, confirms a hazard with SQL once independent sources agree, and publishes the warning back, within seconds. A live map of Munich shows it all. Everything runs on your machine, as sidecars of one Exasol Personal database.

It is a reference implementation for partners of **Data for Road Safety (DFRS)**, the road safety use case of the [Mobility Data Space](docs/context.md). The road network, traffic, weather, roadworks and accident history are real open data; the partners and their reports are simulated.

## Quick start

You need **[Exasol Personal](https://github.com/exasol/exasol-personal)** on macOS or Linux, with about 12 GB of free disk space. Nothing else: Kafka, the service, the simulator and the dashboards run in containers next to the database.

1. **Install the Exasol Personal launcher** with its one-line installer. It puts `exasol` into `~/.local/bin`; if that folder isn't on your PATH, follow what the installer prints:
   ```bash
   curl https://downloads.exasol.com/exasol-personal/installer.sh | sh
   exasol version
   ```
2. **Get this repo and run the setup.** It installs the database if you don't have one yet (several minutes, the first time), adds the sidecars, and loads the open data (17 MB):
   ```bash
   git clone https://github.com/exasol-labs/road-safety-demo.git
   cd road-safety-demo
   ./setup.sh
   ```
3. **Start the simulated partners** and keep the terminal open (Ctrl-C stops them):
   ```bash
   ./demo sim run --start 07:30 --log events
   ```
4. **Open the dashboard:** <http://127.0.0.1:5100/apps/munich-road-conditions>. The Kafka topics are on <http://127.0.0.1:18080>.
5. **Trigger a hazard** in a second terminal, and watch it arrive on the map:
   ```bash
   ./demo sim trigger oil_spill --road A9 --near Freimann --direction south --providers OEM_A       # one source: stays a single report
   ./demo sim trigger oil_spill --providers OEM_A,OEM_D                                            # a second source: confirmed, published
   ```

`./setup.sh` is safe to run again: it checks every step and only does what's missing. After a reboot, `exasol start` brings everything back.

## Explore it with your AI agent

The demo is meant to be explored by talking to an AI coding agent, such as [Claude Code](https://claude.com/claude-code) or Codex. Open it in the repo folder. It finds two MCP servers in [`.mcp.json`](.mcp.json): **`exasol-personal`** (read-only SQL on the database, the `mcp` sidecar) and **`dash-server`** (dashboards). It reads [AGENTS.md](AGENTS.md) for the demo's rules: every hazard is announced as a simulation, the SQL is shown before an answer, and the agent writes nothing to the database except through the demo's commands.

Start the simulation (step 3 above), then ask, in plain words:

**What's there**
> *What data do we have about road safety in Munich? Which partners send what, and how much in the last few minutes?*

> *Which road sensors does the road operator have, and what do they measure?*

> *How much traffic is on the A9 near Freimann right now?*

The agent writes the SQL, shows it and runs it. Open data next to partner data: road segments, traffic counts, weather, the reports arriving from Kafka.

**Make something happen**
> *Simulate an oil spill on the A9 near Freimann, southbound. Only OEM A sees it.*

One source: the report shows up as a grey dot on the dashboard, and that's all. Nothing is confirmed, nobody is warned.

> *Now OEM D sees the oil too.*

A second, independent source: about 12 seconds later Exasol confirms the incident, publishes a warning to the topic `srti.confirmed`, and the carmakers' and the road operator's (simulated) systems receive it.

> *Trigger an accident on Fürstenrieder Straße near Laimer Platz, reported by OEM A, B and C.*

Confirmed after the second report. The dashboard suggests a diversion around it and shows who was warned: vehicles per carmaker, app users, a message sign of the road operator.

**Ask why**
> *Was the accident confirmed? Which reports and which rule confirmed it, and how long did it take?*

> *Who got the warning about the accident?*

> *Which warnings did we publish, and which reports confirmed them?*

> *Only count incidents that 3 providers confirm: which would remain?*

**More to try**
> *A wrong-way driver on the A9 southbound near Fröttmaning.* (it moves for about 2 minutes)

> *Ice on the Reichenbachbrücke; OEM B and the road weather station see it.* (a sensor as the second source)

> *An accident on Leopoldstraße near Münchner Freiheit, as slowly as in reality.*

> *Stop all events.*

About once a simulated minute a **false alarm** appears: one carmaker alone reports a hazard where nothing happened. It must never be confirmed. Ask the agent to find them.

Without an agent, the same commands are in [Commands](#commands), and `./demo sql` opens an SQL prompt.

## Exercise: build dashboard v1

Ask your agent for a dashboard of its own:

> *Build a simple dashboard, v1, that shows the road events coming in live from the partners.*

The agent explores the tables, writes SQL and a page of Python ([Dash](https://dash.plotly.com)), and deploys it to Dash Server over MCP. Dash Server validates it, builds it, tests the SQL and serves it, for example at <http://127.0.0.1:5100/apps/munich-road-events>. It reads the database as a read-only user; the app holds no password.

Ideas to go further: a map of the reports valid now, colored by provider; a table of the newest messages; refresh every 2 seconds; a counter of reports per topic. A reference solution is in [`dashboard-v1/`](dashboard-v1/): three SQL files and `app.py`.

> **Note:** dashboards you build don't survive an `exasol stop` yet: sidecars keep no files, so Dash Server starts empty. Dashboard v2 is deployed again automatically on every start. Persistent sidecar storage is coming to Exasol Personal soon; until then, ask your agent to build v1 again, or keep your files in the repo.

## Dashboard v2

Prebuilt and deployed on every start: <http://127.0.0.1:5100/apps/munich-road-conditions>. Its source is [`dashboard/`](dashboard/).

- traffic per road (modelled from real counts), the road operator's sensors, roadworks, accident history
- single reports (grey), confirmed incidents (red) with the evidence behind them, their reach (vehicles passing in the next 10 minutes) and a suggested diversion
- the warnings published to `srti.confirmed` and who received them: vehicles per carmaker, app users, the road operator's message signs
- the combination rule as sliders: what if 3 providers were needed? (a what-if: the service keeps publishing with the default rule)

## Commands

```bash
./demo sim run --start 07:30 --log events          # the partners; keep it running (--speed 3: faster)
./demo sim trigger <event> --road <road> --near <place> [--direction north|south|east|west] [--providers OEM_A,OEM_B]
./demo sim trigger <event> --stop                  # end one event; ./demo sim stop-all ends all
./demo sim stop                                    # stop the simulator (as Ctrl-C in its terminal)
./demo sim scenarios                               # which event fits where, and who can report it
./demo sim status                                  # the run, the simulated time, messages per topic
./demo logs service -f                             # the service: one line per published warning
./demo sql                                         # SQL prompt (exasol connect)
./demo status                                      # database, sidecars, simulation
```

Events: `wrong_way_driver`, `accident`, `slippery_road` (ice), `oil_spill`, `debris`, `animal`, `roadworks`, `fog`, `storm`. Options for who reports and when: `--providers`, `--sensor` / `--no-sensor`, `--delay OEM_D=60` or `--delay realistic`, `--confidence 0.5`. All of them: `./demo sim trigger --help`.

While `./demo sim run` or `./demo sql` runs, the launcher's own `exasol start`, `stop` and `sidecar` commands wait: stop those first.

## What runs where

```
 THE OTHER PARTNERS (simulated)               THE PARTNER'S NODE: one Exasol Personal deployment
 road-safety-tools sidecar                    ┌────────────────────────────────────────────────────────────┐
   ./demo sim run                             │ kafka ──► Exasol: INBOUND_MESSAGES                         │
     carmakers, navigation ── SRTI events ───►│ (sidecar)    │ parse, combine (SQL)                        │
     road operator ──── sensor readings ─────►│              ▼                                             │
   ./demo sim trigger ...                     │ srti.confirmed ◄── INCIDENTS, OUTBOUND_MESSAGES            │
     "an oil spill here, OEM A sees it"       │      ▲    road-safety-service (sidecar): load, combine,    │
                                              │      │    publish, every 1.5 s                             │
     receivers of the warnings ◄──────────────┤      └── dash-server (sidecar): the dashboards, read-only  │
                                              └────────────────────────────────────────────────────────────┘
 open data (OSM, City of Munich, Autobahn, DWD, BASt, Unfallatlas) ──► ROAD_DATA (munich-road-data.zip)
 your agent ◄── MCP: exasol-personal (the mcp sidecar, SQL), dash-server (dashboards)
```

| Sidecar | What it is | Image | License |
|---|---|---|---|
| `mcp` | The database's MCP server: read-only SQL for your agent, <http://127.0.0.1:4896/mcp> | `docker.io/exasol/mcp-server` (Exasol Personal catalog) | MIT |
| `kafka` | [Apache Kafka](https://kafka.apache.org) 4.4, the transport, 127.0.0.1:19092 | `docker.io/apache/kafka` | Apache 2.0 |
| `kafka-ui` | [Kafbat UI](https://github.com/kafbat/kafka-ui): topics and messages, <http://127.0.0.1:18080> | `docker.io/kafbat/kafka-ui` | Apache 2.0 |
| `road-safety-service` | Loads the topics, combines the reports in SQL, publishes warnings | `ghcr.io/exasol-labs/road-safety-demo/road-safety` | MIT |
| `road-safety-tools` | The same image, idle: `./demo` runs the simulator and the setup steps in it | `ghcr.io/exasol-labs/road-safety-demo/road-safety` | MIT |
| `dash-server` | [Dash Server](https://github.com/exasol-labs/dash-server) with dashboard v2, <http://127.0.0.1:5100>, MCP on `/mcp` | `ghcr.io/exasol-labs/road-safety-demo/dash-server` | MIT |

The database pulls the topics itself through the [Exasol Kafka connector](https://github.com/exasol/kafka-connector-extension) (Java UDFs, MIT). Every container gets its database connection from the launcher; no password is written anywhere. Dash Server reads the database as `DASH_READONLY`, a user with `SELECT` on `ROAD_DATA` and `ROAD_SAFETY` only, which gets a new random password on every start. Nothing is sent home: the images switch off pyexasol's telemetry.

How it is built, what we measured and what's still open: [docs/architecture.md](docs/architecture.md). The stage flow of the talk this demo was made for: [docs/demo-runbook.md](docs/demo-runbook.md).

## Limits

- **Simulated partners.** The carmakers, the navigation service, their events and the road operator's sensor readings are synthetic: DFRS data isn't open. The event format follows DATEX II SRTI, the rules are ours.
- **Modelled traffic.** Volumes follow real counts (BASt) but are modelled per road segment; roadworks are a snapshot from October 2026.
- **Dashboards built over MCP are lost on `exasol stop`** (see the exercise). Kafka topics start empty after every start too; Exasol keeps every message.
- **Tested on macOS (Apple silicon).** Linux should work (the images are built for amd64 and arm64) but hasn't been tested yet.

## Repo layout

| Path | Content |
|---|---|
| `setup.sh`, `demo` | Set up the demo; run its commands |
| `AGENTS.md` | Instructions for the AI agent: rules, triggers, questions, troubleshooting |
| `dashboard/` | Dashboard v2 `munich-road-conditions`, deployed by the `dash-server` sidecar |
| `dashboard-v1/` | Reference solution of the exercise |
| `roadsafety/` | SQL: `combine.sql` (the rule), `parse.sql`, the schemas, the Kafka connector scripts |
| `scripts/` | The service, the simulator, the loaders; `lib.sh` for the shell scripts |
| `containers/` | The two images: `road-safety` (service, simulator, tools) and `dash-server` |
| `docs/` | [architecture](docs/architecture.md), [runbook](docs/demo-runbook.md), [context](docs/context.md), [design history](docs/plan.md), [maintainers](docs/maintainers.md) |
| `research/`, `data/source/` | The open data sources with licenses and limits; small inputs kept in git |

## Data and licenses

The code is MIT licensed ([LICENSE](LICENSE)). The data set [`munich-road-data.zip`](https://github.com/exasol-labs/road-safety-demo/releases) is derived from open data under its own licenses: road network © OpenStreetMap contributors (ODbL 1.0; the derived tables are shared under the ODbL). City districts and roadworks: Landeshauptstadt München (dl-de/by-2.0). Motorway events © Die Autobahn GmbH des Bundes (GeoNutzV). Weather: Deutscher Wetterdienst (CC BY 4.0). Traffic counts: Bundesanstalt für Straßenwesen (CC BY 4.0). Accident atlas: Statistische Ämter des Bundes und der Länder (dl-de/by-2.0). Details per dataset in [research/road-data-sources.md](research/road-data-sources.md) and in the zip's `LICENSE.txt`.

The carmakers, providers, their events and the sensor readings are fictional. No real vehicle or DFRS data is used.
