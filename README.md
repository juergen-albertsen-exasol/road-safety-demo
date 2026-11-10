# Munich Road Safety: a sovereign Data for Road Safety service on Exasol

A **reference implementation** for partners of **Data for Road Safety (DFRS)**, the road safety use case of the [Mobility Data Space](docs/context.md), and a live demo of it on a map of Munich.

Carmakers, a navigation service and the road operator send safety events and sensor readings through **Apache Kafka**. **Exasol** loads them, combines reports from independent sources into confirmed warnings with SQL, and publishes those back, within seconds. Everything runs on the partner's own machine, built from **free components**: no central platform, no third-party service, and every message in and out on record in one database.

Say what happens and where, in plain words: *"an oil spill on the A9 near Freimann, only OEM A sees it"*, *"now a second carmaker reports it"*. Your AI agent starts the simulated event, and the dashboard shows the reports arriving from each provider, the single report staying grey, and the incident being confirmed and published once a second source agrees.

- **Sovereign:** runs on a laptop or the partner's own cloud account (AWS, Azure, Exoscale, STACKIT). Kafka keeps nothing; Exasol keeps everything: what came in, what was confirmed, and what was passed on to whom.
- **Free:** Exasol Personal, Apache Kafka and Kafbat UI (Apache 2.0), the Exasol Kafka connector, Dash Server and the launcher (MIT), Python, open data.
- **Open and replaceable:** the Kafka protocol, events in the DATEX II SRTI vocabulary, the rules as plain SQL.
- **Honest about the data:** the road network, traffic volumes, roadworks, weather and accident history are real open data. The partners, their events and the sensor readings are simulated, because DFRS data isn't open. Every hazard is started on purpose, and the combination never reads which ones were started.

How it is built, what we measured and what is still open: **[docs/architecture.md](docs/architecture.md)**.

## Get started with your agent

