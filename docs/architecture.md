# Architecture: a sovereign road safety service on free components

This repo is a **reference implementation** for partners of [Data for Road Safety (DFRS)](context.md): the service that receives safety events from several carmakers and the road operator, combines them into confirmed warnings and publishes those back. A partner can run it on its own machine or cloud account, with free components only. This page explains how it is built, why, and what we measured. The design history is in [plan.md](plan.md).

## The use case in one paragraph

In DFRS, carmakers detect hazards in their own backends (an ESC intervention on ice, an airbag, a car going the wrong way) and share **finished events** in the eight SRTI categories, never raw vehicle data. Road authorities share what their sensors see. One report alone isn't trustworthy: one carmaker's detection can be wrong, and its many cars are then all wrong the same way. A service provider therefore **combines** reports from independent sources and passes on only what is confirmed, to reach as many drivers as possible. That combining service is what this repo builds.

## The partner's node

```
   THE OTHER PARTNERS (simulated)          THE PARTNER'S OWN NODE (this reference implementation)
                                          ┌───────────────────────────────────────────────────────────────┐
   carmakers OEM A-F, navigation service   │  Apache Kafka (sidecar)           Exasol Personal             │
     SRTI events ─────────────────────────►│  srti.oem-a ... srti.nav ──┐                                   │
   road operator                           │  datex.road-operator ──────┤ Kafka connector (Java UDFs):     │
     sensor readings, alerts ─────────────►│                            └─► INBOUND_MESSAGES (raw JSON)    │
                                           │                                    │ parse.sql               │
                                           │                                    ▼                          │
                                           │                               SRTI_EVENTS, SENSOR_READINGS   │
                                           │                                    │ combine.sql (one query)  │
                                           │                                    ▼                          │
                                           │  srti.confirmed ◄──── publish ── INCIDENTS, INCIDENT_EVIDENCE  │
                                           │        │                         OUTBOUND_MESSAGES             │
                                           │        │            Dash Server: dashboard (read-only user)   │
                                           └────────┼──────────────────────────────────────────────────────┘
                                                    └──► subscribers (iteration 2: the partner's EDC connector → MDS)
```

Everything inside the box is what a partner runs; everything outside is what the data space delivers. In iteration 1 the outside is simulated (`scripts/simulate_road_safety.py`); in iteration 2 the topics are fed by a real connector.

| Part | What it does | Where |
|---|---|---|
| **Exasol Personal** | The database: system of record, all logic in SQL | Local deployment `road-safety-demo` (a VM on macOS), or the partner's cloud account |
| **Apache Kafka 4.4** | Transport between the partners and the service. KRaft mode, one node | Sidecar of the deployment; `127.0.0.1:19092` from the laptop, `kafka:9092` from the database |
| **Kafbat UI** | Shows the topics and messages ("proof that it's Kafka") | Sidecar, <http://127.0.0.1:18080> |
| **Exasol Kafka connector extension** | Java UDFs: the database pulls the messages from Kafka itself | Schema `KAFKA_EXTENSION`, JAR in BucketFS |
| **The road safety service** | Every 3 s: load, parse, combine, keep incidents, publish | `scripts/road_safety_service.py` |
| **Dash Server** | Hosts the dashboard | <http://127.0.0.1:5100/apps/munich-road-conditions> |
| **The simulator** | Plays the other partners: events, sensor readings, false alarms; triggers on request | `scripts/simulate_road_safety.py` (outside the box on purpose) |

## Free components, and what "sovereign" means here

| Component | License | Sends data elsewhere? |
|---|---|---|
| Exasol Personal | Free (proprietary database, free edition; the launcher is MIT) | Optional version checks by the launcher, which can be switched off at install (`--no-launcher-version-check`) |
| Apache Kafka | Apache 2.0, Apache Software Foundation | No |
| Kafbat UI | Apache 2.0 | No |
| Exasol Kafka connector extension | MIT | No |
| Dash Server | MIT | No |
| Python, pyexasol, kafka-python | PSF, MIT, Apache 2.0 | No |
| Open data (OSM, DWD, BASt, City of Munich, Autobahn GmbH, Unfallatlas) | ODbL, CC BY 4.0, dl-de/by-2.0 | Downloaded once |

