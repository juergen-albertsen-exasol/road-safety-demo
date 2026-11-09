# Munich Road Safety: live hazard detection on Exasol

A live demo of **Data for Road Safety (DFRS)**, the road safety use case of the [Mobility Data Space](docs/context.md): connected vehicles and road sensors report what they notice, and SQL in [Exasol](https://www.exasol.com) turns those signals into confirmed safety warnings on a live map of Munich, within seconds.

Say what happens and where, in plain words: *"a wrong-way driver on the A9 southbound near Garching"*, *"an oil spill on Fürstenrieder Straße near Laimer Platz"*. Your AI agent starts the simulated event, and the page shows the signals arriving, the sensors turning red, and the incident being confirmed. Hover over the incident to see the evidence behind it: which vehicles of which providers and which sensors reported it, how fast it was confirmed, and how many vehicles nearby would receive the warning.

- **Real open data:** the OpenStreetMap road network of Munich and the motorway ring, City of Munich roadworks, Autobahn GmbH motorway events, DWD weather, BASt traffic counts, the accident atlas.
- **Simulated:** 6,000 connected probe vehicles from seven fictional providers and 158 road sensors, each reporting every few seconds. Every hazard is started on purpose and marked as a simulation.
- **Detected in SQL:** one rule per SRTI category confirms a hazard from several vehicles, providers and sensors, then computes its reach. The detection never reads which hazards were started.

## Get started with your agent

You need macOS or Linux, Python 3.10 or newer, about 12 GB of free disk space (10 GB for the database, 0.6 GB of downloads), and an AI coding agent such as [Claude Code](https://claude.com/claude-code) or Codex.

1. Clone this repo and open your agent in it.
2. Say **"Set up the demo."**

The agent follows [AGENTS.md](AGENTS.md). It installs and starts the database, downloads and loads the open data, creates the simulation, starts Dash Server and deploys the dashboard. It checks each step and asks before installing anything on your machine.

Then say **"Start the simulation"** and open <http://127.0.0.1:5100/apps/munich-road-conditions>.

## What runs where

```
open data (OSM, city, Autobahn, DWD, BASt) --prepare_road_data.py--> CSV --load_road_data.sh--> ROAD_DATA
                                                                                                  |
simulate_road_safety.py run      ---- vehicle signals, sensor readings every 2 s ---->  ROAD_SAFETY
simulate_road_safety.py trigger  ---- "start an oil spill here" (SCENARIO_LOG)          |
                                                                                        |  SQL: hazards, reach,
Dash Server: munich-road-conditions  <---- read-only (dash_readonly) -----------------  evidence, traffic, diversions
Your agent  <---- MCP: exasol-personal (SQL), dash-server (dashboard) ----
```

| Component | What it is |
|---|---|
| [Exasol Personal](https://github.com/exasol/exasol-personal) | The database, a local deployment named `road-safety-demo` on 127.0.0.1:8563. Its MCP server (port 4896) lets the agent run SQL |
| [exapump](https://github.com/exasol-labs/exapump) | Loads the CSVs and runs ad-hoc SQL (profile `road-safety-demo`) |
| [Dash Server](https://github.com/exasol-labs/dash-server) | Hosts the dashboard on <http://127.0.0.1:5100>; the agent deploys it over MCP |
| `.venv` | Python for data preparation, the simulator and setup (`requirements.txt`) |

## Set up by hand

The same steps your agent runs. Commands are run from the repo folder.

```bash
# 1. Database: install the Exasol Personal launcher (https://github.com/exasol/exasol-personal), then
exasol install local -d road-safety-demo
exasol mcp enable -d road-safety-demo

# 2. Python and exapump (https://github.com/exasol-labs/exapump)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/setup_access.py          # creates the exapump profile road-safety-demo

# 3. Open data: download and prepare (data/road-data), then load into ROAD_DATA
.venv/bin/python scripts/prepare_road_data.py
scripts/load_road_data.sh

# 4. Simulation: schema ROAD_SAFETY, vehicles, sensors, detection views
.venv/bin/python scripts/simulate_road_safety.py setup
.venv/bin/python scripts/simulate_road_safety.py views
.venv/bin/python scripts/setup_access.py          # again: now also creates the read-only dashboard user

# 5. Dash Server, cloned next to this repo
git clone https://github.com/exasol-labs/dash-server ../dash-server
(cd ../dash-server && python3 -m venv .venv && .venv/bin/pip install -e .)
scripts/start_dash_server.sh                      # keep it running

# 6. Dashboard: ask your agent to "deploy the dashboard" (Dash Server MCP; see AGENTS.md)
```

## Run the demo

```bash
.venv/bin/python scripts/simulate_road_safety.py run --start 07:30 --speed 3 --log events   # keep it running
.venv/bin/python scripts/simulate_road_safety.py trigger wrong_way_driver --road A9 --near Garching-Süd --direction south
.venv/bin/python scripts/simulate_road_safety.py trigger oil_spill --road "Fürstenrieder Straße" --near "Laimer Platz"
.venv/bin/python scripts/simulate_road_safety.py stop-all
```

On stage you don't type these: you tell the agent what happens and where, and it explains the simulation, runs the trigger and reports what the SQL detected. Event types: `wrong_way_driver`, `accident`, `slippery_road` (ice), `oil_spill`, `debris`, `animal`, `roadworks`, `fog`, `storm`. `scenarios` lists them with the places they fit. See [AGENTS.md](AGENTS.md#running-the-demo) for the flow.

## Repo layout

| Path | Content |
|---|---|
| `AGENTS.md` | Instructions for the agent: setup, demo rules, troubleshooting |
| `dashboard/` | The Dash app `munich-road-conditions`: `app.py` and its SQL in `queries/` |
| `roadsafety/` | SQL templates: schema, hazard detection, reach, evidence, traffic, diversions (with a Python routing UDF) |
| `scripts/` | `prepare_road_data.py`, `load_road_data.sh`, `simulate_road_safety.py` (with `road_scenarios.py`, `road_traffic.py`), `build_dashboard_queries.py`, `setup_access.py`, `start_dash_server.sh` |
| `data/source/` | Small inputs kept in git: city districts, city roadworks, congestion hotspots |
| `research/` | The open data sources with licenses and limits; the Munich congestion hotspots |
| `docs/` | [context.md](docs/context.md): the Mobility Data Space and DFRS. [plan.md](docs/plan.md): the design and how it evolved |

## Data and licenses

Road network © OpenStreetMap contributors (ODbL 1.0). City districts and roadworks: Landeshauptstadt München (dl-de/by-2.0). Motorway events © Die Autobahn GmbH des Bundes (GeoNutzV). Weather: Deutscher Wetterdienst (CC BY 4.0). Traffic counts: Bundesanstalt für Straßenwesen (CC BY 4.0). Accident atlas: Statistische Ämter des Bundes und der Länder (dl-de/by-2.0). Details per dataset in [research/road-data-sources.md](research/road-data-sources.md).

The vehicles, providers and sensors are fictional. No real vehicle data is used.