You need macOS or Linux, Python 3.10 or newer, about 12 GB of free disk space (10 GB for the database, 0.6 GB of downloads), and an AI coding agent such as [Claude Code](https://claude.com/claude-code) or Codex.

1. Clone this repo and open your agent in it.
2. Say **"Set up the demo."**

The agent follows [AGENTS.md](AGENTS.md). It installs and starts the database and the Kafka sidecars, downloads and loads the open data, creates the simulation, starts Dash Server and deploys the dashboard. It checks each step and asks before installing anything on your machine.

Then say **"Start the simulation"** and open <http://127.0.0.1:5100/apps/munich-road-conditions>.

## What runs where

```
 THE OTHER PARTNERS (simulated)            THE PARTNER'S NODE (this repo)
 ./demo sim run                            ┌──────────────────────────────────────────────────────────┐
   carmakers, navigation ── SRTI events ──►│ Apache Kafka ──► Exasol: INBOUND_MESSAGES                │
   road operator ───── sensor readings ───►│ (sidecar)           │ parse, combine (SQL)               │
 ./demo sim trigger ...                    │                     ▼                                    │
   "an oil spill here, OEM A sees it"      │ srti.confirmed ◄─ INCIDENTS, OUTBOUND_MESSAGES           │
                                           │     ▲   ./demo service: load, combine, publish (1.5 s)   │
                                           │     └── Dash Server: the dashboard (read-only)           │
                                           └──────────────────────────────────────────────────────────┘
 open data (OSM, City of Munich, Autobahn, DWD, BASt, Unfallatlas) ──► ROAD_DATA (loaded once)
 your agent ◄── MCP: exasol-personal (SQL), dash-server (dashboard)
```

| Component | What it is | License |
|---|---|---|
| [Exasol Personal](https://github.com/exasol/exasol-personal) | The database, a local deployment `road-safety-demo` on 127.0.0.1:8563, with its MCP server (port 4896) | Free; launcher MIT |
| [Apache Kafka](https://kafka.apache.org) 4.4 | The transport, a sidecar of the deployment (127.0.0.1:19092) | Apache 2.0 |
| [Kafbat UI](https://github.com/kafbat/kafka-ui) | Topics and messages in the browser, a sidecar (<http://127.0.0.1:18080>) | Apache 2.0 |
| [Exasol Kafka connector extension](https://github.com/exasol/kafka-connector-extension) | Java UDFs: the database pulls the topics itself | MIT |
| [Dash Server](https://github.com/exasol-labs/dash-server) | Hosts the dashboard on <http://127.0.0.1:5100>; the agent deploys it over MCP | MIT |
| [exapump](https://github.com/exasol-labs/exapump) | Loads the open data and runs ad-hoc SQL (profile `road-safety-demo`) | MIT |
| `.venv` | Python for the scripts (`requirements.txt`: pyexasol, kafka-python, ...) | MIT, Apache 2.0, ... |

## Set up by hand

The same steps your agent runs, from the repo folder. `./demo <name>` starts a script with the project's Python (`./demo help` lists the names).

```bash
# 1. Database: install the Exasol Personal launcher (https://github.com/exasol/exasol-personal), then
exasol install local -d road-safety-demo
exasol mcp enable -d road-safety-demo

# 2. Python and exapump (https://github.com/exasol-labs/exapump)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./demo access                      # creates the exapump profile road-safety-demo

# 3. Kafka: sidecars, the connector JAR in BucketFS, Java for UDFs, the UDF scripts
./demo kafka                       # then do what it prints: exasol start (or stop + start for Java)

# 4. Open data: download and prepare (data/road-data), then load into ROAD_DATA
./demo prepare
./demo load

# 5. Simulation: providers and sensors, the live view; then the read-only dashboard user
./demo sim setup
./demo sim views
./demo access

# 6. Dash Server, cloned next to this repo
git clone https://github.com/exasol-labs/dash-server ../dash-server
(cd ../dash-server && python3 -m venv .venv && .venv/bin/pip install -e .)
./demo dash                        # keep it running

# 7. Dashboard: ask your agent to "deploy the dashboard" (Dash Server MCP; see AGENTS.md)
```

## Run the demo

```bash
./demo service                                          # keep it running: load, combine, publish
./demo sim run --start 07:30 --speed 3 --log events     # keep it running: the partners
./demo sim trigger oil_spill --road A9 --near Freimann --providers OEM_A     # one source: stays a single report
./demo sim trigger oil_spill --providers OEM_A,OEM_D                         # a second source: confirmed, published
./demo sim trigger oil_spill --stop
```

On stage you don't type these: you tell the agent what happens and where, and it explains the simulation and runs the trigger. Event types: `wrong_way_driver`, `accident`, `slippery_road` (ice), `oil_spill`, `debris`, `animal`, `roadworks`, `fog`, `storm`; `./demo sim scenarios` lists them with the places they fit and who reports them. See [AGENTS.md](AGENTS.md#running-the-demo) for the flow.

## Repo layout

| Path | Content |
|---|---|
| `AGENTS.md` | Instructions for the agent: setup, demo rules, troubleshooting |
| `demo` | Starts the scripts by a short name |
| `dashboard/` | The Dash app `munich-road-conditions`: `app.py` and its SQL in `queries/` |
| `roadsafety/` | SQL: `combine.sql` (the rule), `parse.sql`, the schemas, the Kafka connector scripts |
| `scripts/` | `road_safety_service.py` (the service), `kafka_loader.py`, `setup_kafka.py`, `simulate_road_safety.py` (with `srti_events.py`, `road_scenarios.py`, `road_traffic.py`), `prepare_road_data.py`, `load_road_data.sh`, `setup_access.py`, `build_dashboard_queries.py`, `start_dash_server.sh` |
| `data/source/` | Small inputs kept in git: city districts, city roadworks, congestion hotspots |
| `research/` | The open data sources with licenses and limits; the Munich congestion hotspots |
| `docs/` | [architecture.md](docs/architecture.md): how it is built and why. [context.md](docs/context.md): the Mobility Data Space and DFRS. [plan.md](docs/plan.md): the design and how it evolved |

## Data and licenses

Road network © OpenStreetMap contributors (ODbL 1.0). City districts and roadworks: Landeshauptstadt München (dl-de/by-2.0). Motorway events © Die Autobahn GmbH des Bundes (GeoNutzV). Weather: Deutscher Wetterdienst (CC BY 4.0). Traffic counts: Bundesanstalt für Straßenwesen (CC BY 4.0). Accident atlas: Statistische Ämter des Bundes und der Länder (dl-de/by-2.0). Details per dataset in [research/road-data-sources.md](research/road-data-sources.md).

The carmakers, providers, their events and the sensor readings are fictional. No real vehicle or DFRS data is used.
