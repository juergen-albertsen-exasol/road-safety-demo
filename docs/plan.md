# Design and history: live road conditions demo (Data for Road Safety)

This is the working plan the demo was built from, kept as its design record. How to set up and run the demo is in [README.md](../README.md) and [AGENTS.md](../AGENTS.md); the background on the Mobility Data Space and DFRS is in [context.md](context.md).

Status: first version built (sections 1-8); revised on 2026-11-06 and 2026-11-07, see the next sections. Where the revisions and the sections below differ, the revisions apply. The revision of 2026-11-09 is in progress: steps 2 to 7 done.

## Revision 2026-11-09: a reference implementation for DFRS partners

**Status: built** (2026-11-09): steps 2 to 7. The demo runs end to end: partners send SRTI events through Kafka, `scripts/road_safety_service.py` loads, combines and publishes them, the dashboard shows it; README, `docs/architecture.md` and the deck explain it. Open: a real DFRS sample (step 1) and clearing by absence.

*Update 2026-11-10:* the service runs as a sidecar (`./demo sidecar install`), so one `exasol start` brings up the whole node; Kafka advertises its IP on the deployment network instead of the hosts file; the deployment is called `default`. The road operator's sensors report as a steady stream; false alarms come about once a minute and are kept out of each other's grouping radius. The demo has a simple dashboard v1 the agent builds live (`dashboard-v1/`), and v2 suggests diversions around accidents. The stage flow is in `docs/demo-runbook.md`, the deck has five slides.

### The pitch

Exasol shows a **reference implementation** that DFRS partners can take and run themselves: the service that receives safety events from several carmakers and road authorities, combines them into confirmed SRTI warnings, and publishes them back into the data space.

- **Sovereign.** It runs on the partner's own machine or cloud account. Data stays with the partner and is exchanged directly with other partners, like the MDS itself: no central platform and no third-party service in between. Exasol Personal also deploys to the partner's own AWS, Azure, Exoscale or STACKIT account, so a European cloud is possible.
- **Free for the first iteration.** Exasol Personal plus free components: the launcher (MIT), Dash Server (MIT), Apache Kafka and Kafbat UI (Apache 2.0), the Exasol Kafka connector extension (MIT), Python, OpenStreetMap and other open data. Free is the requirement; open source is welcome but not required.
- **Ready to run.** Clone the repo, point your agent at it, say "set up the demo". The rules per SRTI category are SQL with parameters, so a partner can read, change and extend them.

### What doesn't match DFRS today

The current demo gets raw vehicle signals and vehicle positions into Exasol and detects hazards from them. A DFRS service provider or road authority never gets that data: each carmaker detects hazards in its own backend and shares **events** (SRTI category, position, direction, time, validity, confidence), not raw signals or trips. Vehicle positions are personal data and the carmaker's business data. So:

| Today | DFRS reality | Change |
|---|---|---|
| Exasol detects hazards from raw signals | Carmakers send finished events | Exasol **combines** events from several providers and the road operator |
| Evidence = individual vehicles | Evidence = events per provider | Evidence chain per provider and event |
| Reach = vehicles within 2 km heading towards the hazard | Only each carmaker knows where its cars are | Reach **estimated from traffic volume** (`SEGMENT_TRAFFIC`, BASt counts) |
| Incidents exist only as a live query | A service needs stable incidents and a lifecycle | `INCIDENTS` table, and confirmed warnings **published back** |
| Data arrives by direct INSERT | Data arrives through the partner's EDC connector, Kafka extension | Kafka topics between the simulator and Exasol |

### Target architecture

```
SIMULATED PARTNERS                        THE PARTNER'S OWN SETUP (the reference implementation)
                                          ┌──────────────────────────────────────────────────────────┐
carmakers OEM A–F, nav service           │  Kafka (sidecar)          Exasol Personal                 │
  fake SRTI events, steered ──events─────►│  srti.oem-a … srti.nav ──►  SRTI_EVENTS                   │
road operator                             │  datex.road-operator  ──►  SENSOR_READINGS                │
  sensors, warning trailers ─────────────►│                            │ SQL: combine, confirm,       │
                                          │                            ▼ confidence, reach            │
                                          │  srti.confirmed  ◄──────── INCIDENTS, INCIDENT_EVIDENCE   │
                                          │        │                   │                              │
                                          │        │            Dash Server: dashboard (read-only)    │
                                          └────────┼──────────────────────────────────────────────────┘
                                                   └──► (iteration 2: the partner's EDC connector → MDS)
```

The boundary of the box is the point: everything inside is what a partner runs; everything outside is what the data space delivers. In iteration 1 the outside is simulated; in iteration 2 the topics are fed by a real connector.

### Data

**Inbound events** (one Kafka topic per provider, JSON, modelled on the DATEX II SRTI profile until we have a real DFRS sample; field names kept in one place so they are easy to change). Real SRTI messages to model on: Finland's Digitraffic publishes them in DATEX II 3.5 and 3.7 (also over MQTT, CC BY 4.0); the categories follow the SRTI working group's message sets (DATEX II, DENM, TPEG-TEC): event id, provider, SRTI category, position (lat/lon, road segment if known), direction (heading), detection time, valid until, provider confidence, status (new, update, cancel), optional cause (ice, fluid).

**Road operator** (topic `datex.road-operator`): sensor readings and alerts, warning trailers, as today.

**Tables in Exasol:**

| Table | Content |
|---|---|
| `INBOUND_MESSAGES` | Every message as received, raw JSON, with topic, topic id, partition, offset and Kafka timestamp. Written by the loader (built in step 3) |
| `SRTI_EVENTS` | The inbound events parsed from `INBOUND_MESSAGES` (replaces `VEHICLE_SIGNALS` as the input to detection) |
| `SENSOR_READINGS` | As today, now loaded from the road operator topic |
| `INCIDENTS` | Confirmed incidents with a stable id and lifecycle: first event, confirmed, updated, cleared; category, cause, confidence, providers, reach |
| `INCIDENT_EVIDENCE` | Which events and sensor readings support which incident |
| `OUTBOUND_MESSAGES` | Every warning published to `srti.confirmed`, with its offset: the log of what was passed on |
| `DATA_OFFERS`, `CONTRACTS` | Metadata only: which offers this partner has "negotiated" and under which usage policy (road safety only, credit the source). Shown on the dashboard, not enforced |

### Simulator: fake event sources the presenter steers

The sources don't simulate vehicles any more. They send **fake SRTI events** directly, the way a carmaker's backend would after its own detection, and the presenter steers them.

