# Demo runbook: Data for Road Safety, sovereign and free

The live part, step by step: what to type, what appears, and a few points to remember. How it is built: [architecture.md](architecture.md).

| # | Step | Where | Time |
|---|---|---|---|
| 1 | The node: the sidecars | Terminal | 1 min |
| 2 | Start the simulation | Terminal | 1 min |
| 3 | Kafka: a tour of Kafbat UI | Browser | 2 min |
| 4 | SQL on the ingested data | Terminal | 2 min |
| 5 | Explore the data with the agent | Agent | 3 min |
| 6 | Dashboard v1, built live | Agent, browser | 3 min |
| 7 | Dashboard v2, prepared | Browser | 2 min |
| 8 | One carmaker: an oil spill | Agent, browser | 2 min |
| 9 | Three carmakers: an accident | Agent, browser | 3 min |
| 10 | Questions, clean up | Agent | 2 min |

## Before the talk (30 minutes ahead)

**1. Check that everything runs.**

```bash
exasol status                                     # "database_ready"
exasol sidecar status                             # kafka, kafka-ui, mcp, road-safety-service: serving
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5100/apps/munich-road-conditions   # 200
```

Database stopped: `exasol start` (about 20 s, starts the sidecars and the service too). Dash Server doesn't answer: `./demo dash` in a terminal of its own.

**2. Open the windows** (large font, dark background):

| Window | What |
|---|---|
| T1 | Terminal for your commands: sidecar status, the SQL prompt (`exasol connect`) |
| T2 | The simulation (`--log events`): every message the partners send, a short grey line per second for the road operator's sensors, the incident banners |
| Agent | Claude Code (or your agent) in the repo folder |
| Browser | Kafbat UI <http://127.0.0.1:18080>; dashboard v2 <http://127.0.0.1:5100/apps/munich-road-conditions> (layers: all on except roadworks and accident history); v1 opens in step 6 |

Optional: a terminal with `./demo sidecar logs -f` (the service: one line per published warning).

**3. Rehearse once, then reset.** Run steps 2–9, then tell the agent *"reset the demo after the rehearsal"*: it deletes the v1 app (after you confirm) so it can be built live again, and withdraws the events. Stop the simulation in T2 (Ctrl-C). Starting it again on stage begins a clean run.

## Live

### 1. The node

```bash
exasol status
exasol sidecar status
```

- Database plus four sidecars: `kafka`, `kafka-ui`, `mcp`, `road-safety-service`
- One `exasol start` for all of it; nothing in a cloud
- The service is a container too: load, combine, publish every 1.5 s

### 2. Start the simulation

T2:

```bash
./demo sim run --start 07:30 --log events
```

- 7:30 on a Tuesday in October; real weather of that day
- Grey lines: the road operator's 158 sensors, each once a minute: a steady stream
- Yellow: false alarms, one carmaker alone
- About one a minute: a demo rate, higher than in reality
- No hazard yet: those I trigger

### 3. Kafka: a tour of Kafbat UI

Browser, <http://127.0.0.1:18080> → Topics.

| Show | Where |
|---|---|
| One topic per partner | Topics: `srti.oem-a` … `srti.oem-f`, `srti.nav`, `datex.road-operator` |
| A message as it travels | `datex.road-operator` → Messages → open one: the JSON of a sensor reading |
| An SRTI event | a topic with a false alarm (the yellow line in T2) → Messages: category, DATEX II type, position, validity, confidence |
| The outbound topic | `srti.confirmed`: empty until the first incident |

- Plain Apache Kafka, Apache 2.0, a sidecar on this laptop
- Finished events, never raw vehicle data
- Kafka only carries; it keeps nothing after a restart

### 4. SQL on the ingested data

T1:

```bash
exasol connect
```

At the SQL prompt (a statement runs at its `;`, it may span several lines):

```sql
SELECT "topic", COUNT(*) AS "messages", MAX("loaded_at") AS "last_loaded"
FROM ROAD_SAFETY.INBOUND_MESSAGES
GROUP BY 1 ORDER BY 1;
```

```sql
SELECT "detected_at", "provider", "status", "srti_category", "road", "topic", "kafka_offset"
FROM ROAD_SAFETY.SRTI_EVENTS
WHERE "sim_run_id" = (SELECT MAX("run_id") FROM ROAD_SAFETY.SIM_STATE)
ORDER BY "detected_at" DESC LIMIT 5;
```

Run the first one twice, a few seconds apart: the counts grow. `exit` leaves the prompt.

- The database pulls the topics itself (Kafka connector, Java UDFs), 2–4 s from send to row
- `INBOUND_MESSAGES`: every message, raw, with its Kafka offset
- `SRTI_EVENTS`: the same, parsed into columns, with SQL
- Exasol keeps everything: the record
- `status`: one report (`event_id`), several messages
  - `new`: first report of a hazard by one provider; every false alarm is only a `new`
  - `update`: the same report again, about every 30 s: current position, more vehicles, longer validity
  - `cancel`: the provider withdraws it (hazard over, provider dropped out)
  - Expiry sends nothing: the report just runs out (false alarms after 10 min)
