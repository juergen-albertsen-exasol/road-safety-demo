# Demo runbook: Data for Road Safety, sovereign and free

A demo of about 20 minutes: five slides first, then everything live in the terminal, the agent and the browser, without going back to the slides. The slides are the deck *Data for Road Safety in Munich*; the architecture behind it is in [architecture.md](architecture.md).

| Part | What | Where | Time |
|---|---|---|---|
| Slides 1–5 | Use case, technical setup, simulator, what you'll see | Deck | 6 min |
| 1 | The node: the sidecars | Terminal | 1 min |
| 2 | Start the simulation | Terminal | 1 min |
| 3 | Dashboard v1, built live | Agent, browser | 3 min |
| 4 | Dashboard v2, prepared | Browser | 2 min |
| 5 | One carmaker: an oil spill | Agent, browser | 2 min |
| 6 | Three carmakers: an accident | Agent, browser | 3 min |
| 7 | Questions, clean up | Agent | 2 min |

## Before the talk (30 minutes ahead)

**1. Check that everything runs.**

```bash
exasol status -d road-safety-demo                 # "database_ready"
exasol sidecar status -d road-safety-demo         # kafka, kafka-ui, mcp, road-safety-service: serving
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5100/apps/munich-road-conditions   # 200
```

If the database is stopped: `exasol start -d road-safety-demo` (about 20 s, starts the sidecars and the service too). If Dash Server doesn't answer: `./demo dash` in a terminal of its own.

**2. Open the windows** (large font, dark background):

| Window | What | |
|---|---|---|
| T1 | Terminal for your commands (sidecar status, starting the simulation) | Step 1, 2 |
| T2 | The simulation runs here (`--log events`): every message the partners send, the incident banners | From step 2 on |
| Agent | Claude Code (or your agent) in the repo folder | Step 3, 5, 6 |
| Browser | The deck; dashboard v2 <http://127.0.0.1:5100/apps/munich-road-conditions> (layers: all on except roadworks and accident history); v1 opens in step 3 | |