- **Background false alarms:** now and then a single provider reports a hazard at a random spot where nothing happened (as when one car's ESC intervenes on a sharp bend), and no other provider or sensor reports the same. Such a lone report never meets the rule (2 providers, or 1 provider plus a sensor), so it never becomes an incident and is never published. On the map it stays a grey dot, "reported once, not confirmed": it shows that one source alone isn't trusted. It is also the test: a background report that turns red means the rules are too loose.
- **Triggers** keep today's syntax and banner (`trigger oil_spill --road "Fürstenrieder Straße" --near "Laimer Platz"`). A trigger now makes the chosen providers publish events at that spot: new, updates while it lasts, cancel when it ends. Defaults per category say which providers report it, how many events and after how many seconds (for example the oil spill: OEM A after 20 s, OEM D after 50 s, then updates every 30 s).
- **Steering on stage:** options to choose who reports, so the presenter can show the rule at work. `--providers OEM_A` (one provider: not confirmed), then `--providers OEM_A,OEM_D` (confirmed), `--sensor` (the road operator's sensor adds an alert), `--delay` and `--confidence` per provider. Claude maps plain requests to them ("only OEM A sees the oil", "now a second carmaker reports it").
- **Provider profiles** stay plausible: the navigation service reports no accidents (no airbag data), OEM E and F report no weather categories.
- The road operator publishes sensor readings and alerts as today; triggers switch its sensors at the spot.
- `--transport kafka` (default once built) or `--transport direct` (INSERTs into `SRTI_EVENTS`, the fallback without Kafka). `SCENARIO_LOG` and the banners stay, and the combination SQL never reads `SCENARIO_LOG`.

**What goes away:** the 6,000 probe vehicles, `VEHICLE_SIGNALS`, the traffic model's probe speeds and the raw-signal detection. With them go the live traffic colours and the diversions measured from probe speeds. Traffic volume for the reach estimate stays (`SEGMENT_TRAFFIC` from the open data). The old raw-signal version stays in git history.

### Detection becomes combination

The rules stay one SQL statement with parameters, per SRTI category. Typical rule: **confirmed when 2 providers report the same category within radius and time, or 1 provider plus a matching sensor alert**; the road operator alone confirms road works and wrong-way drivers (camera).

- **Matching:** same category, within the category's radius (300 m for slippery road, 100 m for obstacles, a cell for fog and weather), compatible direction, overlapping validity.
- **De-duplication:** updates of the same provider event replace each other; several events of one provider count once.
- **Disagreement:** a provider's cancel removes its support; the incident clears when no supporting event is valid any more, or when providers that saw the spot report nothing (cleared by absence, as today).
- **Confidence:** from the number of providers, their own confidence, sensor support and how fresh the events are.
- **Cause** (ice or fluid): from the providers' cause field, else the road weather station or the DWD temperature.
- **Accident → unmanaged blockage** after 10 min without a road operator response, as today.
- **Reach:** vehicles per hour on the segments within 2 km leading to the incident (`SEGMENT_TRAFFIC` at the simulated time), times the remaining validity: "about 1,400 vehicles in the next 10 minutes".

A scheduled step (every few seconds, run by the loader or a Lua script) turns the live result into `INCIDENTS` rows and writes new or changed incidents to `srti.confirmed`. The live views stay for the dashboard and the agent.

### Kafka as a sidecar

- **Apache Kafka 4.4** (`docker.io/apache/kafka:4.4.0`, the official JVM image) in KRaft mode: one container that is broker and controller, no ZooKeeper, heap capped at 512 MB. Apache 2.0, governed by the Apache Software Foundation, sends nothing home. Configured entirely through `KAFKA_*` variables in the sidecar definition (listeners, single-node replication factors, a fixed `CLUSTER_ID`). Not the `apache/kafka-native` image: faster, but Apache calls it experimental and not for production.
- **Kafbat UI** (`docker.io/kafbat/kafka-ui:v1.5.0`, Apache 2.0) as a second sidecar on `http://127.0.0.1:18080`: shows the topics and the messages arriving. Useful on stage ("here is what the carmakers send").
- **Why not Redpanda,** which step 2 used first: the sovereignty message. Redpanda is source-available under the Business Source License of one US vendor, and its default configuration reports cluster metadata to the vendor (`enable_metrics_reporter = true`, to `m.rp.vectorized.io`). Its advantages (no JVM, fast start, small) turned out small next to KRaft-mode Kafka. Kafka uses a bit more memory, see below.
- Added as user entries `kafka` and `kafka-ui` in the deployment's `sidecars.yaml` by `scripts/setup_kafka.py`. This is documented and supported in the launcher (`user-docs/content/sidecars.md` in exasol-personal): saved definitions are authoritative, user-added entries count as enabled, `exasol start` starts them, `exasol sidecar status kafka` reports them. `exasol sidecar enable` only accepts catalog names, so setup edits the file instead.
- Networking: the database reaches the broker as `kafka:9092` inside the deployment network; the simulator and loader on the laptop use the published port `127.0.0.1:19092`. Two listeners (`INTERNAL`, `EXTERNAL`), each advertised with its own address, plus the controller on `localhost:9093` inside the container. Readiness: a `tcpSocket` probe on the published port.
- On macOS the sidecars run in the database VM and take memory from it. Measured on 2026-11-09 in a VM with 24 GB and 2 CPUs: Kafka about 370 MB, Kafbat UI about 310 MB (Redpanda was about 290 MB, its Console about 50 MB). `exasol start` with both sidecars, database already stopped: 18 s.
- Images from `docker.io/...`. A partner who takes sovereignty seriously mirrors them into a registry of their own.
- No TLS or authentication on the broker: it is reachable only on loopback and the deployment network. Data between partners goes through the EDC connector (iteration 2), never through a published Kafka port.
- **Loading into Exasol (built, step 3):** the Exasol Kafka connector extension 2.0.1 (MIT; Java `SET` scripts `KAFKA_CONSUMER`, `KAFKA_IMPORT`, `KAFKA_METADATA` in schema `KAFKA_EXTENSION`; JAR in BucketFS; needs the Java script language container). The database pulls the messages itself; `scripts/kafka_loader.py` on the laptop only decides when: every 3 s it compares Kafka's end offsets with what is loaded and imports only topics with new messages, up to 4 in parallel. Each import costs 2 to 3 s (JVM start), so a topic with nothing new is skipped.
- **Measured:** 2 to 4 s from send to row in Exasol with Apache Kafka (3 to 5 s with Redpanda) (`POLL_TIMEOUT_MS = 1000`, `CONSUME_ALL_OFFSETS = true`).
- **The connector resumes per table, not per topic:** it reads `MAX(KAFKA_OFFSET)` per partition from the table named in `TABLE_NAME`. So each topic gets its own staging table in `KAFKA_STAGE` (only the last row per partition is kept); after each import the new rows are copied into `ROAD_SAFETY.INBOUND_MESSAGES`. Topic names stay plain (`srti.oem-a`); a run id in the name, as planned first, would not have helped.
- **The UDF sandbox has no DNS** (empty `resolv.conf` and `hosts`), so `kafka` doesn't resolve in a UDF, while the IP works. The Java scripts read a hosts file from BucketFS (`%jvmoption -Djdk.net.hosts.file=/buckets/bfsdefault/default/kafka/hosts`); the loader writes it with the broker's current IP (`podman inspect` in the VM through `exasol shell host`) at start and after an outage. The broker keeps advertising `kafka:9092`. The IP does change: it moved from 10.89.0.6 to 10.89.0.3 in the restart test. Cause: the official script language containers ship empty `etc/resolv.conf` and `etc/hosts`, while the working configuration sits unused at `/conf` in the sandbox (its resolver, Podman's `10.89.0.1`, does know `kafka`). Reported as [exasol/exasol-personal#464](https://github.com/exasol/exasol-personal/issues/464); once fixed, the hosts file and the `%jvmoption` can go.
  *Update 2026-11-10:* the hosts file is gone. With the service as a sidecar, nothing could write it (a sidecar has no access to BucketFS). Instead Kafka advertises its own IP on the deployment network (a `command` in its sidecar entry sets `KAFKA_ADVERTISED_LISTENERS` from `hostname -i` before the image's entrypoint), and the loader passes that IP as the bootstrap server: from DNS inside the sidecar, from `podman inspect` on the laptop. The `%jvmoption` is removed from the scripts.

### Persistence: sidecars keep nothing, Exasol keeps everything

Sidecars can't keep files across restarts (no volumes; `exasol stop` removes their containers). So **Kafka is only the transport, and Exasol is the system of record.** Everything that must survive a restart lives in Exasol:

| What | Where in Exasol | After a restart |
|---|---|---|
| Every inbound message | `INBOUND_MESSAGES`, with topic, topic id, partition and offset | Kept. Topics start empty. Messages Kafka acknowledged but the loader hadn't imported yet (the last few seconds before `exasol stop`) are lost: 4 of 69 in the restart test with Apache Kafka. To avoid it, stop the producers first and wait until the loader has nothing left, then `exasol stop` |
| The loader's position per topic and partition | The last row per partition in the topic's staging table (`KAFKA_STAGE.<TOPIC>`), and the topic id in `KAFKA_STAGE.TOPICS` | **Resets by itself:** a recreated topic has a new topic id (Kafka assigns a UUID); the loader then empties the staging table and loads from offset 0 again. Tested on 2026-11-09 with a producer running through `exasol stop` and `start`, with Redpanda and again with Apache Kafka: each Kafka position loaded once, the loader reconnected by itself and found the new topic id. Missing were only messages from the last seconds before the stop (see above) |
| Incidents and their evidence | `INCIDENTS`, `INCIDENT_EVIDENCE` | Kept; active ones expire or clear by the normal rules |
| What was published | `OUTBOUND_MESSAGES` | Kept. On start, still active incidents are published to `srti.confirmed` again, so subscribers see the current state |
| Triggers, simulated time | `SCENARIO_LOG`, `SIM_STATE` | As today (runs don't resume) |
| Offers and contracts | `DATA_OFFERS`, `CONTRACTS` | Kept |
| Sidecar configuration | Not in files: arguments and environment in `sidecars.yaml` (Kafka listeners, Kafbat UI's broker address) | Recreated from the definition |

This is also a point for the pitch: the audit trail a usage policy may require (what came in from whom, what went out to whom) is in one database, queryable with SQL.
- Once a Kafka-compatible entry exists in the launcher's sidecar catalog, setup switches to `exasol sidecar enable`.

### Connector (EDC)

Not run in iteration 1: heavy, invisible on stage, and it needs an MDS identity. The deck shows where it sits, the dashboard shows the `DATA_OFFERS` and `CONTRACTS` metadata. Iteration 2: an EDC connector as a further sidecar, connected to the MDS test environment, feeding the same topics. Persistence: EDC keeps contract negotiations and agreements in memory by default (lost on restart, so contracts would be negotiated again). Its SQL stores are written for the PostgreSQL dialect and only tested there, so Exasol as EDC's store is ruled out. Decision for iteration 2: renegotiate contracts on every start, or run the connector with a small PostgreSQL outside the sidecar model if renegotiating proves too slow or not allowed.

### Dashboard

- Inbound events as dots coloured by provider (instead of vehicles and raw signals); unconfirmed single reports grey; sensors as today; confirmed incidents red with a hover showing the evidence per provider and the reach estimate.
- An "outbound" ticker: the warnings published to `srti.confirmed`.
- A panel with the negotiated offers and their usage policies.
- Background: roads coloured by modelled traffic volume at the simulated time (from `SEGMENT_TRAFFIC`), instead of live probe speeds.
- Sliders: minimum providers, sensor may replace a provider (yes/no).

### Iterations

| | Iteration 1 (this plan) | Iteration 2 | Production at a partner |
|---|---|---|---|
| Runs on | A laptop, Exasol Personal | The partner's own cloud account, Exasol Personal (AWS, Azure, Exoscale, STACKIT) | The partner's infrastructure, sized for real volumes |
| Inbound | Simulated carmakers and road operator → Kafka | An EDC connector with the Kafka extension, MDS test environment, real DFRS sample data | The partner's DFRS contracts |
| Outbound | `srti.confirmed` topic | Published as a data offer through the connector | As iteration 2 |
| Cost | Free | Free software, the partner's cloud costs | To be discussed with Exasol |

### Order of work

1. Event format: get a DFRS sample message from MDS; until then define the JSON from real DATEX II SRTI messages (Digitraffic).
2. Kafka sidecar: entry in `sidecars.yaml`, check from the laptop and from the database (`kafka:9092`), memory in the VM; optionally a web UI. Setup step in `AGENTS.md`. **Done** (Apache Kafka and Kafbat UI).
3. Loading: try the Exasol Kafka connector extension on Exasol Personal (Java UDFs, JAR in BucketFS); otherwise the Python consumer. Measure the time from message to row with a low `POLL_TIMEOUT_MS`. Test a restart: `exasol stop` and `start` with the simulator running, then check that no message is skipped or loaded twice and that active incidents are published again. **Done** except the republishing of incidents, which comes with step 5.
4. Simulator: fake event sources per provider, background reports, trigger defaults per category, the steering options (`--providers`, `--sensor`, `--delay`, `--confidence`), road operator topic, `--transport`. Remove the probe vehicles. **Done** (2026-11-09):
   - Event format in `scripts/srti_events.py` (all field names in one place): DATEX II record type and value per SRTI category, as Digitraffic's DATEX II 3.7 feed uses them (`GeneralObstruction`/`unprotectedAccidentArea`, `PoorEnvironmentConditions`, `probabilityOfOccurrence`, validity from and until). The message never contains the scenario id.
   - Who can report what and the defaults per scenario (providers, delays, validity, update interval) are in the same module; the trigger refuses impossible combinations.
   - The road operator's `--sensor` report names a sensor of the right kind within 1 km (road weather station, camera, warning trailer), otherwise a mobile one (`MS-<n>`).
   - Sensor readings go to `datex.road-operator` too; loop detectors and cameras take their vehicle counts and speeds from the traffic model (`road_traffic.vehicles_on`).
   - Tested: every scenario type; steering (one provider, a second one added, the sensor added, an impossible provider refused); cancels when a scenario stops; `--transport direct`; 10x speed with eight scenarios at once: 3,362 messages sent, 3,362 loaded, each Kafka position once.
   - Found on the way: the wrong-way driver ends when it reaches the edge of the road network (A9 north of Garching: after about 1.5 km); the loader shared a database connection between threads with more topics than workers (fixed: a connection pool).
5. Combination SQL, reach from traffic volume, `INCIDENTS`, `INCIDENT_EVIDENCE`, publishing to `srti.confirmed`. Test all trigger types again (same list as the 2026-11-07 test), each with one provider (not confirmed) and two (confirmed). **Done** (2026-11-09):
   - `scripts/road_safety_service.py` runs load (Kafka connector), parse (`roadsafety/parse.sql`, a watermark per topic id and partition in `PARSE_STATE`), combine (`roadsafety/combine.sql`), keeps `INCIDENTS` and `INCIDENT_EVIDENCE`, and publishes new, update and cleared to `srti.confirmed` (`OUTBOUND_MESSAGES`), every 3 s. After a restart it publishes the active incidents again; incidents of an ended run are cleared.
   - Grouping in SQL: reports of one category within the category's radius (300 m slippery road, 150 m obstacle, 250 m accident, 400 m road works, 2.5 km wrong-way driver, 5 km fog, 6 km storm), heading the same way for directional categories; each report joins the group of its earliest neighbour. Confirmed: 2 providers, or 1 plus a road operator sensor, or the road operator alone for road works and wrong-way cameras. About 0.2 s per run.
   - Sensor alerts are evidence too (low friction on a frozen surface, poor visibility, wrong-way camera, stationary vehicle, warning trailer).
   - Tested live: one provider stays a single report; a second confirms it (10 s after its first report); stop clears it; the wrong-way driver moves and clears when gone; the accident becomes an unmanaged blockage after 10 min; road works confirmed by the trailer alone; false alarms never confirmed; restart republishes.
   - Not done: clearing by absence (providers passing the spot without reporting it). Incidents clear when no valid report is left (cancel or expiry).
6. Dashboard: events by provider, outbound ticker, offers panel, traffic volume background. **Done** (2026-11-09): map with the valid events as dots in the provider's colour (grey: single reports, not confirmed), road sensors, confirmed incidents with their evidence, reach and published id; background: traffic volume per main road at the simulated hour; incident cards; the outbound ticker; offers and contracts; sliders for the providers needed and whether a sensor may replace one (what-if: the service keeps the default rule); an event stream tab with every message, classed. The queues, diversions and probe-speed traffic state of the raw-signal version are gone.
7. Docs: README (the pitch), `docs/context.md` (the mapping table and "what the demo doesn't show"), AGENTS.md (setup table, troubleshooting), the deck (setup, detection, a slide "what you receive and what you send", the iterations). **Done** for the repo (2026-11-09): README (the pitch: sovereign, free components), the new `docs/architecture.md` (the node, data flow, licenses, restarts, measurements, limits), `docs/context.md` (the mapping table, what the demo doesn't show), AGENTS.md. The deck is updated separately.

### Open questions

Checked on 2026-11-09:

- **Kafka connector license:** MIT. Release 2.0.1 of 26 Oct 2026, actively maintained.
- **Offsets in the Kafka connector:** read from the target table (`KAFKA_PARTITION`, `KAFKA_OFFSET`) across all topics in it; no reset option. Solved with a staging table per topic and the topic id (see "Kafka").
- **Does the connector run on Exasol Personal?** Yes, with the Java script language container, and Kafka named by its IP (see "Kafka").
- **Latency of loading:** 2 to 4 s from send to row. What remains is the event delays per provider and the combination step (step 5).
- **Redpanda's license:** would have been fine for partners, but Redpanda was replaced by Apache Kafka for the sovereignty message (see "Kafka"). No license question left for the broker.
- **EDC state (iteration 2):** in memory by default; SQL stores are PostgreSQL only. See "Connector".
- **Memory in the macOS VM:** no problem, see "Kafka" (measured in step 2).

Still open:

- The real DFRS event format and fields: no public sample; ask MDS. Until then, model on Digitraffic's SRTI messages.
- Latency end to end: event delays plus loading (3 to 5 s) plus combination; on stage the confirmation should come within about a minute of the trigger. Measure in step 5.
- Duplicates from producers: a send that times out and is retried can reach Kafka twice (seen in the restart test). The combination must de-duplicate by event id, as planned.

## Revision 2026-11-07: safety events anywhere, derived from sensors

**Status: built.** The demo is about the eight DFRS / SRTI categories, not about everyday traffic. Congestion is day-to-day, not a hazard: the congestion list, its reasons (bottleneck, roadworks, high volume) and the "congestion likely" warnings are gone from the dashboard. Traffic stays as the background colour, and the queue behind an accident, the reach of a warning and the diversions stay.

**Story on stage.** The presenter (or the audience: "name me an event") says what happens and where, in plain words, for example "a wrong-way driver on the A9 southbound near Garching". Claude turns it into one trigger, runs it and shows the banner. On the incident map the sensors turn red and the incident appears with its evidence chain on hover: SRTI category, first signal, confirmation time, how many signals from how many vehicles of which providers, and which road sensors.

**Trigger anywhere.** `simulate_road_safety.py trigger <event> --road <road> --near <place> --direction <dir> [--at lat,lon] [--dry-run]`:

| Event (aliases) | Where it can go | SRTI category detected |
|---|---|---|
| `wrong_way_driver` (wrong_way) | a one-way carriageway of a motorway, trunk or primary road (motorways, Mittlerer Ring, Leopoldstraße ...) | Wrong-way driver |
| `accident` (crash) | any main road, motorway to tertiary | Unprotected accident area, after 10 min Unmanaged blockage of a road |
| `slippery_road` (ice) | any road (about 500 m), an Isar bridge as a whole | Temporary slippery road (cause: ice) |
| `oil_spill` (oil) | any road, about 500 m in the direction of travel | Temporary slippery road (cause: fluid) |
| `debris` (obstacle), `animal` | any road; an animal away from forests and parks gets a note | Animal, people, obstacles, debris on the road |
| `roadworks` (works, new) | any road, about 300 m in one direction; a road operator warning trailer appears as a sensor | Short-term road works |
| `fog` (visibility) | about 6 x 5 km around a place | Reduced visibility |
| `storm` (weather) | a band moving west to east across a place in 20 min (`--area west-east`: the whole city) | Exceptional weather conditions |

- `--road`: a road number (`A9`, `A 99`, `B2R` or `Mittlerer Ring`) or a street name. `--near`: a place from `ROAD_DATA.PLACES` (805 OSM names: suburbs, villages, the 59 motorway junctions such as "Kreuz München-Nord" or "Garching-Süd", stations, landmarks such as "Allianz Arena"), or a street. `--direction`: north/south/east/west, in/out (of the city), or "toward <place>" (also outside the area: Salzburg, Nürnberg, Stuttgart, Lindau, Garmisch, the airport ...).
- The spot is the fitting segment closest to the place. If the road doesn't pass within 4 km, or the event doesn't fit the road, the command says so with a hint; Claude then proposes something close instead of forcing it. Notes (a minor street with few probes, an animal far from a forest) are printed with the banner.
- **How Claude maps a request:** pick the event, then road, place and direction from the words; run it with `--dry-run` first when the wording is ambiguous, and say where it lands ("the A 9 southbound near Garching-Süd"); then trigger. One trigger per event; `--stop` or `stop-all` ends it. The named areas of the first version (`--area a99-nord`, `isar-bridges` ...) still work.

**Sensors.** 158 instead of 61: 35 road weather stations (Isar bridges plus motorway and main road bridges), 69 wrong-way cameras at motorway entries (named after the nearest junction), 12 tunnel cameras, 42 loop detectors (motorways and Mittlerer Ring), plus a warning trailer per active road works site. On the map, every sensor shows its newest reading on hover; a sensor whose reading is an alert (frozen surface with low friction, visibility below 150 m, wrong-way vehicle, stationary vehicle, road works) is drawn red.

**Data and code.** `ROAD_DATA.PLACES` (new part `prepare_road_data.py --only places`); `road_scenarios.locate()` and `build()`; `SENSOR_READINGS.works_active`; a `roadworks` rule in `roadsafety/hazards.sql` (the trailer confirms it at once, braking vehicles add evidence; braking within 1 km of a trailer is not an obstacle). `setup` was re-run (new sensors and column; vehicles are the same with the same seed).

**Tested** at x3 from 07:30: wrong-way driver A9 near Garching-Süd (confirmed after 14 s, camera plus the vehicle), road works A99 near Kreuz München-Nord (28 s, trailer plus braking vehicles), debris on the Mittlerer Ring at the Donnersbergerbrücke (18 s), ice on Leopoldstraße (15 s, cause ice), accident on Leopoldstraße (1 s after the airbag signals), oil on the A96 near Blumenau, fog around Aubing (2 road weather stations), storm near Riem, animal on the A95 near Kreuzhof, wrong-way driver on the Mittlerer Ring (followed along the ring, 2 wrong-way cameras).

**Fixed during the test.** A storm at a place was a 20 km band and showed up as three weather incidents: now about 10 km, one incident. A wrong-way driver that reached the end of the A8 stopped there and the braking around it was detected as an obstacle: now the driver leaves the road at the end of the carriageway, and braking within 1.5 km of a wrong-way signal is not an obstacle.

**Open.** Detection on quiet side streets is slow (few probes). The obstacle can briefly clear and come back while vehicles pass the spot.

## Revision 2026-11-06: traffic, congestion and incidents

**Status: built, then partly withdrawn on 2026-11-07** (the congestion list, reasons and warnings are no longer shown; see above). After seeing the first dashboard, the presenter changed the story. The demo now shows live traffic: congestion with the reasons derived from the data, warnings of congestion that is likely to form, rush hour building up at Munich's real bottlenecks, and two accidents plus an oil spill triggered by hand, with diversion routes. Sections 1-8 below describe the first version; what this revision changes is listed here, the rest stays (open data, `ROAD_SAFETY` stream, visible triggers, detection in SQL, read-only dashboard).

### Story on stage

| # | Presenter | What happens |
|---|---|---|
| 1 | "Show me the traffic in Munich right now." | Map of the main roads coloured by speed (free, slow, congested, standstill). It is 06:00 on a weekday: traffic is light. Today's real roadworks are on the map. |
| 2 | (the simulation runs faster for a few minutes) | Rush hour builds up at the known hotspots: Mittlerer Ring Süd (Brudermühl, Heckenstallerstraße, Candidplatz), Mittlerer Ring Nord (Petuelring, Georg-Brauchle-Ring), Landshuter Allee and Donnersbergerbrücke, the A99 interchanges, the A8 Ost and A9 into the city, the roadworks at Fürstenrieder Straße/A96 and A95/A995. Each congestion has a reason on the page: "rush-hour volume at a bottleneck", "roadworks: lane closed", later "accident". Warnings appear where congestion is likely in the next minutes. |
| 3 | "Simulate an accident on the A99 at Kreuz München-Nord." | Claude explains and triggers it. The accident is detected within seconds from airbag and hazard-light signals, a warning appears at once, the queue grows behind it on the map, and the page proposes diversion routes with their travel time against the route through the queue. |
| 4 | "And a second one on the Mittlerer Ring." | Same in the city: diversion over parallel streets. |
| 5 | "Vehicles report an oil spill on ..." / "Simulate an oil spill on ..." | Temporary slippery road, detected from ESC/ABS and wheel slip concentrated on one short stretch in dry, mild weather: the page names it "fluid on the road (oil)", not ice. |
| 6 | "Stop all simulations." | Accidents cleared, queues dissolve, diversions disappear. |

### What changes

**Traffic model (simulator).** The 3,000 simulated vehicles become *probe vehicles*: the connected share of the traffic. Behind them, the simulator keeps a traffic volume per road segment and time: a real rush-hour curve (BASt hourly count data, stations near Munich), a relative volume per road (BAYSIS road traffic census 2021 where it covers the road, road class elsewhere), more inbound traffic in the morning. Each segment has a capacity (lanes, road class); hotspots get the bottleneck capacity that makes them congest first; roadworks reduce capacity where they are; an accident closes lanes. Speed per segment follows volume against capacity, queues spill back to the upstream segments, and the probe vehicles drive at that speed. So the probes measure the congestion, and the SQL sees it only through their signals, as in reality.

**Time.** The simulation starts at 06:00 on a dry, mild weekday. A new command changes the speed while it runs: x10-x20 for the build-up, x1 for the incidents.

**Data.** New: the BASt hourly counts (rush-hour curve), BAYSIS traffic census (volumes) if the WFS works and its license is confirmed, and the hotspot list ([research/munich-congestion-hotspots.md](../research/munich-congestion-hotspots.md)). The city roadworks and Autobahn roadworks are matched to the road segments and are always on (no trigger). The simulation date should be close to today, so today's roadworks fit.

**Detection in SQL.**
- *Traffic state* per segment from the probe speeds of the last minutes against the free-flow speed: free, slow, congested, standstill.
- *Congestion* = connected congested segments, with length, delay and a *reason* derived by SQL: accident or other hazard in or just downstream of it, roadworks on it, known bottleneck plus rush-hour volume, else "high volume".
- *Warnings of likely congestion*: speeds falling over the last minutes, a queue growing towards the next junction, roadworks or an incident ahead of rising volume.
- *Hazards* as before: accident area, slippery road (cause ice or fluid, from the weather), plus the other types.

**Diversions.** When an accident is confirmed, routes are computed on the road graph from a point a few kilometres before it to a point after it, without the blocked segments, with the travel times the probes currently measure. Proposal: a Python UDF in Exasol (Dijkstra over the main road network), so the routing runs in the database next to the data. The page shows the 1-2 best routes and the time saved against the route through the queue.

**Dashboard.** Rebuilt around traffic: main roads coloured by traffic state, congestion list with reasons and delay, warnings, incidents with diversion routes on the map, KPIs (congested km, total delay, incidents, vehicles that would be rerouted), simulation band and timeline as before.

**Scenarios.** Main triggers: `accident` (two areas: A99 Kreuz München-Nord and one on the Mittlerer Ring), `oil_spill` (new). The others (fog, storm, wrong-way driver, animal, debris, black ice) stay available but leave the main flow.

### Open decisions

1. Simulation day: a dry weekday close to today, or keep 2025-01-10 (frost, which would make the oil-spill story ambiguous)?
2. Diversion routing: Python UDF in Exasol, or in the dashboard?
3. Areas: which Mittlerer Ring spot for the second accident, and where the oil spill happens.

## Goal

Show live on a Munich city map how safety-related traffic information is created from vehicle and infrastructure data, in line with the Data for Road Safety (DFRS) use case of the Mobility Data Space: many providers share signals, and a hazard is confirmed and passed on to as many vehicles as possible.

- **All base data is real open data**: the complete road network, roadworks, motorway events, weather and accident history.
- **The only synthetic part is the vehicle and sensor stream.** No open source of raw vehicle signals exists, and DFRS data isn't published under an open license.
- **The presenter triggers hazards on purpose, and the trigger is visible.** The audience always sees which hazards were injected by the simulation, and then watches the data pipeline detect and confirm them.

## Event types

The eight categories on the DFRS slide are the EU SRTI categories (Safety-Related Traffic Information, Delegated Regulation (EU) No 886/2013). They are used 1:1:

| SRTI category | Code in the demo |
|---|---|
| Temporary slippery road | `slippery_road` |
| Animal, people, obstacles, debris on the road | `obstacle` |
| Unprotected accident area | `accident_area` |
| Short-term road works | `short_term_roadworks` |
| Reduced visibility | `reduced_visibility` |
| Wrong-way driver | `wrong_way_driver` |
| Unmanaged blockage of a road | `unmanaged_blockage` |
| Exceptional weather conditions | `exceptional_weather` |

## 1. Open data (real)

Check every source before loading: endpoint, license, coverage of Munich, how current it is. Loading: `scripts/load_road_data.sh` with exapump (see "Platform").

| Layer | Source | License | Use |
|---|---|---|---|
| Road network, complete | OpenStreetMap, Geofabrik extract Oberbayern, clipped to the city boundary plus the motorway ring | ODbL | Every street segment with road class, one-way, speed limit, `bridge`, `tunnel`, name |
| Districts | `DISTRICTS`, City of Munich open data (`data/source/districts.csv`) | dl-de/by-2.0 | District rollups |
| City roadworks | `ROADWORKS`, City of Munich open data (`data/source/roadworks.csv`) | dl-de/by-2.0 | Real roadworks on the map, and the basis for `short_term_roadworks` |
| Motorways (A8, A9, A92, A94, A95, A96, A99) | Autobahn GmbH, Mobilithek offer "Die Autobahn App", JSON endpoint verkehr.autobahn.de | GeoNutzV | Real roadworks, closures and warnings, shown as a separate layer |
| Weather | DWD Open Data: 10-minute and hourly station values (München-Stadt, München-Flughafen) | CC BY 4.0 | Air temperature, precipitation, visibility, gusts. They set the background conditions and make the scenarios plausible |
| Accident history | Unfallatlas of the Statistical Offices, accidents with coordinates, 2016–2025 | dl-de/by-2.0 | Accident hotspots per segment. Background accidents are weighted toward them |
| Wildlife risk | OSM forest and park edges (Forstenrieder Park, Perlacher Forst, Aubinger Lohe …) | ODbL | Where `obstacle` (animal) is plausible |

Output: a script `scripts/prepare_road_data.py` that downloads, clips and converts the sources into CSV files for a schema `ROAD_DATA`, in the same style as `scripts/prepare_dataset.py`. The research notes, with source, license, date and limits per dataset, go in `research/road-data-sources.md`.

## 2. Synthetic stream (schema `ROAD_SAFETY`)

| Table | Content |
|---|---|
| `DATA_PROVIDERS` | Fictional participants with generic names: OEM A–F, a road operator, a navigation service. No real brand names, so nobody assumes real data is behind them |
| `VEHICLES` | About 3,000 synthetic connected vehicles: provider, vehicle type, current position |
| `INFRA_SENSORS` | About 60 virtual sensors at real places taken from OSM: road weather stations on the Isar bridges, cameras and loop detectors in the Mittlerer Ring tunnels, motorway entries |
| `VEHICLE_SIGNALS` | Raw stream: ts, vehicle, lat/lon, segment, heading, speed, ESC/ABS active, hard braking, hazard lights, fog lights, wiper level, outside temperature, airbag |
| `SENSOR_READINGS` | Road surface temperature, friction, visibility, wrong-way detection, stationary vehicle |
| `SCENARIO_LOG` | Every triggered scenario: id, type, area, started, ended, triggered by. This is the ground truth of what was injected |
| `SAFETY_EVENTS` (view) | Confirmed hazards, derived by SQL from signals and readings |

### Simulator `scripts/simulate_road_safety.py`

- Uses pyexasol from the project `.venv` with the deployment's `sys` login (password from the deployment's `secrets.json`, never printed), and writes only to `ROAD_SAFETY`. Analysis SQL by the agent stays read-only: the agent never writes, except through the simulator when the presenter asks for a trigger.
- **Background mode** (`run`): vehicles move along OSM segments and send normal signals every 2 s in micro-batches. Background noise is included on purpose (single hard brakings, single ESC interventions), so the confirmation rule has something to filter out.
- **Weather** follows a real DWD day, chosen with `--weather-day`, for example a frost morning.
- `--seed` makes runs reproducible. `--speed` speeds up time.
- `--record` / `--replay` keep a fallback recording in case something fails on stage.

## 3. Triggering a hazard in the demo, visibly

The presenter triggers a hazardous condition on purpose. It must always be obvious that the condition is simulated, and what the system detected by itself.

### How the trigger works

Command, always run by Claude when the presenter asks for it (see Decisions):

```
python scripts/simulate_road_safety.py trigger slippery_road --area isar-bridges
```

1. The command prints a clear banner: `SIMULATION: injecting "temporary slippery road" on 6 Isar bridges, 07:42:05`.
2. It writes a row into `SCENARIO_LOG`.
3. From then on, the simulator changes the behavior of the vehicles and sensors in that area. For slippery road: cars crossing the bridges have ESC/ABS interventions and slow down, outside temperature reads around -1 °C, and the road weather sensors on the bridges report low friction and a surface temperature below 0 °C.
4. **The trigger injects signals, never hazards.** Nothing is written into `SAFETY_EVENTS`. Whether a hazard appears depends only on the detection SQL.

Before every trigger, Claude explains in a few short lines what it is about to do, for example:

> **This is a simulation.** I'm injecting "temporary slippery road" on the 6 Isar bridges: from now on, cars crossing them report ESC/ABS interventions and an outside temperature around -1 °C, and the 6 bridge sensors report low friction. This writes synthetic signals into `ROAD_SAFETY`. It doesn't create a hazard: whether one shows up on the map depends only on the detection SQL.

After the command, Claude shows its banner line and says what to watch on the map.

### Available scenarios

| Scenario | Area (from OSM) | Injected signals | Expected hazard |
|---|---|---|---|
| `slippery_road` | Isar bridges | ESC/ABS, temperature around -1 °C, low friction at the bridge sensors | Temporary slippery road |
| `accident` | a segment of the Mittlerer Ring | Airbag + hazard lights from 1–2 vehicles, then hard braking upstream | Unprotected accident area; it becomes an unmanaged blockage if no road operator signal arrives within 10 min |
| `wrong_way_driver` | A9 exit Freimann | One vehicle moving against the OSM one-way direction, confirmed by a camera | Wrong-way driver, moving segment by segment |
| `fog` | north of the city | Fog lights, speed drops, visibility sensor reads < 150 m | Reduced visibility |
| `storm` | a band moving west to east | Wipers at max, gusts | Exceptional weather conditions |
| `animal` | Forstenrieder Park edge | Emergency braking from several vehicles at one spot | Obstacle (animal) on the road |
| `debris` | A96 | Swerving and braking at one point | Obstacle (debris) on the road |

`trigger ... --stop` and `reset` end a scenario.

### What the map shows

Injected and detected are always visually separate:

- **Simulation band** at the top of the page while a scenario runs: "SIMULATION ACTIVE: temporary slippery road, Isar bridges, since 07:42:05". The page reads it from `SCENARIO_LOG`.
- **Injected area**: a dashed outline on the map, labeled "simulated".
- **Detected hazard**: a filled icon and red segments, only once the SQL has confirmed it.
- **Timeline** under the map: trigger time, first raw signal, confirmation, and the time between them ("confirmed after 48 s from 4 vehicles of 3 providers").
- Background noise that was *not* confirmed can be switched on as grey dots, to show what the rule filters out.

## 4. Detection in SQL (the Exasol part)

A view `ROAD_SAFETY.V_HAZARDS_LIVE` turns raw signals into confirmed hazards. Rule per type, for example slippery road:

- at least 3 vehicles with ESC/ABS on the same or adjacent segments within 300 m and 5 minutes,
- from at least 2 different providers,
- or 1 sensor reading (friction below a threshold, surface temperature at or below 0 °C) plus 1 vehicle.

Every hazard gets a confidence value, the number of vehicles and providers that reported it, first seen, last confirmed, and an expiry time after which it is dropped without new signals.

The thresholds are parameters (pyexasol placeholders), so they can be changed on the page and in prompts, e.g. "require 3 providers".

**Reach**: the number of vehicles within 2 km driving toward the hazard, which would receive the warning. This is the DFRS goal on the slide: maximizing the reach of safety-related information.

## 5. Dashboard `munich-road-conditions` (Dash Server)

- **Map**: OSM segments colored by condition (ok, caution, danger), hazard icons per SRTI type, the wrong-way driver as a moving marker with a trail, sensors as small squares. Layers you can switch on: real city roadworks, motorway events, accident hotspots. `uirevision="keep"`.
- **Live**: `dcc.Interval` every 3 s, one bounded query per tick (last 30 min).
- **KPI row**: active hazards, vehicles reporting, signals per second, median time from first signal to confirmation, reach.
- **Side panel**: event ticker (type, street, age, confidence, providers), and the contribution per provider, which shows that no single provider would have confirmed most hazards alone.
- **Controls**: event types, providers ≥ N, time window, replay slider.
- Simulation band and timeline (section 3).
- "SQL executed" foldout and a caveat line: "Vehicle and sensor data is synthetic and marked as simulation. Road network, roadworks, weather and accident history are open data."
- Optional second step: an Ask panel, an assistant on the page ("What is happening on the Mittlerer Ring?").

## 6. Demo flow

| # | Presenter | What happens |
|---|---|---|
| 1 | "Which open data do we need for road conditions in Munich?" | Sources, licenses and limits (section 1) |
| 2 | "Show me the road conditions in Munich live on a map." | Dashboard: normal traffic, real roadworks, no hazards. Background noise visible as grey dots |
| 3 | "Simulate black ice on the Isar bridges." | Claude announces the simulation and runs the trigger. The simulation band appears, the dashed area appears, and within a minute the bridges turn red. Claude shows the SQL that confirmed it |
| 4 | "Only count a hazard if 3 providers confirm it." | Threshold changes; weaker hazards disappear |
| 5 | "Simulate a wrong-way driver on the A9. Who needs to be warned?" | Moving marker, reach query |
| 6 | "Stop all simulations." | Hazards expire one after another, the map returns to normal |

## 7. Repo layout

- `research/road-data-sources.md`: the open data research
- `scripts/prepare_road_data.py`: download and convert the open data into `ROAD_DATA`
- `scripts/load_road_data.sh`: load the CSVs with exapump
- `scripts/simulate_road_safety.py`: background stream, triggers (`road_scenarios.py`, `road_traffic.py`)
- `roadsafety/*.sql`: schema, detection, reach, evidence, traffic and diversion templates
- `dashboard/`: the Dash app `munich-road-conditions`; `scripts/build_dashboard_queries.py` generates its composite queries from `roadsafety/`
- `AGENTS.md`: setup and demo rules for the agent (the simulator writes; the agent only reads, except the triggers the presenter asks for)

## 8. Order of work

1. Check the open data sources and write `research/road-data-sources.md`.
2. `prepare_road_data.py`, then load `ROAD_DATA` with `scripts/load_road_data.sh`. Done: 10 tables loaded on 2026-11-06.
3. `ROAD_SAFETY` schema and the simulator in background mode. Done 2026-11-06 (`roadsafety/schema.sql`, `scripts/simulate_road_safety.py setup|run|status|reset`):
   - 3,000 vehicles from 7 providers (OEM A-F, a navigation service), 60 sensors (13 Isar bridges, 12 tunnels, 25 motorway entries, 10 loop detectors on motorways).
   - Each vehicle reports every 10 s of simulation time and at once on an event; the simulator writes a batch every 2 s. About 300 signals per second at speed x1.
   - The navigation service shares no ESC/ABS, airbag or environment values (NULL), and OEM E and F share no environment values: that's why several providers are needed.
   - Extra table `SIM_STATE`: one row per run with the simulation time, so the dashboard knows "now".
   - Test run of 10 minutes at x10: 234 hard brakings, 49 ESC, 40 ABS, 39 hazard lights, and no false black-ice alarm under the rule in section 4.
   - pyexasol's bulk import fails on the version string of Exasol Personal (`2026.2.0-nano.8`), so the simulator writes with multi-row INSERTs (3,000 rows in 0.2 s).
4. Detection view and reach query, tested with SQL. Done 2026-11-06:
   - `roadsafety/hazards.sql` and `roadsafety/reach.sql` are templates with pyexasol placeholders (`{window_min!d}`, `{min_vehicles!d}`, `{min_providers!d}`, `{friction_max!f}`, `{blockage_after_s!d}`, `{reach_m!d}`, `{toward_deg!d}`). `simulate_road_safety.py views` renders them with the defaults into `V_HAZARDS_LIVE` and `V_HAZARD_REACH`; the dashboard runs the templates with its own values.
   - One generic rule table: per hazard type the evidence kinds, radius, window, minimum vehicles/providers/signals, whether a sensor can stand in for vehicles, a required kind (airbag for accidents) and the expiry. Hazards are grouped per ~450 m grid cell, the wrong-way driver per vehicle.
   - Detection never reads `SCENARIO_LOG`.
   - Background stream (10 min, 180,000 signals): 0 hazards, 0 wrong-way signals.
   - Hand-made test events (deleted afterwards): slippery road from 3 vehicles/2 providers, slippery road from 1 vehicle + bridge sensor, accident (airbag + hazard lights), wrong-way driver on the A9, fog sensor: all 5 found; a single ESC event not. With `min_providers = 3` the 2-provider hazard disappears, the sensor-confirmed one stays.
   - 0.12 s for detection and 0.19 s for reach on 10 minutes of data.
   - Not tested yet: the switch from accident area to unmanaged blockage after 10 minutes (needs the accident scenario).
5. Triggers and `SCENARIO_LOG`. Done 2026-11-06:
   - `simulate_road_safety.py scenarios | trigger <type> [--area] [--stop] | stop-all`; areas and effects in `scripts/road_scenarios.py`. A trigger writes one `SCENARIO_LOG` row and prints a SIMULATION banner; the running simulator picks it up within one tick. Default areas: 13 Isar bridges, Landshuter Allee (Mittlerer Ring), A9 at Freimann, the north (Freimann to the A99), a storm band west to east in 20 min, the A95 at Forstenrieder Park, the A96.
   - All 7 scenarios are detected. Changes to detection that the tests showed were needed: groups of the same type merge (`merge_m`), grid size per type (`cell_m`), fog and storm counted per cell instead of pairwise (`rule_method` = `cell`), camera alarms attach to the wrong-way vehicle, braking within 1 km of an airbag is not an obstacle, evidence thinned to one per vehicle, kind and segment per 30 s, window 15 min.
   - Hazards end when cleared: 5 vehicles that share the signal drove the hazard's segments after the last evidence without reporting it (`clear_vehicles`), or on expiry. The wrong-way driver only expires.
   - The background noise is only produced while moving (no ESC in a jam), hard braking 0.3 per vehicle and hour.
   - Black-ice run at x5: 0 hazards in 10 min of background; 8 bridges confirmed 50 s after the trigger, 9-11 while it runs (low-traffic bridges come and go); after the stop they clear one after another, the last after about 11 min of simulation time.
   - All 7 scenarios at once: 22 hazards, detection 1.8 s, reach 2.6 s. In the demo usually 1-2 scenarios run at a time.
   - The wrong-way driver drives to the end of the network (about 5 min) and stops; its hazard expires 5 min later.
6. Dashboard. Done 2026-11-06: `munich-road-conditions` on Dash Server, http://127.0.0.1:5100/apps/munich-road-conditions
   - Reads through the Dash Server profile `road-safety-demo` (user `dash_readonly`). Refresh every 5 s; one refresh (11 queries) takes about 2 s with two scenarios running.
   - Main query `queries/hazards_live.sql` = `roadsafety/reach.sql` with `roadsafety/hazards.sql` inlined, generated; thresholds (providers, vehicles) come from the page's sliders.
   - Simulation band, KPI row, map (vehicles, raw event signals, sensors, hazard segments red/amber, area hazards as circles, dashed injected areas labelled "simulated", wrong-way trail; switchable city roadworks, motorway events, accident hotspots), ticker, provider contribution, timeline trigger -> first signal -> confirmation, SQL foldout, caveat and attribution.
   - The app's source is `dashboard/` in this repo.
   - Not built yet: the replay slider and the Ask panel (optional second step).
7. Open: a dry run with a recording as the fallback.

## Platform

The demo runs on Exasol Personal, exapump and Dash Server:

- Database: the named Exasol Personal deployment `default` (`exasol status`, local preset, 127.0.0.1:8563, user `sys`). Start and stop with `exasol start|stop`.
- SQL and loading: exapump with the profile `road-safety-demo` (`exapump sql -p road-safety-demo "..."`, `exapump upload`).
- Python: the project `.venv` (`requirements.txt`: osmium, shapely, pyexasol).
- Dashboard: [Dash Server](https://github.com/exasol-labs/dash-server) (http://127.0.0.1:5100), with an Exasol profile `road-safety-demo`: user `dash_readonly`, `SELECT` on `ROAD_DATA` and `ROAD_SAFETY` only (writes are refused, tested). Password in `secrets/dash_readonly_password` and in Dash Server's secret store (see `secrets/README.md`).
- Agent SQL: the deployment's MCP server (`exasol mcp enable`, http://127.0.0.1:4896/mcp), registered in Claude Code as `exasol-personal`. It offers only read tools (no write query), but logs in as `sys`, the only user in the database.

## Decisions

- **Claude runs every trigger**, in the same Claude Code session as the rest of the demo. There is no second terminal. Before each trigger, Claude explains in plain words that this is a simulation, what it injects (which signals, from how many vehicles and sensors, in which area) and what it does *not* do (it writes no hazards: detection is left to the SQL). After the trigger, Claude shows the banner line from the command output.
- **The motorway ring is included in full** (A8, A9, A92, A94, A95, A96, A99), not clipped to the city boundary.
- **Weather day: 2026-10-27, a dry, mild weekday** (the default of `run --weather-day`), since the 2026-11-06 revision: the oil spill must not look like ice, and the city's real roadworks fit a recent day. The frost day below stays available with `--weather-day 2025-01-10`. Originally chosen: **2025-01-10**: a mild, wet day followed by a dry frost night, the clearest case of wet roads refreezing. Checked in the 10-minute values, see below. The other candidates stay listed as alternatives.

### Weather day candidates (DWD station 3379, München-Stadt)

| Date | What happened | Min air / ground (°C) | Why it fits |
|---|---|---|---|
| **2025-01-10** (chosen) | Mild, wet day (13.7 °C, 4.8 mm rain), then a dry, clear frost night | -2.4 / -4.7 | Classic refreeze of wet roads, no snow cover to confuse the picture |
| 2025-01-03 | 10.5 °C and 15.7 mm rain the day before, then frost | -3.3 / -5.8 | Same pattern, more water on the road, 2 cm of snow |
| 2024-01-19 | 11.3 °C and 3.6 mm rain, then a sharp drop; the day stays below zero | -5.4 / -6.9 | Flash freeze, ice lasts all day |
| 2024-01-17 | Deep frost overnight, then 9.7 mm of rain during the day | -5.2 / -8.8 | Rain on frozen ground: freezing rain, the most dramatic case |

**Check in the 10-minute values (station 3379, times in CET; DWD stores UTC):** on 9 Jan the afternoon is mild (12 °C). A cold front passes at about 19:00: the air drops to 5 °C within 30 minutes and 4.7 mm of rain falls between 19:00 and 20:10. Overnight the air settles just above 0 °C, but the temperature 5 cm above the ground goes below zero from about 00:30 and reaches -1.6 °C at 06:30-07:00, then rises above zero at about 09:00. So the black-ice window on 10 Jan is about 00:30-09:00, worst in the morning rush hour. With the air near 0 °C, bridges freeze before the other roads: that fits the Isar-bridge scenario. The simulation clock starts at **2025-01-10 06:30 CET**.

There is no fog on 2025-01-10 (visibility above 40 km all morning). For `fog`, the simulator takes visibility from a fog day: 2024-10-30 or 2022-11-12 at station 3379, or a day from station 1262 München-Flughafen. Details of all sources: [research/road-data-sources.md](../research/road-data-sources.md).