**Sovereign, concretely:**
- **It runs where the partner decides:** on a laptop, or in the partner's own cloud account. Exasol Personal also deploys to AWS, Azure, Exoscale and STACKIT, so a European provider is possible. There is no central platform and no third-party service in between.
- **Data rests in one place the partner controls:** Kafka keeps nothing (topics are empty after every start); Exasol keeps everything: what came in (`INBOUND_MESSAGES`), what it was turned into (`INCIDENTS`, `INCIDENT_EVIDENCE`) and what was passed on, to whom and when (`OUTBOUND_MESSAGES`). That is the audit trail a usage policy may require, queryable with SQL.
- **Open interfaces, replaceable parts:** the transport is the Kafka protocol (any Kafka, managed or not, works), the events follow DATEX II SRTI, the logic is plain SQL a partner can read and change.
- **No lock-in through the broker:** Apache Kafka was chosen over Redpanda on purpose: vendor-neutral, Apache 2.0, and no usage reporting (Redpanda's default configuration reports cluster metadata to its vendor).

**What a partner should still do:** mirror the container images (Kafka, Kafbat UI, the database) into a registry of their own instead of pulling from Docker Hub; keep Kafka unexposed (no TLS or authentication: it listens on loopback and the deployment network only). Exchange with other partners goes through the EDC connector (iteration 2), never through a published Kafka port.

## Data flow, step by step

**1. The partners send events.** One Kafka topic per provider (`srti.oem-a` … `srti.oem-f`, `srti.nav`, `datex.road-operator`), JSON as defined in `scripts/srti_events.py`. Field names follow the DATEX II SRTI profile as real feeds use it (Finland's Digitraffic, DATEX II 3.7): the SRTI category with its DATEX II record type (`GeneralObstruction`/`unprotectedAccidentArea`, `PoorEnvironmentConditions`/`fog`, ...), position, heading, road, detection time, validity, `probabilityOfOccurrence`, the provider's confidence, how many of its vehicles saw it, and a cause where known (ice, fluid). An event has a stable id; `update` replaces the previous version, `cancel` withdraws it. Not every provider can report everything: the navigation service has no airbag or ESC data, two carmakers have no environment sensors.

**2. The database loads them itself.** The Exasol Kafka connector (Java UDFs) imports every topic with new messages, up to 4 in parallel, into a staging table per topic, from where they are copied into `INBOUND_MESSAGES`. Measured: 2 to 4 s from send to row.

**3. Parse.** `roadsafety/parse.sql` turns new messages into `SRTI_EVENTS` (every version of every event) and `SENSOR_READINGS`. A watermark per topic and partition (`PARSE_STATE`) means nothing is parsed twice or skipped.

**4. Combine.** `roadsafety/combine.sql`, one SQL statement with parameters, about 0.2 s:
- every report valid now: the current version of each event (not cancelled, not expired), plus every road operator sensor whose latest reading alerts (frozen surface with low friction, poor visibility, wrong-way vehicle, stationary vehicle, warning trailer);
- reports belong together when they have the same category, are within the category's radius (300 m for a slippery road, 150 m for an obstacle, 250 m for an accident, 400 m for road works, 2.5 km for a moving wrong-way driver, 5 km for fog, 6 km for a storm) and, for directional categories, head the same way;
- **confirmed** with 2 different providers, or 1 provider plus a road operator sensor, or the road operator alone where it can know (its warning trailer for road works, its wrong-way cameras). Several reports or vehicles of one provider count once;
- the cause of a slippery road (ice or fluid) from the providers, a road weather station or the air temperature; confidence from all reports; an accident without a road operator response becomes an **unmanaged blockage** after 10 minutes;
- the **reach** from the traffic volume on the open data: for a spot, the vehicles that pass it in the next 10 minutes in its direction; for fog and storms, the vehicles in the area.

**5. Keep incidents.** The service matches each confirmed group to an open incident (shared reports, else distance) and keeps `INCIDENTS` with a stable id (`INC-<run>-<n>`), a lifecycle (active, cleared) and a version, plus `INCIDENT_EVIDENCE`. An incident clears when no valid report supports it any more.

**6. Publish.** Every new, changed or cleared incident goes to the topic `srti.confirmed` as a warning (`srti-incident/1`: category, cause, position, validity, confidence, the rule that confirmed it, the reach, and an attribution of the sources as the usage policies require), logged in `OUTBOUND_MESSAGES` with its Kafka offset.

**7. Show.** The dashboard reads with a read-only user: events as dots in the provider's colour (grey while unconfirmed), sensors, incidents with evidence and reach, the outbound ticker, data offers and contracts, and an event stream. Its sliders run the same `combine.sql` with another rule as a what-if; the service keeps publishing with the default rule.

## Restarts: Kafka keeps nothing, Exasol keeps everything

Sidecars have no volumes, so after `exasol stop`/`start` the topics are empty and get new topic ids. The connector resumes from the highest offset in its target table, so each topic has its own staging table, emptied when the topic id changes. Tested with a producer running through a restart: every Kafka position loaded exactly once, the loader followed Kafka to its new IP. After a restart the service publishes the still active incidents again. Not covered: messages Kafka acknowledged in the last seconds before the stop and nobody loaded yet (4 of 69 in one test); stop the producers first for a clean stop.

## Measured on a laptop (macOS, Apple silicon, Exasol Personal VM with 24 GB and 2 CPUs)

| | |
|---|---|
| Send to row in Exasol | 2 to 4 s |
| Parse and combine | about 0.2 s per round |
| Confirmation | one round after the second provider's first report (seconds); with the default delays 30 to 90 simulated seconds after a trigger |
| Kafka / Kafbat UI memory | about 370 MB / 310 MB |
| `exasol start` with the sidecars | 18 s |
| Load test, 10x speed, 8 scenarios at once | 3,362 messages sent, 3,362 loaded, each Kafka position once |

## Known limits and open points

- **Exasol Personal on a laptop has no DNS in the UDF sandbox**, so the Java UDFs read the name `kafka` from a hosts file in BucketFS that the loader keeps current. Reported as [exasol/exasol-personal#464](https://github.com/exasol/exasol-personal/issues/464); once fixed, the workaround goes.
- **No connector, no enforced usage policy yet** (iteration 2): `DATA_OFFERS` and `CONTRACTS` are metadata shown on the dashboard.
- **The event format is provisional:** modelled on DATEX II SRTI until a real DFRS sample is available.
- **Clearing by absence** (providers passing the spot without reporting it) isn't built: an incident clears when its reports are withdrawn or expire.
- **The service runs as a Python process**, not yet as a sidecar of the deployment; making it one would put the whole node under `exasol start`.
- **The rules are ours**, not the official DFRS logic, and the partners, their events and the sensor readings are synthetic.

## Iterations

| | Iteration 1 (built) | Iteration 2 | Production at a partner |
|---|---|---|---|
| Runs on | A laptop, Exasol Personal | The partner's own cloud account, Exasol Personal | The partner's infrastructure, sized for real volumes |
| Inbound | Simulated carmakers and road operator → Kafka | An EDC connector with the Kafka extension, MDS test environment, real DFRS sample data | The partner's DFRS contracts |
| Outbound | Topic `srti.confirmed` | Published as a data offer through the connector | As iteration 2 |
| Cost | Free | Free software, the partner's cloud costs | To be discussed with Exasol |