- The combination counts only the latest version of each report, unless it's a `cancel` or expired

### 5. Explore the data with the agent

Agent, for example:

> *What data do we have about road safety in Munich? Which partners send what, and how much in the last few minutes?*

> *Which road sensors does the road operator have, and what do they measure?*

> *How much traffic is on the A9 near Freimann right now?*

- The agent asks Exasol through the `mcp` sidecar: read-only
- It writes and shows the SQL; I don't
- Open data next to partner data: roads, traffic counts, weather

### 6. Dashboard v1, built live

Agent:

> *Build a simple dashboard, v1, that shows the road events coming in live from the partners.*

The agent creates `munich-road-events` on Dash Server (about a minute) and explains it. Open <http://127.0.0.1:5100/apps/munich-road-events>.

- Three SQL files and a page of Python
- Dash Server validates, builds, tests the SQL, serves it
- Read-only database user, no password in the app
- Refreshes every 2 s; *SQL executed* at the bottom
- The dots so far: false alarms, single carmakers

### 7. Dashboard v2, prepared

Browser, <http://127.0.0.1:5100/apps/munich-road-conditions>.

- Prepared: built over several iterations, takes longer than a demo slot
- Same Dash Server, same database, more SQL
- Traffic per road (from real counts), sensors, single reports grey, incidents, published warnings
- The rule as sliders

### 8. One carmaker: an oil spill

Agent:

> *Simulate an oil spill on the A9 near Freimann, southbound. Only OEM A sees it.*

| After the trigger | What you see |
|---|---|
| at once | v2: dashed outline *SIMULATION: oil spill*; T2: the banner |
| about 10 s | OEM A's report: grey dot in v2, blue dot in v1, red line in T2 |

- Through Kafka into Exasol in seconds
- One source: stays grey, not passed on

### 9. Three carmakers: an accident

Agent:

> *Now an accident on Leopoldstraße near Münchner Freiheit. Three carmakers see it: OEM B, C and E.*

| After the trigger | What you see |
|---|---|
| about 9 s | OEM B's report: one grey dot |
| about 17 s | OEM C's report: **confirmed**. v2: red marker, incident card, *Published to srti.confirmed*; T2: *INCIDENT CONFIRMED BY EXASOL* |
| about 24 s | OEM E: the card updates to 3 providers |

- Second independent source → confirmed: one SQL query, a fifth of a second
- Card: which carmakers, vehicles passing in the next 10 minutes
- Warning published back to Kafka (Kafbat UI: `srti.confirmed` now has messages)

Then in v2: *Different providers needed* → 3.

- The rule is a parameter of the SQL
- Accident still holds (three carmakers); the oil spill never had a chance
- Set it back to 2

### 10. Questions, clean up

Agent: *"Which warnings did we publish, and which reports confirmed them?"*

- Kafka keeps nothing, Exasol keeps everything: the audit trail

Clean up: *"Stop all events."*: the carmakers withdraw, the clearing is published too.

## Timing

Measured on the wall clock (2026-11-10), the service as a sidecar, simulation speed 1:

| Trigger | First report in Exasol | Confirmed and published | Last report |
|---|---|---|---|
| Oil spill, 1 OEM | 8.5 s | never (one source) | |
| Accident, 3 OEMs | 7 s | 15 s | 22 s |

The dashboard adds up to 2–3 s, the agent 10–20 s for announcing and starting the trigger. The providers' pace: `STAGE_DELAYS` in `scripts/srti_events.py` (5, 12, 20 s of simulated time). Don't use `--speed` on stage: the reports would come within a few seconds.

## If something goes wrong

| Symptom | Do |
|---|---|
| No new dots after a trigger | T2 (simulation) running? `exasol sidecar status`: `road-safety-service` serving? The banner says when each carmaker reports. |
| The accident doesn't confirm | Banner: two or more carmakers named? `./demo sidecar logs` shows what the service published. |
| v1 can't be created: the app exists | Left from a rehearsal: let the agent replace it, or open it as it is. |
| v1 shows an error | Reload. If it persists, the agent reads `app_tail_logs`. |
| Dashboard frozen | Reload. The clock under the title says whether the simulation is stopped. |
| Kafka trouble (no messages arrive) | Fallback without Kafka: `./demo sidecar disable`, then `./demo service --transport direct` in T1 and `./demo sim run --start 07:30 --log events --transport direct` in T2. Afterwards `./demo sidecar enable`. |
| Database stopped | `exasol start` (starts the service too), then restart T2. |

## Optional extras

| What | Say to the agent |
|---|---|
| A second source for the oil spill | *"Now OEM D sees the oil too."* (confirmed about 12 s later) |
| The road operator as the second source | *"Ice on the Reichenbachbrücke, OEM B and the road weather station see it."* |
| A wrong-way driver | *"A wrong-way driver on the A9 southbound near Fröttmaning."* (moves for about 2 minutes) |
| Realistic pace | add *"as slowly as in reality"*: the second report after about a minute |