Optional: a third terminal with `./demo sidecar logs -f` (the service's line per published warning) and Kafbat UI <http://127.0.0.1:18080>.

**3. Rehearse once, then reset.** Run steps 2–6, then tell the agent *"reset the demo after the rehearsal"*: it deletes the v1 app (after you confirm) so it can be built live again, and withdraws the events (`./demo sim stop-all`). Stop the simulation in T2 (Ctrl-C). Starting it again on stage begins a clean run.

## The slides (6 min)

**Slide 1, cover.** "What a partner in Data for Road Safety would run: a service that takes safety events from several carmakers and the road operator, decides which it can trust, and passes the warnings on. On the partner's own machine, from free components."

**Slide 2, use case.** "Data for Road Safety is a use case of the Mobility Data Space. The goal: get safety warnings to as many drivers as possible, from what vehicles and roads detect. Carmakers such as BMW, Ford or Volkswagen, map and traffic services, and road authorities across Europe take part. They share eight kinds of events, from slippery road to wrong-way driver: finished events, never raw vehicle data. Every partner runs its own connector with Kafka, data goes directly from partner to partner, and each provider decides who gets it. Service providers combine the reports and pass warnings on. That's what our demo plays. And the question for a service provider is: when is a report a warning you can trust? One carmaker is one source, however many of its cars saw it. Two different carmakers, or one plus the road operator's sensor, make it an incident."

**Slide 3, technical setup** (the same picture as slide 2, as it runs). "On the left, the partners: the simulator plays six carmakers, a navigation service and the road operator, each with its own Kafka topic. In the middle, the service provider's node: Exasol Personal with its sidecars, one `exasol start`. Kafka carries the messages, the database pulls them itself, combines them with one SQL query and keeps the record; the road safety service drives that every one and a half seconds. On the right, what comes out: confirmed warnings on `srti.confirmed`, and the dashboards on Dash Server. With real partners, an EDC connector sits in front of the same topics, and nothing else changes. And my agent steers it all: triggers, SQL questions, dashboards."

**Slide 4, simulator.** "Munich is real: roads, traffic per hour, weather, roadworks, accident history, all open data. The partners are simulated, because DFRS data isn't open. A hazard only exists when I trigger one, and the reports come after 5, 12 and 20 seconds: compressed, real backends take a minute or more."

**Slide 5, what you'll see.** Read the six steps. "From here on, no more slides."

## Live

### 1. The node (1 min)

T1:

```bash
exasol sidecar status -d road-safety-demo
```

> "This is the partner's node. The database, and four sidecars: Kafka, its UI, the MCP server for my agent, and our service. One `exasol start` brings up all of it. Nothing runs in a cloud."

### 2. Start the simulation (1 min)

T2:

```bash
./demo sim run --start 07:30 --log events
```

> "Now the other partners start sending: it's 7:30 on a Tuesday in October in Munich, with the real weather of that day. The road operator's sensors send a reading every minute, and now and then a single carmaker reports something nobody else sees: a false alarm. No hazard yet: I'll trigger those."

### 3. Dashboard v1, built live (3 min)

Agent:

> *Build a simple dashboard, v1, that shows the road events coming in live from the partners.*

The agent creates the app `munich-road-events` on Dash Server and explains while it deploys (about a minute). Open <http://127.0.0.1:5100/apps/munich-road-events>.

> "Three SQL files and a page of Python. Dash Server validates it, builds it, tests the SQL against the database and serves it, with a read-only database user: no password in the app. Every two seconds the page asks Exasol what came in. Each dot is a report, in the colour of the carmaker that sent it." Open *SQL executed* at the bottom.

There may be few dots yet: only false alarms, now and then. That's fine: the message list on the right fills with sensor readings within a minute.

### 4. Dashboard v2, prepared (2 min)

Switch to <http://127.0.0.1:5100/apps/munich-road-conditions>.

> "This is version two. I built it beforehand, over several iterations, because it takes longer than a demo slot. Same Dash Server, same database, a lot more SQL: traffic volume per road, modelled from real counts, the road sensors, single reports in grey, confirmed incidents with the reports behind them, how many vehicles pass the spot, and every warning we publish. And the rule itself, as sliders."

### 5. One carmaker: an oil spill (2 min)

Agent:

> *Simulate an oil spill on the A9 near Freimann, southbound. Only OEM A sees it.*

The agent says it's a simulation and runs `trigger oil_spill --road A9 --near Freimann --direction south --providers OEM_A` (it takes 10 to 20 s to answer). Then, from the trigger:

| After | What you see |
|---|---|
| at once | v2: a dashed outline *SIMULATION: oil spill* on the A9; T2: the banner |
| about 10 s | OEM A's report: a grey dot in v2, a blue dot in v1, a red line in T2 |

> "OEM A reports oil on the A9. Its report comes through Kafka into Exasol within seconds. And it stays grey: one source is not enough. It's not passed on."

### 6. Three carmakers: an accident (3 min)

Agent:

> *Now an accident on Leopoldstraße near Münchner Freiheit. Three carmakers see it: OEM B, C and E.*

The agent runs `trigger accident --road Leopoldstraße --near "Münchner Freiheit" --providers OEM_B,OEM_C,OEM_E`. From the trigger:

| After | What you see |
|---|---|
| about 9 s | OEM B's report (one dot, grey in v2) |
| about 17 s | OEM C's report: **confirmed**. v2: the red marker, the incident card (2 providers, about 390 vehicles in the next 10 minutes), *Published to srti.confirmed* in the ticker; T2: *INCIDENT CONFIRMED BY EXASOL* |
| about 24 s | OEM E's report: the card updates to 3 providers |

> "Three carmakers, three independent sources. As soon as the second one is in, Exasol confirms it: one SQL query, a fifth of a second, and the warning goes back out through Kafka for everyone. The card says why: which carmakers, and how many vehicles pass this spot in the next ten minutes, from real traffic counts."

Then the rule, in v2: set *Different providers needed* to 3.

> "And if a partner wants to be stricter? The rule is a parameter of the SQL. Require three: the accident still holds, three carmakers saw it. The oil spill never had a chance." (Set it back to 2.)

### 7. Questions, clean up (2 min)

Questions go to the agent, which answers with SQL: *"Which warnings did we publish, and which reports confirmed them?"*

> "Kafka keeps nothing. Exasol keeps everything: what came in from whom, which reports confirmed which incident, every warning that went out. That's the audit trail, in the partner's own database."

Clean up when you're done: *"Stop all events."* (`./demo sim stop-all`): the carmakers withdraw their reports and the clearing is published too.

## Timing

Measured on the wall clock on 2026-11-10, the service as a sidecar, simulation speed 1 (`--start 07:30`, no `--speed`):

| Trigger | First report in Exasol | Confirmed and published | Last report |
|---|---|---|---|
| Oil spill, 1 OEM | 8.5 s | never (one source) | |
| Accident, 3 OEMs | 7 s | 15 s | 22 s |

The dashboard adds up to 2–3 s (it refreshes every 2 s). Add the agent's own 10 to 20 s for announcing and starting the trigger: enough time to say what's about to happen. The providers' pace is set in `scripts/srti_events.py` (`STAGE_DELAYS`: 5, 12, 20 s of simulated time). Don't run the simulation faster (`--speed`) on stage: the reports would arrive within a few seconds, which looks unrealistic.

## If something goes wrong

| Symptom | Do |
|---|---|
| No new dots after a trigger | Is T2 (simulation) running? `exasol sidecar status -d road-safety-demo`: is `road-safety-service` serving? The banner says when each carmaker reports. |
| The accident doesn't confirm | Check the banner: are two or more carmakers named? `./demo sidecar logs` shows what the service published. |
| v1 can't be created: the app exists | Left from a rehearsal: let the agent replace it, or open it as it is. |
| v1 shows an error | Reload. If it persists, the agent reads `app_tail_logs`. |
| Dashboard frozen | Reload the page. The clock under the title says whether the simulation is stopped. |
| Kafka trouble (no messages arrive) | Fallback without Kafka: `./demo sidecar disable`, then in T1 `./demo service --transport direct` and in T2 `./demo sim run --start 07:30 --log events --transport direct`. Everything works, without the Kafka part of the story. Afterwards `./demo sidecar enable`. |
| Database stopped | `exasol start -d road-safety-demo` (starts the service too), then restart T2. |

## Optional extras

| What | Say to the agent |
|---|---|
| A second source for the oil spill | *"Now OEM D sees the oil too."* (confirmed about 12 s later) |
| The road operator as the second source | *"Ice on the Reichenbachbrücke, OEM B and the road weather station see it."* |
| A wrong-way driver | *"A wrong-way driver on the A9 southbound near Fröttmaning."* (moves for about 2 minutes) |
| Realistic pace | add *"as slowly as in reality"*: the second report after about a minute |
