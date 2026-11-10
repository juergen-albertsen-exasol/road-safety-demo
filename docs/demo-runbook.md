# Demo runbook: Data for Road Safety, sovereign and free

A step-by-step demo of about 20 minutes, with the commands to type and what to say. The slides are the deck *Data for Road Safety in Munich*; the architecture behind it is in [architecture.md](architecture.md).

Every command runs from the repo folder. Instead of typing a trigger you can also say it to your agent in plain words ("an oil spill on the A9 near Freimann, only OEM A sees it"); it runs the same command and shows the banner (see [AGENTS.md](../AGENTS.md#running-the-demo)).

## Before the talk (30 minutes ahead)

**1. Check that everything runs.**

```bash
exasol status -d road-safety-demo                 # "database_ready"
exasol sidecar status -d road-safety-demo         # kafka, kafka-ui, mcp, road-safety-service: serving
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5100/apps/munich-road-conditions   # 200
```

If the database is stopped: `exasol start -d road-safety-demo` (about 20 s, starts the sidecars too, the service among them). If Dash Server doesn't answer: `./demo dash` in a terminal of its own.

**2. Open four terminals** (large font, dark background):

| Terminal | Command | Keep it visible? |
|---|---|---|
| T1 Service | `./demo sidecar logs -f` (the service runs as a sidecar) | Yes: one line per published warning |
| T2 Partners | `./demo sim run --start 07:30 --log events` | Yes: every message the partners send, and the incident banners |
| T3 Triggers | (your commands below) | Yes |
| T4 SQL | (the queries in step 8) | Only for step 8 |

T1 only shows the service's output; the service itself runs with the database. A new simulator run closes everything left from rehearsals; the service clears their incidents.

**3. Open three browser tabs:** the deck; the dashboard <http://127.0.0.1:5100/apps/munich-road-conditions> (layers: all on except roadworks and accident history; rule: 2 providers, sensor may replace one); Kafbat UI <http://127.0.0.1:18080> (Topics).

**4. Rehearse once:** steps 3 and 4 below, then `./demo sim stop-all`. Then restart T2 (Ctrl-C, start again) for a clean run.

## The demo

### Step 1: The use case (slides 1–4, 3 min)

> "Data for Road Safety is a partnership of carmakers, map providers and road authorities in the Mobility Data Space: more than 8 million connected vehicles. Each carmaker detects hazards in its own backend and shares a finished event in one of eight categories. Never raw data."
>
> "The question is: when is a report a warning you can trust? One carmaker reporting ice, even from 26 of its own cars, is one source. If its detection is off, all 26 are off the same way."

### Step 2: The partner's node (slides 5–6, 3 min)

> "This is what a partner would run. Everything inside the dashed box runs on their own machine or cloud account: Kafka as the transport, Exasol as the system of record, a small service, a dashboard. All free components: Exasol Personal, Apache Kafka, open-source tools, open data."

Switch to the terminals and Kafbat UI:

```bash
exasol sidecar status -d road-safety-demo
```

> "Kafka runs as a sidecar of the database, on this laptop, and so does our service: one `exasol start` brings up the whole node. Here are the topics: one per partner. And here (T2) are the messages the partners are sending right now: road sensor readings every minute, and now and then a single carmaker reporting something nobody else sees: a false alarm."

For a technical audience, slides 7–11 go deeper (real and synthetic, the partners, the sensors, the combination rule, the system of record); otherwise skip to the live demo and come back to slides 10–11 when the questions start.

### Step 3: The dashboard at rest (1 min)

Dashboard tab. Point to: traffic volume per road (modelled from real counts), road sensors, grey dots.

> "Grey dots are single reports: one provider, nobody else. They stay grey. They are never passed on."

### Step 4: One source (2 min)

```bash
./demo sim trigger oil_spill --road A9 --near Freimann --direction south --providers OEM_A
```

What happens: the banner in T3; on the map at once a dashed outline *SIMULATION: oil spill*; after about 5 s a red line in T2 (OEM A, NEW) and a grey dot on the A9.

> "This is a simulation: I just put oil on the A9, and only one carmaker sees it. Its report arrives through Kafka into Exasol within seconds. And it stays grey: one source isn't enough."

Optional: Kafbat UI → `srti.oem-a` → Messages: the JSON of the event (category, DATEX II type, position, validity, confidence, cause: fluid).

### Step 5: A second source (2 min)

```bash
./demo sim trigger oil_spill --providers OEM_A,OEM_D
```

What happens: after about 12 s OEM D's report (T2), one round later the incident: the red marker, the card on the right, the banner *INCIDENT CONFIRMED BY EXASOL* in T2, a line `NEW INC-…` in T1, and the warning in the ticker *Published to srti.confirmed*.

> "A second carmaker, a different source, reports the same thing at the same place, in the same direction. Now Exasol confirms it: one SQL query, a fifth of a second. The card says why: two providers, cause fluid, and about how many vehicles pass this spot in the next ten minutes, from real traffic volumes. And the warning is already published back to Kafka, for everyone."

Optional: Kafbat UI → `srti.confirmed`: the outgoing warning, with the sources' attribution.

### Step 6: A sensor as the second source (2 min)

```bash
./demo sim trigger slippery_road --area reichenbachbruecke --providers OEM_B --sensor
```

What happens: OEM B after 5 s, the road weather station on the Reichenbachbrücke after 8 s; confirmed as *1 provider and a road operator sensor*, cause ice.

> "It doesn't always need two carmakers. Here one carmaker reports ice on the Reichenbachbrücke, and the road operator's weather station on the bridge measures a frozen surface. Two independent sources: confirmed."

### Step 7: Change the rule (1 min)

On the dashboard, set *Different providers needed* to 3.

> "The rule is a parameter of the SQL. Require three providers, and the oil spill is no longer an incident: two aren't enough. This is a what-if on the page; the service keeps publishing with its own rule." (Set it back to 2. Open *SQL executed* at the bottom to show the query.)

### Step 8: Sovereign: everything on record (2 min)

T4:

```bash
exapump sql -p road-safety-demo 'SELECT "sim_at", "status", "incident_id", "topic", "kafka_offset" FROM ROAD_SAFETY.OUTBOUND_MESSAGES ORDER BY "sent_wall" DESC LIMIT 5'
exapump sql -p road-safety-demo 'SELECT i."incident_id", i."srti_category", i."rule", e."provider", e."report_id" FROM ROAD_SAFETY.INCIDENTS i JOIN ROAD_SAFETY.INCIDENT_EVIDENCE e ON e."incident_id" = i."incident_id" WHERE i."run_id" = (SELECT MAX("run_id") FROM ROAD_SAFETY.SIM_STATE) ORDER BY i."confirmed_at" DESC'
```

> "Kafka keeps nothing. Exasol keeps everything: what came in from whom, which reports confirmed which incident, and every warning that went out, with its Kafka offset. That's the audit trail a usage policy asks for, in the partner's own database, one query away. Nothing here left this laptop."

Or ask the agent: *"Which warnings did we publish, and which reports confirmed them?"*

### Step 9: A wrong-way driver (2 min, the showpiece)

```bash
./demo sim trigger wrong_way_driver --road A9 --direction south --near Fröttmaning
```

What happens: two carmakers after 5 and 12 s, a wrong-way camera at a ramp when the driver passes it; the incident moves north along the A9 with each update, and clears after about 2 minutes when the driver has left the motorway.

> "Someone is driving north on the southbound A9. Two carmakers see it, a camera at the ramp sees it. The incident follows the driver, every update is published. When the driver is gone, the providers withdraw their events, and the clearing is published too."

### Step 10: Clean up (30 s)

```bash
./demo sim stop-all
```

> "The providers withdraw their reports; the incidents are cleared and the clearing is published."

### Step 11: Close (slides 15–16, 2 min)

> "Iteration one is what you saw: a laptop, the other partners simulated. Iteration two puts the partner's EDC connector in front of the same topics and runs in their own cloud account, a European one if they like. Same topics, same SQL. Trust comes from independent sources, the whole service runs on the partner's ground, and it starts free."

## Optional extras

| What | Command | Say |
|---|---|---|
| An accident that nobody secures | `./demo sim trigger accident --road Leopoldstraße --near "Münchner Freiheit"`, then `./demo sim speed 10` for a minute, back with `./demo sim speed 1` | "After ten minutes without a road operator response the accident area becomes an unmanaged blockage, the seventh category." |
| Fog | `./demo sim trigger fog --near Aubing` | "Three carmakers with fog lights on and road weather stations measuring under 150 m visibility: an area warning." |
| Realistic pace | add `--delay realistic` to a trigger | "In reality a backend takes longer to detect and share; the second report comes after a minute or so." |
| Ask the audience | "Where should it happen?" Let the agent place it (`--dry-run` first if the place is vague). | |

## If something goes wrong

| Symptom | Do |
|---|---|
| No new dots after a trigger | Does `exasol sidecar status -d road-safety-demo` show `road-safety-service` serving? Is T2 (simulator) running? The banner says when each provider reports. |
| An incident doesn't confirm | Only one provider named? Add a second (`--providers A,B`) or `--sensor`. |
| Dashboard frozen | Reload the page. The clock under the title says whether the simulation is stopped. |
| Kafka trouble (no messages in Kafbat UI) | Fallback without Kafka: `./demo sidecar disable`, Ctrl-C T1 and T2, then `./demo service --transport direct` and `./demo sim run --start 07:30 --log events --transport direct`. Everything works, without the Kafka part of the story. |
| Database stopped | `exasol start -d road-safety-demo`, then restart T1 and T2; the service comes back with the database. |
