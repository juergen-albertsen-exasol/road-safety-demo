#!/usr/bin/env python3
"""Simulate the partners of the road safety demo: carmakers, a navigation service, the road operator.

Everything this script sends is SYNTHETIC. The road network, weather and traffic volumes it builds on
are real open data in ROAD_DATA (scripts/prepare_road_data.py). Design: docs/plan.md, revision
2026-11-09.

The partners don't send raw vehicle data, as in Data for Road Safety: each carmaker detects hazards
in its own backend and shares finished SRTI events (scripts/srti_events.py), one Kafka topic per
provider (srti.oem-a ... srti.oem-f, srti.nav). The road operator publishes its sensor readings, and
alerts from its sensors, to datex.road-operator. scripts/kafka_loader.py loads all of it into
ROAD_SAFETY.INBOUND_MESSAGES; combining the events into confirmed warnings is up to the SQL.

    .venv/bin/python scripts/simulate_road_safety.py setup            # schema, providers, sensors
    .venv/bin/python scripts/simulate_road_safety.py run              # the partners, Ctrl-C to stop
    .venv/bin/python scripts/simulate_road_safety.py status
    .venv/bin/python scripts/simulate_road_safety.py reset --yes      # empty the scenario and state tables

`run` options: --speed (simulation seconds per wall second, default 1), --seed, --weather-day
(default 2026-10-27), --start (local time, default 06:00), --tick (wall seconds per batch, default 1),
--false-alarms (single unconfirmed reports per simulation hour, default 20), --transport kafka|direct
(direct writes the same messages into INBOUND_MESSAGES, without Kafka and the loader). --log prints
every message sent: red = caused by a triggered scenario, yellow = a single false alarm that should
never be confirmed, grey = routine sensor readings (--log events leaves those out).

Background false alarms: now and then one provider reports a hazard where nothing happened (one car's
ESC on a sharp bend). Nobody else reports it, so it must never become an incident.

Scenarios are injected on purpose and visibly, while `run` is running:

    .venv/bin/python scripts/simulate_road_safety.py scenarios                       # what can be triggered
    .venv/bin/python scripts/simulate_road_safety.py trigger oil_spill --road "Fürstenrieder Straße" --near "Laimer Platz"
    .venv/bin/python scripts/simulate_road_safety.py trigger wrong_way_driver --road A9 --direction south --near Garching-Süd
    .venv/bin/python scripts/simulate_road_safety.py trigger fog --near Aubing
    .venv/bin/python scripts/simulate_road_safety.py trigger slippery_road --stop
    .venv/bin/python scripts/simulate_road_safety.py stop-all

Steering who reports, to show the combination rule at work (with a place: for the new scenario;
without one: for the running scenario of that type):

    trigger oil_spill --road A9 --near Freimann --providers OEM_A      # one carmaker only: not confirmed
    trigger oil_spill --providers OEM_A,OEM_D                          # a second carmaker reports it too
    trigger oil_spill --sensor                                         # the road operator's sensor sees it
    trigger fog --near Aubing --delay OEM_A=10,OEM_B=60 --confidence 0.6

A trigger writes one row into SCENARIO_LOG and prints a SIMULATION banner. The running simulator
picks it up within one tick. It never writes a hazard or an incident.

Connects as `sys` to the Exasol Personal deployment (--deployment, default road-safety-demo); the
password is read from the deployment's secrets.json and never printed. Writes to ROAD_SAFETY, and
with --transport kafka to the Kafka sidecar (127.0.0.1:19092).
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import random
import signal
import ssl
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyexasol

import db
import road_scenarios as rs
import road_traffic as rt
import srti_events as ev

ROOT = Path(__file__).resolve().parent.parent
BERLIN = ZoneInfo("Europe/Berlin")
STATION = 3379
KAFKA = "127.0.0.1:19092"

PROVIDERS = [  # id, name, kind, dynamics, environment, fleet share %, topic
    ("OEM_A", "OEM A", "oem", True, True, 24.0),
    ("OEM_B", "OEM B", "oem", True, True, 19.0),
    ("OEM_C", "OEM C", "oem", True, True, 14.0),
    ("OEM_D", "OEM D", "oem", True, True, 11.0),
    ("OEM_E", "OEM E", "oem", True, False, 9.0),
    ("OEM_F", "OEM F", "oem", True, False, 6.0),
    ("NAV", "Navigation service", "navigation", False, False, 17.0),
    ("ROAD_OP", "Road operator", "road_operator", None, None, None),
]
FLEET = {p[0]: p[5] for p in PROVIDERS if p[5]}

CLASS_WEIGHT = {  # how much traffic a road class carries: where background false alarms happen
    "motorway": 8, "motorway_link": 2, "trunk": 6, "trunk_link": 2, "primary": 5, "primary_link": 1.5,
    "secondary": 3.5, "secondary_link": 1, "tertiary": 2, "tertiary_link": 1, "unclassified": 0.3,
    "residential": 0.1, "living_street": 0.02,
}
DEFAULT_KMH = {"motorway": 130, "motorway_link": 80, "trunk": 80, "trunk_link": 60, "living_street": 7,
               "residential": 30}
TRAFFIC_EVERY_S = 10
SENSOR_EVERY_S = 60
FALSE_ALARM_CATEGORIES = {"temporary_slippery_road": 0.4, "obstacle_on_road": 0.35, "wrong_way_driver": 0.1,
                          "reduced_visibility": 0.1, "unprotected_accident_area": 0.05}
FALSE_ALARM_VALID_MIN = 10


# --------------------------------------------------------------------------- database
def connect(deployment: str) -> pyexasol.ExaConnection:
    return db.connect(deployment, dict_rows=True, compression=True)


lit, insert, wall_now = db.lit, db.insert, db.wall_now


# --------------------------------------------------------------------------- road graph
@dataclass
class Segment:
    sid: int
    from_node: int
    to_node: int
    oneway: int
    road_class: str
    name: str
    ref: str
    kmh: float
    is_tunnel: bool
    coords: list            # [(lon, lat), ...] from -> to
    cum: list               # cumulative metres along coords
    length: float
    crosses_isar: bool = False
    bridge_name: str = ""
    mid: tuple = (0.0, 0.0)  # lon, lat
    heading: float = 0.0     # degrees, from -> to


class Graph(dict):
    """segment id -> Segment, plus node indexes: exits[node] = [(sid, forward)], into[node] = [sid]."""
    exits: dict
    into: dict


def parse_linestring(wkt: str) -> list[tuple[float, float]]:
    body = wkt[wkt.index("(") + 1: wkt.rindex(")")]
    return [tuple(map(float, p.split())) for p in body.split(",")]


KX = 111320.0 * math.cos(math.radians(48.137))
KY = 110540.0


def load_graph(c):
    segs = Graph()
    for r in c.execute('SELECT "segment_id", "from_node", "to_node", "oneway", "road_class", "name", "ref", '
                       '"maxspeed_kmh", "is_tunnel", "geometry_wkt", "crosses_isar", "bridge_name", "mid_lon", "mid_lat", '
                       '"heading_deg" FROM ROAD_DATA.ROAD_SEGMENTS'):
        coords = parse_linestring(r["geometry_wkt"])
        cum = [0.0]
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            cum.append(cum[-1] + math.hypot((x2 - x1) * KX, (y2 - y1) * KY))
        rc = r["road_class"]
        kmh = float(r["maxspeed_kmh"]) if r["maxspeed_kmh"] is not None else DEFAULT_KMH.get(rc, 50)
        sid = int(r["segment_id"])
        segs[sid] = Segment(sid, int(r["from_node"]), int(r["to_node"]), int(r["oneway"]), rc,
                            r["name"] or "", r["ref"] or "", kmh, bool(r["is_tunnel"]), coords, cum, max(cum[-1], 1.0),
                            bool(r["crosses_isar"]), r["bridge_name"] or "", (r["mid_lon"], r["mid_lat"]),
                            float(r["heading_deg"] or 0))
    # node -> segments you can enter from it, with direction (True = from -> to)
    exits: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    for s in segs.values():
        if s.oneway in (0, 1):
            exits[s.from_node].append((s.sid, True))
        if s.oneway in (0, -1):
            exits[s.to_node].append((s.sid, False))
    into: dict[int, list[int]] = defaultdict(list)
    for s in segs.values():
        into[s.to_node].append(s.sid)
    segs.exits, segs.into = exits, into
    return segs, exits


def point_at(seg: Segment, fwd: bool, pos: float) -> tuple[float, float, float]:
    """lon, lat, heading at `pos` metres from the start of the segment in driving direction."""
    d = pos if fwd else seg.length - pos
    d = min(max(d, 0.0), seg.cum[-1])
    i = min(max(bisect.bisect_right(seg.cum, d) - 1, 0), len(seg.coords) - 2)
    (x1, y1), (x2, y2) = seg.coords[i], seg.coords[i + 1]
    span = seg.cum[i + 1] - seg.cum[i]
    t = (d - seg.cum[i]) / span if span > 0 else 0.0
    lon, lat = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
    hdg = math.degrees(math.atan2((x2 - x1) * KX, (y2 - y1) * KY)) % 360
    return lon, lat, (hdg if fwd else (hdg + 180) % 360)


def road_label(seg: Segment) -> str | None:
    """The road as a carmaker's map matching would name it: the motorway number, else the street name."""
    if seg.ref and seg.road_class.startswith(("motorway", "trunk")):
        return seg.ref.split(";")[0]
    return seg.name or seg.ref.split(";")[0] or None


# --------------------------------------------------------------------------- weather
class Weather:
    """DWD 10-minute values of the weather day (station 3379), looked up by simulation time."""

    def __init__(self, c, day: datetime):
        lo, hi = day - timedelta(days=1), day + timedelta(days=2)
        self.ten = {}
        for r in c.execute('SELECT "ts_local", "air_temp_c", "temp_5cm_c", "precip_mm", "wind_ms", "gust_ms" '
                           'FROM ROAD_DATA.WEATHER_10MIN WHERE "station_id" = {st!d} AND "ts_local" BETWEEN {lo} AND {hi}',
                           {"st": STATION, "lo": lo.strftime("%Y-%m-%d %H:%M:%S"), "hi": hi.strftime("%Y-%m-%d %H:%M:%S")}):
            self.ten[_ts(r["ts_local"])] = r
        self.vis = {}
        for r in c.execute('SELECT "ts_local", "visibility_m" FROM ROAD_DATA.WEATHER_VISIBILITY '
                           'WHERE "station_id" = {st!d} AND "ts_local" BETWEEN {lo} AND {hi}',
                           {"st": STATION, "lo": lo.strftime("%Y-%m-%d %H:%M:%S"), "hi": hi.strftime("%Y-%m-%d %H:%M:%S")}):
            self.vis[_ts(r["ts_local"])] = r["visibility_m"]
        if not self.ten:
            sys.exit(f"error: no weather values for {day:%Y-%m-%d} in ROAD_DATA.WEATHER_10MIN")
        self.keys = sorted(self.ten)

    def at(self, t: datetime) -> dict:
        i = max(bisect.bisect_right(self.keys, t) - 1, 0)
        r = dict(self.ten[self.keys[i]])
        for k in ("air_temp_c", "temp_5cm_c", "precip_mm", "wind_ms", "gust_ms"):
            r[k] = float(r[k]) if r[k] is not None else None
        hour = t.replace(minute=0, second=0, microsecond=0)
        v = self.vis.get(hour)
        r["visibility_m"] = float(v) if v is not None else 20000.0
        lo = t - timedelta(minutes=60)
        r["precip_last_hour_mm"] = sum(float(self.ten[k]["precip_mm"] or 0) for k in self.keys if lo < k <= t)
        return r


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("T", " ")[:19])


# --------------------------------------------------------------------------- setup
def pick_spread(cands, min_dist_m, limit):
    """Greedy: closest to the centre first, at least min_dist_m apart."""
    cx, cy = 11.575, 48.137
    cands = sorted(cands, key=lambda s: math.hypot((s[1] - cx) * KX, (s[2] - cy) * KY))
    out = []
    for c in cands:
        if all(math.hypot((c[1] - o[1]) * KX, (c[2] - o[2]) * KY) >= min_dist_m for o in out):
            out.append(c)
        if len(out) >= limit:
            break
    return out


def setup(args) -> None:
    c = connect(args.deployment)
    sql = (ROOT / "roadsafety" / "schema.sql").read_text()
    stmts = [s.strip() for s in "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).split(";")]
    for s in stmts:
        if s:
            c.execute(s)
    print("Schema ROAD_SAFETY created")
    insert(c, "DATA_PROVIDERS", [(*p, ev.TOPICS[p[0]]) for p in PROVIDERS])
    print(f"  {len(PROVIDERS)} providers, one topic each")

    segs, _ = load_graph(c)
    rows = list(c.execute('SELECT "segment_id", "mid_lon", "mid_lat", "road_class", "name", "ref", "bridge_name", '
                          '"crosses_isar", "is_tunnel", "is_bridge", "length_m", "to_node", "oneway" FROM ROAD_DATA.ROAD_SEGMENTS'))
    sensors = []
    # road weather stations: one per Isar bridge
    bridges = {}
    for r in rows:
        if r["crosses_isar"]:
            key = r["bridge_name"] or r["name"] or (f"{r['ref']} Isar bridge" if r["ref"] else f"Isar bridge {r['segment_id']}")
            if key not in bridges or r["length_m"] > bridges[key]["length_m"]:
                bridges[key] = r
    for k, (name, r) in enumerate(sorted(bridges.items()), 1):
        sensors.append((f"RW-{k:02d}", "road_weather", "isar_bridge", f"Road weather station {name}",
                        r["segment_id"], "ROAD_OP", r["mid_lat"], r["mid_lon"]))
    # more road weather stations on motorway and main road bridges (bridge decks freeze first)
    taken = [(r["segment_id"], r["mid_lon"], r["mid_lat"]) for r in bridges.values()]
    other = [(r["segment_id"], r["mid_lon"], r["mid_lat"], r["ref"] or r["name"]) for r in rows
             if r["is_bridge"] and not r["crosses_isar"] and r["length_m"] >= 60
             and r["road_class"] in ("motorway", "trunk", "primary")]
    other = [o for o in other if all(math.hypot((o[1] - t[1]) * KX, (o[2] - t[2]) * KY) >= 2500 for t in taken)]
    for k, (sid, lon, lat, label) in enumerate(pick_spread(other, 3500, 24), len(bridges) + 1):
        sensors.append((f"RW-{k:02d}", "road_weather", "bridge", f"Road weather station, bridge {label or sid}".strip(),
                        sid, "ROAD_OP", lat, lon))
    # tunnel cameras: one per named tunnel road of the main network
    tunnels = {}
    for r in rows:
        if r["is_tunnel"] and r["road_class"] in ("motorway", "trunk", "primary", "secondary") and r["length_m"] >= 150:
            key = r["name"] or r["ref"]
            if key and (key not in tunnels or r["length_m"] > tunnels[key]["length_m"]):
                tunnels[key] = r
    for k, (name, r) in enumerate(sorted(tunnels.items(), key=lambda kv: -kv[1]["length_m"])[:12], 1):
        sensors.append((f"TC-{k:02d}", "camera", "tunnel", f"Tunnel camera {name}",
                        r["segment_id"], "ROAD_OP", r["mid_lat"], r["mid_lon"]))
    # wrong-way cameras at motorway entries: links whose end joins a motorway, named after the nearest junction
    motorway_at = {int(s.from_node): s.ref.split(";")[0] for s in segs.values() if s.road_class == "motorway"}
    junctions = [(p["name"], float(p["lon"]), float(p["lat"])) for p in
                 c.execute("SELECT \"name\", \"lon\", \"lat\" FROM ROAD_DATA.PLACES WHERE \"kind\" = 'motorway_junction'")]

    def junction(lon, lat):
        best = min(junctions, key=lambda j: math.hypot((j[1] - lon) * KX, (j[2] - lat) * KY), default=None)
        return best[0] if best and math.hypot((best[1] - lon) * KX, (best[2] - lat) * KY) < 2500 else None
    entries = [(r["segment_id"], r["mid_lon"], r["mid_lat"],
                " ".join(x for x in (motorway_at[int(r["to_node"])], junction(r["mid_lon"], r["mid_lat"])) if x))
               for r in rows if r["road_class"] == "motorway_link" and r["oneway"] == 1 and int(r["to_node"]) in motorway_at]
    for k, (sid, lon, lat, label) in enumerate(pick_spread(entries, 700, 90), 1):
        sensors.append((f"WW-{k:02d}", "camera", "motorway_entry", f"Wrong-way camera, entry {label or sid}".strip()[:200],
                        sid, "ROAD_OP", lat, lon))
    # loop detectors on the motorway main carriageways
    main = [(r["segment_id"], r["mid_lon"], r["mid_lat"], r["ref"]) for r in rows if r["road_class"] == "motorway" and r["ref"]]
    ring = [(r["segment_id"], r["mid_lon"], r["mid_lat"], r["name"] or r["ref"]) for r in rows
            if r["road_class"] == "trunk" and "2R" in (r["ref"] or "")]
    loops = [(x, "motorway") for x in pick_spread(main, 2500, 30)] + [(x, "ring") for x in pick_spread(ring, 1500, 15)]
    for k, ((sid, lon, lat, ref), kind) in enumerate(loops, 1):
        sensors.append((f"LD-{k:02d}", "loop_detector", kind, f"Loop detector {ref}", sid, "ROAD_OP", lat, lon))
    insert(c, "INFRA_SENSORS", sensors)
    kinds = defaultdict(int)
    for s in sensors:
        kinds[s[2]] += 1
    print(f"  {len(sensors)} sensors: " + ", ".join(f"{v} {k}" for k, v in kinds.items()))


# --------------------------------------------------------------------------- sending
class Publisher:
    """Sends messages to Kafka (one topic per provider), or with --transport direct writes the same
    messages straight into ROAD_SAFETY.INBOUND_MESSAGES, as if the loader had loaded them."""

    def __init__(self, transport: str, c, run_id: int):
        self.transport, self.c, self.run_id = transport, c, run_id
        self.sent = 0
        if transport == "kafka":
            from kafka import KafkaProducer
            try:
                self.producer = KafkaProducer(bootstrap_servers=KAFKA, linger_ms=20, acks=1)
            except Exception as e:  # noqa: BLE001
                sys.exit(f"error: Kafka not reachable at {KAFKA} ({type(e).__name__}). Is the kafka sidecar running? "
                         "(exasol sidecar status kafka -d road-safety-demo), or use --transport direct")
        else:
            sys.path.insert(0, str(ROOT / "scripts"))
            from kafka_loader import SETUP_SQL
            for s in SETUP_SQL:
                c.execute(s)
            self.offsets: dict[str, int] = defaultdict(int)
            self.rows: list[tuple] = []

    def send(self, topic: str, key: str, msg: dict) -> None:
        self.sent += 1
        if self.transport == "kafka":
            self.producer.send(topic, key=key.encode(), value=json.dumps(msg, ensure_ascii=False).encode())
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            self.rows.append((topic, f"direct-{self.run_id}-{topic}", 0, self.offsets[topic], now, now,
                              json.dumps(msg, ensure_ascii=False)))
            self.offsets[topic] += 1

    def flush(self) -> None:
        if self.transport == "kafka":
            try:
                self.producer.flush(timeout=10)
            except Exception as e:  # noqa: BLE001 - keep simulating; the loader shows what arrived
                print(f"  !! Kafka: {type(e).__name__}: {e}", flush=True)
        elif self.rows:
            insert(self.c, "INBOUND_MESSAGES", self.rows)
            self.rows = []


# --------------------------------------------------------------------------- run
@dataclass
class Report:
    """One provider's event about one scenario: the provider detected it and keeps it up to date."""
    provider: str
    event_id: str
    due: float                  # simulation second of the next message
    confidence: float
    sensor_id: str | None = None
    version: int = 0
    observations: int = 0
    cancel_at: float | None = None
    done: bool = False
    category: str = ""          # as last sent, for the cancel message
    lon: float = 0.0
    lat: float = 0.0


class Sim:
    def __init__(self, c, args):
        self.c, self.args = c, args
        self.rng = random.Random(args.seed)
        self.graph, self.exits = load_graph(c)
        self.segs = self.graph
        capw = {int(r["segment_id"]): float(r["capacity_vph"]) / 1000
                for r in c.execute('SELECT "segment_id", "capacity_vph" FROM ROAD_DATA.SEGMENT_TRAFFIC')}
        main = [s for s in self.segs.values() if s.road_class in rs.MAIN and not s.is_tunnel]
        self.bg_segs = [s.sid for s in main]
        self.bg_weights = [s.length * CLASS_WEIGHT.get(s.road_class, 0.5) * capw.get(s.sid, 0.5) for s in main]
        self.main_segs = main
        # fixed sensors only: a warning trailer (location_kind mobile) reports only while its road works run
        self.sensors = list(c.execute("SELECT * FROM ROAD_SAFETY.INFRA_SENSORS WHERE \"location_kind\" <> 'mobile'"))
        if not self.sensors:
            sys.exit("error: no sensors; run `setup` first")
        day = datetime.strptime(args.weather_day, "%Y-%m-%d")
        self.weather = Weather(c, day)
        h, m = map(int, args.start.split(":"))
        self.t0 = day.replace(hour=h, minute=m)
        self.t = 0.0                    # simulation seconds since t0
        self.next_sensor = 0.0
        self.run_id = int(c.execute('SELECT COALESCE(MAX("run_id"), 0) + 1 FROM ROAD_SAFETY.SIM_STATE').fetchval())
        self.running: dict[int, rs.Running] = {}
        self.steering: dict[int, dict] = {}
        self.reports: dict[int, dict[str, Report]] = {}
        self.seg_fx: dict[int, dict] = {}
        self.speed = float(args.speed)
        self.traffic = rt.TrafficModel(c, self.graph)
        self.traffic.update(self.t0, 0.0, {})
        self.traffic_t = 0.0
        self.pub = Publisher(args.transport, c, self.run_id)

    @property
    def now(self) -> datetime:
        return self.t0 + timedelta(seconds=self.t)

    def new_id(self, provider: str) -> str:
        return f"{provider}-{self.rng.getrandbits(40):010x}"

    # ---- scenarios
    def refresh_scenarios(self) -> list[str]:
        """Pick up new, changed and ended scenarios from SCENARIO_LOG. Returns banner lines for the log."""
        notes = []
        rows = list(self.c.execute('SELECT "scenario_id", "scenario_type", "area", "started_at", "ended_at", "steering" '
                                   'FROM ROAD_SAFETY.SCENARIO_LOG WHERE "run_id" = {run!d}', {"run": self.run_id}))
        for r in rows:
            sid = int(r["scenario_id"])
            steering = json.loads(r["steering"]) if r["steering"] else {}
            if r["ended_at"] is None and sid not in self.running:
                try:
                    self.running[sid] = rs.Running(r, self)
                except ValueError as e:
                    notes.append(f"!! scenario {sid} ignored: {e}")
                    continue
                self.steering[sid] = steering
                self.reports[sid] = {}
                self.plan_reports(sid)
                notes.append(f">> SIMULATION scenario {sid} active: {r['scenario_type']} on "
                             f"{self.running[sid].area.description}")
            elif r["ended_at"] is None and steering != self.steering.get(sid):
                self.steering[sid] = steering
                self.plan_reports(sid)
                notes.append(f">> SIMULATION scenario {sid}: now reported by "
                             f"{', '.join(self.reporting(sid)) or 'nobody'}")
            elif r["ended_at"] is not None and sid in self.running:
                for rep in self.reports[sid].values():        # each provider withdraws its event
                    if rep.cancel_at is None:
                        rep.cancel_at = self.t + self.rng.uniform(5, 30)
                self.running.pop(sid).stop(self)
                notes.append(f">> SIMULATION scenario {sid} ended: {r['scenario_type']}")
        per_seg: dict[int, list] = defaultdict(list)
        for run_ in self.running.values():
            run_.move(self, point_at)
            for seg, fx in run_.effects(self).items():
                per_seg[seg].append(fx)
            if run_.kind == "storm":
                self.c.execute('UPDATE ROAD_SAFETY.SCENARIO_LOG SET "area_wkt" = {w} WHERE "scenario_id" = {id!d}',
                               {"w": run_.area.wkt, "id": run_.id})
        self.seg_fx = {seg: rs.merge(fxs) for seg, fxs in per_seg.items()}
        return notes

    def wanted(self, sid: int) -> tuple[dict, dict, bool]:
        """Providers with their delay (s), confidence overrides, and whether the road operator's sensor reports."""
        st = self.steering.get(sid, {})
        providers, sensor, self._sensor_delay = ev.plan_delays(self.running[sid].kind, st)
        return providers, st.get("confidence") or {}, sensor

    def reporting(self, sid: int) -> list[str]:
        providers, _, sensor = self.wanted(sid)
        return list(providers) + (["ROAD_OP sensor"] if sensor else [])

    def plan_reports(self, sid: int) -> None:
        """Start reports for providers that should report, withdraw those that no longer should."""
        run_ = self.running[sid]
        providers, conf, sensor = self.wanted(sid)
        since = max(run_.started_t, float(self.steering.get(sid, {}).get("since_t", 0.0)))
        reports = self.reports[sid]
        want = dict(providers)
        if sensor:
            want["ROAD_OP"] = self._sensor_delay
        for p, delay in want.items():
            if p in reports and reports[p].cancel_at is None and not reports[p].done:
                continue
            c = conf.get(p, conf.get("*"))
            reports[p] = Report(p, self.new_id(p), max(self.t, since + float(delay)),
                                float(c) if c is not None else self.rng.uniform(*ev.DEFAULT_CONFIDENCE),
                                sensor_id=self.sensor_for(run_) if p == "ROAD_OP" else None)
        for p, rep in reports.items():
            if p not in want and rep.cancel_at is None:
                rep.cancel_at = self.t + self.rng.uniform(2, 10)

    SENSOR_KINDS = {  # which of the road operator's sensors can see it
        "temporary_slippery_road": ("road_weather",), "reduced_visibility": ("road_weather",),
        "exceptional_weather": ("road_weather",), "wrong_way_driver": ("camera",),
        "unprotected_accident_area": ("camera",), "obstacle_on_road": ("camera",),
        "short_term_road_works": ("warning_trailer",)}

    def sensor_for(self, run_) -> str:
        """The road operator's sensor that sees it: the warning trailer, a sensor of the right kind within 1 km,
        or a mobile one (a patrol, an emergency call) when there is none."""
        if run_.trailer:
            return run_.trailer["sensor_id"]
        kinds = self.SENSOR_KINDS[ev.SCENARIO_EVENTS[run_.kind]["category"]]
        spot = run_.area.point or (self.graph[run_.area.start_seg].mid if run_.area.start_seg else None)
        near = [(rs.dist_m((float(s["lon"]), float(s["lat"])), spot), s["sensor_id"]) for s in self.sensors
                if s["sensor_type"] in kinds] if spot else []
        if near and min(near)[0] <= 1000:
            return min(near)[1]
        return f"MS-{run_.id}"

    def spot(self, run_) -> tuple[float, float, float | None, str | None] | None:
        """Where a provider's vehicle saw it this time: lon, lat, heading, road. None: nothing to see any more."""
        k = run_.kind
        if k == "wrong_way_driver":
            if run_.ww_now is None:
                return None
            lon, lat, hdg = run_.ww_now
            seg = min(((rs.dist_m(self.graph[s].mid, (lon, lat)), s) for s, _ in run_.ww_path))[1]
            return lon, lat, hdg, road_label(self.graph[seg])
        if k == "storm":                                 # within 3 km of the storm's core, in the moving band
            x0, y0, x1, y1 = run_.area.polygon.bounds
            core = (y0 + y1) / 2
            cands = [s for s in self.main_segs if x0 <= s.mid[0] <= x1 and y0 <= s.mid[1] <= y1
                     and abs(s.mid[1] - core) * KY <= 3000]
            if not cands:
                return None
            seg = self.rng.choice(cands)
            fwd = seg.oneway != -1
        elif k in ("accident", "animal", "debris"):
            seg = self.graph[run_.area.start_seg]
            fwd = run_.area.extra.get("fwd", seg.oneway != -1)
            lon, lat, hdg = point_at(seg, fwd, seg.length / 2 + self.rng.uniform(-20, 20))
            return lon, lat, hdg, road_label(seg)
        else:
            seg = self.graph[self.rng.choice(sorted(run_.area.segs))]
            fwd = run_.area.extra.get("fwd", seg.oneway != -1 if seg.oneway else self.rng.random() < 0.5)
        lon, lat, hdg = point_at(seg, fwd, self.rng.uniform(0, seg.length))
        return lon, lat, hdg, road_label(seg)

    def events(self, dt: float, w: dict) -> list[tuple[str, dict, str]]:
        """The messages of this tick: (topic, message, origin) with origin 'scenario' or 'false_alarm'."""
        out = []
        for sid, reports in list(self.reports.items()):
            run_ = self.running.get(sid)
            kind = run_.kind if run_ else None
            for rep in reports.values():
                if rep.done:
                    continue
                if rep.cancel_at is not None:                # withdraw it, if it was ever sent
                    if self.t >= rep.cancel_at:
                        rep.done = True
                        if rep.version:
                            out.append(self.cancel(rep))
                    continue
                if run_ is None or self.t < rep.due:
                    continue
                spec = ev.SCENARIO_EVENTS[kind]
                where = self.spot(run_)
                if where is None:                            # e.g. the wrong-way driver has left the road
                    rep.cancel_at = self.t
                    continue
                lon, lat, hdg, road = where
                rep.version += 1
                rep.observations += self.rng.randint(1, 3) if rep.provider != "ROAD_OP" else 1
                cause = spec["cause"]
                if spec["category"] == "temporary_slippery_road" and rep.provider not in ev.KNOWS_CAUSE:
                    cause = None                             # no outside temperature: can't tell ice from oil
                msg = ev.srti_event(
                    event_id=rep.event_id, version=rep.version, status="new" if rep.version == 1 else "update",
                    provider=rep.provider, category=spec["category"], cause=cause, lat=lat, lon=lon, heading=hdg,
                    road=road, detected=self.now, valid_until=self.now + timedelta(minutes=spec["valid_min"]),
                    confidence=min(0.98, rep.confidence + 0.02 * (rep.observations - 1)),
                    observations=rep.observations, run_id=self.run_id, sensor_id=rep.sensor_id)
                rep.due = self.t + spec["update_s"] * self.rng.uniform(0.8, 1.2)
                rep.category, rep.lon, rep.lat = spec["category"], lon, lat
                out.append((ev.TOPICS[rep.provider], msg, "scenario"))
            if run_ is None and all(r.done for r in reports.values()):
                del self.reports[sid]
        rate = self.args.false_alarms * dt / 3600
        while self.rng.random() < rate:                      # Poisson-ish: usually 0, rarely more per tick
            out.append(self.false_alarm(w))
            rate -= 1
        return out

    def cancel(self, rep: Report) -> tuple[str, dict, str]:
        rep.version += 1
        msg = ev.srti_event(event_id=rep.event_id, version=rep.version, status="cancel", provider=rep.provider,
                            category=rep.category, cause=None, lat=rep.lat, lon=rep.lon, heading=None, road=None,
                            detected=self.now, valid_until=self.now, confidence=rep.confidence,
                            observations=rep.observations, run_id=self.run_id, sensor_id=rep.sensor_id)
        return ev.TOPICS[rep.provider], msg, "scenario"

    def false_alarm(self, w: dict) -> tuple[str, dict, str]:
        cats, wts = zip(*FALSE_ALARM_CATEGORIES.items())
        cat = self.rng.choices(cats, wts)[0]
        able = [p for p in FLEET if cat in ev.CAPABILITIES[p]]
        provider = self.rng.choices(able, [FLEET[p] for p in able])[0]
        seg = self.segs[self.rng.choices(self.bg_segs, self.bg_weights)[0]]
        fwd = seg.oneway == 1 or (seg.oneway == 0 and self.rng.random() < 0.5)
        lon, lat, hdg = point_at(seg, fwd, self.rng.uniform(0, seg.length))
        cause = None
        if cat == "temporary_slippery_road" and provider in ev.KNOWS_CAUSE:
            cause = "ice" if (w["air_temp_c"] if w["air_temp_c"] is not None else 5) <= 1 else None
        msg = ev.srti_event(event_id=self.new_id(provider), version=1, status="new", provider=provider, category=cat,
                            cause=cause, lat=lat, lon=lon, heading=hdg, road=road_label(seg), detected=self.now,
                            valid_until=self.now + timedelta(minutes=FALSE_ALARM_VALID_MIN),
                            confidence=self.rng.uniform(0.4, 0.7), observations=1, run_id=self.run_id)
        return ev.TOPICS[provider], msg, "false_alarm"

    def step(self, dt: float) -> None:
        self.t += dt
        if self.t - self.traffic_t >= TRAFFIC_EVERY_S:
            caps = {seg: fx["capacity"] for seg, fx in self.seg_fx.items() if "capacity" in fx}
            self.traffic.update(self.now, self.t - self.traffic_t, caps)
            self.traffic_t = self.t

    def readings(self, w: dict) -> list[dict]:
        rows = []
        wet = w["precip_last_hour_mm"] > 0
        wrong_way = [r for r in self.running.values() if r.kind == "wrong_way_driver"]
        for s in self.sensors:
            sid = int(s["segment_id"])
            fx = self.seg_fx.get(sid, {})
            r = {"ts": self.now, "sensor_id": s["sensor_id"], "sensor_type": s["sensor_type"], "surface_temp_c": None,
                 "friction": None, "visibility_m": None, "wrong_way_detected": None, "stationary_vehicle": None,
                 "vehicle_count": None, "avg_speed_kmh": None, "works_active": None}
            if s["sensor_type"] == "road_weather":
                base = w["temp_5cm_c"] if w["temp_5cm_c"] is not None else (w["air_temp_c"] or 5.0)
                surface = round(base - 0.7 + self.rng.gauss(0, 0.2), 1)      # bridge decks run colder
                if "surface_max" in fx:
                    surface = min(surface, fx["surface_max"] + round(self.rng.gauss(0, 0.2), 1))
                friction = (0.6 if wet else 0.8) + self.rng.gauss(0, 0.02)
                if "friction" in fx:
                    friction = self.rng.uniform(*fx["friction"])
                visibility = min(w["visibility_m"], 2000)
                if "visibility" in fx:
                    visibility = self.rng.uniform(*fx["visibility"])
                r.update(surface_temp_c=surface, friction=round(min(max(friction, 0), 1), 2), visibility_m=round(visibility))
            elif s["sensor_type"] == "warning_trailer":   # a road works site: the trailer reports itself
                r["works_active"] = True
            else:
                cam = s["sensor_type"] == "camera"
                traffic = self.traffic.vehicles_on(sid, self.now)
                if traffic:
                    r["vehicle_count"], r["avg_speed_kmh"] = traffic
                if cam:
                    r["wrong_way_detected"] = s["location_kind"] == "motorway_entry" and \
                        any(x.camera_alarm((float(s["lon"]), float(s["lat"]))) for x in wrong_way)
                    r["stationary_vehicle"] = bool(fx.get("stationary"))
            rows.append(r)
        return rows

    def state(self, status: str, first: bool = False) -> None:
        if first:
            insert(self.c, "SIM_STATE", [(self.run_id, status, self.t0.date().isoformat(), self.t0, self.now,
                                          float(self.args.speed), self.args.seed, None, wall_now(), wall_now(),
                                          self.args.transport)])
        else:
            self.c.execute('UPDATE ROAD_SAFETY.SIM_STATE SET "status" = {st}, "sim_now" = {now}, "updated_wall" = {wall} '
                           'WHERE "run_id" = {run!d}', {"st": status, "now": lit(self.now)[1:-1],
                                                      "wall": lit(wall_now())[1:-1], "run": self.run_id})


class MessageLog:
    """Prints every message the partners send. red: caused by a triggered scenario (should lead to an
    incident); yellow: a single false alarm (must never be confirmed); grey: routine sensor readings."""
    GREY, YELLOW, RED, BOLD, RESET = "\033[90m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"

    def __init__(self, sim, mode: str):
        self.sim, self.mode = sim, mode
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.names = {s["sensor_id"]: s.get("sensor_name", "") for s in sim.sensors}

    def c(self, code, text):
        return f"{code}{text}{self.RESET}" if self.color else text

    def legend(self):
        print(self.c(self.BOLD, "Message log: ") + self.c(self.RED, "red = reports of a triggered scenario, ")
              + self.c(self.YELLOW, "yellow = a single false alarm (must not be confirmed), ")
              + self.c(self.GREY, "grey = routine sensor readings"), flush=True)

    def events(self, msgs):
        lines = []
        for topic, m, origin in msgs:
            what = m["srti_category"].replace("_", " ") + (f" ({m['cause']})" if m.get("cause") else "")
            line = (f"{m['detected_at'][11:19]} {m['provider']:<7} {m['status'].upper():<6} {what:<38} "
                    f"{str(m['road'] or '-')[:24]:<24} {m['lat']:.5f},{m['lon']:.5f}  "
                    f"conf {m['confidence']:.2f}  seen by {m['observations']}" +
                    (f"  sensor {m['sensor_id']}" if m.get("sensor_id") else "") + f"  -> {topic}" +
                    ("  [false alarm]" if origin == "false_alarm" else ""))
            lines.append(self.c(self.RED + self.BOLD if origin == "scenario" else self.YELLOW, line))
        if lines:
            print("\n".join(lines), flush=True)

    def readings(self, rows):
        lines = []
        for r in rows:
            alarm = []
            f, s = r["friction"], r["surface_temp_c"]
            if f is not None and f < 0.4 and s is not None and s <= 0:
                alarm.append(f"LOW FRICTION {f:.2f} at {s:.1f}C")
            if r["visibility_m"] is not None and r["visibility_m"] < 150:
                alarm.append(f"VISIBILITY {r['visibility_m']:.0f} m")
            if r["wrong_way_detected"]:
                alarm.append("WRONG-WAY VEHICLE")
            if r["stationary_vehicle"]:
                alarm.append("STATIONARY VEHICLE")
            if r["works_active"]:
                alarm.append("ROAD WORKS SET UP")
            if self.mode == "events" and not alarm:
                continue
            vals = (f"surface {s:.1f}C friction {f:.2f} visibility {r['visibility_m']:.0f} m" if s is not None
                    else "" if r["sensor_type"] == "warning_trailer"
                    else f"{r['vehicle_count'] or 0} vehicles" + (f", {r['avg_speed_kmh']} km/h" if r["avg_speed_kmh"] else ""))
            line = (f"{r['ts']:%H:%M:%S} ROAD_OP SENSOR {r['sensor_id']:<7} {str(self.names.get(r['sensor_id'], ''))[:40]:<40} "
                    f"{vals}" + ("  " + ", ".join(alarm) if alarm else ""))
            lines.append(self.c(self.RED + self.BOLD if alarm else self.GREY, line))
        if lines:
            print("\n".join(lines), flush=True)


class IncidentWatcher(threading.Thread):
    """Polls ROAD_SAFETY.INCIDENTS (written by scripts/road_safety_service.py) and prints a banner when an
    incident is confirmed or cleared, a line when it changes. Only the combination SQL decides."""

    def __init__(self, deployment: str, log: MessageLog, run_id: int, every_s: float = 2.0):
        super().__init__(daemon=True)
        self.c, self.log, self.run_id, self.every = connect(deployment), log, run_id, every_s
        self.seen: dict[str, int] = {}

    def run(self):
        while True:
            try:
                rows = list(self.c.execute('SELECT * FROM ROAD_SAFETY.INCIDENTS WHERE "run_id" = {r!d}', {"r": self.run_id}))
                for h in rows:
                    v = int(h["version"])
                    if self.seen.get(h["incident_id"]) != v:
                        self.show(h, first=h["incident_id"] not in self.seen)
                        self.seen[h["incident_id"]] = v
            except pyexasol.ExaQueryError:
                pass                                   # INCIDENTS doesn't exist until the service has started once
            except Exception as e:  # noqa: BLE001 - keep watching
                print(f"(incident watcher: {type(e).__name__})", flush=True)
            time.sleep(self.every)

    def show(self, h, first: bool):
        what = h["srti_category"].replace("_", " ") + (f" (cause: {h['cause']})" if h["cause"] else "")
        road = h["road"] or "unnamed road"
        red = self.log.RED + self.log.BOLD
        if h["status"] == "cleared":
            print(self.log.c(red, f">>> INCIDENT CLEARED: {h['incident_id']} {what} on {road}"), flush=True)
            return
        if not first:
            n, m = int(h["providers"]), int(h["sensors"])
            print(self.log.c(red, f">>> incident {h['incident_id']} updated: {what} on {road}, {n} provider{'s' * (n != 1)}, "
                                  f"{m} sensor{'s' * (m != 1)}, confidence {float(h['confidence']):.2f}"), flush=True)
            return
        secs = int((h["confirmed_at"] - h["first_report_at"]).total_seconds())
        reach = (f"about {int(h['reach_vehicles']):,} vehicles " + ("in the area" if h["reach_kind"] == "in_area"
                                                                    else "pass it in the next 10 minutes")
                 if h["reach_vehicles"] is not None else "reach unknown")
        bar = "=" * 100
        lines = [bar, f">>> INCIDENT CONFIRMED BY EXASOL: {what.upper()} on {road} ({h['incident_id']})",
                 f"    first report {h['first_report_at']:%H:%M:%S}, confirmed {h['confirmed_at']:%H:%M:%S} ({secs} s later) "
                 f"by {h['rule']}: " + " + ".join(x for x in (h["provider_list"],
                                                              f"sensors {h['sensor_list']}" if h["sensor_list"] else "") if x),
                 f"    at {float(h['lat']):.5f},{float(h['lon']):.5f}, confidence {float(h['confidence']):.2f}; {reach}. "
                 "Published to srti.confirmed.", bar]
        print("\n".join(self.log.c(red, ln) for ln in lines), flush=True)


def run(args) -> None:
    c = connect(args.deployment)
    c.execute("UPDATE ROAD_SAFETY.SIM_STATE SET \"status\" = 'stopped' WHERE \"status\" = 'running'")
    c.execute('UPDATE ROAD_SAFETY.SCENARIO_LOG SET "ended_at" = "started_at" WHERE "ended_at" IS NULL')
    print("Loading road network, sensors, traffic and weather ...")
    sim = Sim(c, args)
    sim.state("running", first=True)
    where = f"Kafka at {KAFKA}" if args.transport == "kafka" else "ROAD_SAFETY.INBOUND_MESSAGES (direct, no Kafka)"
    print(f"SIMULATION run {sim.run_id}: {len(FLEET)} providers report SRTI events, the road operator "
          f"{len(sim.sensors)} sensors; weather of {args.weather_day} (DWD {STATION}), starting {sim.now:%Y-%m-%d %H:%M}, "
          f"speed x{args.speed:g}, about {args.false_alarms:g} false alarms per hour; sending to {where}")
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    log = MessageLog(sim, args.log) if args.log else None
    if log:
        log.legend()
        IncidentWatcher(args.deployment, log, sim.run_id).start()
    counts = defaultdict(int)
    try:
        while not stop["flag"]:
            t_wall = time.monotonic()
            for note in sim.refresh_scenarios():
                print(f"  {note}", flush=True)
            sim.speed = float(c.execute('SELECT "speed" FROM ROAD_SAFETY.SIM_STATE WHERE "run_id" = {r!d}',
                                        {"r": sim.run_id}).fetchval() or sim.speed)
            sim_dt = args.tick * sim.speed
            sim.step(sim_dt)
            w = sim.weather.at(sim.now)
            msgs = sim.events(sim_dt, w)
            for topic, m, origin in msgs:
                sim.pub.send(topic, m["event_id"], m)
                counts[origin] += 1
            readings = []
            if sim.t >= sim.next_sensor:
                readings = sim.readings(w)
                sim.next_sensor += SENSOR_EVERY_S
                for r in readings:
                    sim.pub.send(ev.TOPICS["ROAD_OP"], r["sensor_id"], ev.sensor_reading(r, sim.run_id))
                counts["sensor"] += len(readings)
            sim.pub.flush()
            sim.state("running")
            if log:
                log.events(msgs)
                log.readings(readings)
            elif args.verbose or int(sim.t) % 60 < sim_dt:
                active = ", ".join(f"{r.kind}" for r in sim.running.values()) or "none"
                print(f"  {sim.now:%H:%M:%S}  x{sim.speed:g}  events {counts['scenario']:,} from scenarios, "
                      f"{counts['false_alarm']:,} false alarms, {counts['sensor']:,} sensor readings; "
                      f"traffic: {sim.traffic.summary()}; scenarios: {active}", flush=True)
            if args.duration and sim.t >= args.duration:
                break
            time.sleep(max(0.0, args.tick - (time.monotonic() - t_wall)))
    except KeyboardInterrupt:
        pass
    finally:
        sim.pub.flush()
        sim.state("stopped")
        print(f"Stopped at {sim.now:%H:%M:%S}: {counts['scenario']:,} scenario events, {counts['false_alarm']:,} "
              f"false alarms, {counts['sensor']:,} sensor readings sent")


# --------------------------------------------------------------------------- triggers
def current_run(c) -> dict:
    row = c.execute("SELECT * FROM ROAD_SAFETY.SIM_STATE WHERE \"status\" = 'running' ORDER BY \"run_id\" DESC LIMIT 1").fetchone()
    if not row:
        sys.exit("error: no simulator is running. Start it first: simulate_road_safety.py run")
    return row


def banner(lines: list[str]) -> None:
    bar = "=" * 78
    print(bar)
    for ln in lines:
        print(ln)
    print(bar, flush=True)


def provider_id(text: str) -> str:
    t = text.strip().upper().replace("-", "_").replace(" ", "_")
    t = {"A": "OEM_A", "B": "OEM_B", "C": "OEM_C", "D": "OEM_D", "E": "OEM_E", "F": "OEM_F", "NAVIGATION": "NAV",
         "ROAD_OPERATOR": "ROAD_OP", "OPERATOR": "ROAD_OP"}.get(t, t)
    if not t.startswith(("OEM_", "NAV", "ROAD_OP")) and len(t) == 4 and t.startswith("OEM"):
        t = "OEM_" + t[3]
    if t not in ev.TOPICS:
        sys.exit(f"error: unknown provider '{text}'. Providers: {', '.join(ev.TOPICS)}")
    return t


def per_provider(text: str | None, kind: str) -> dict:
    """'20' -> {'*': 20.0}; 'OEM_A=20,OEM_D=50' -> {'OEM_A': 20.0, 'OEM_D': 50.0}"""
    if text is None:
        return {}
    out = {}
    if kind == "delay" and text.strip().lower() in ("realistic", "stage"):
        return {}
    for part in text.split(","):
        if "=" in part:
            p, v = part.split("=", 1)
            out[provider_id(p)] = float(v)
        else:
            out["*"] = float(part)
    if kind == "confidence" and any(not 0 < v <= 1 for v in out.values()):
        sys.exit("error: --confidence takes values between 0 and 1")
    return out


def steering_from(args, kind: str, sim_now: datetime, t0: datetime) -> dict | None:
    if args.providers is None and args.delay is None and args.confidence is None and args.sensor is None:
        return None
    category = ev.SCENARIO_EVENTS[kind]["category"]
    st: dict = {"since_t": (sim_now - t0).total_seconds() if sim_now and t0 else 0.0}
    if args.providers is not None:
        providers = [provider_id(p) for p in args.providers.split(",") if p.strip()]
        if "ROAD_OP" in providers:
            sys.exit("error: the road operator reports through its sensors: use --sensor")
        unable = [p for p in providers if category not in ev.CAPABILITIES[p]]
        if unable:
            able = [p for p in ev.CAPABILITIES if category in ev.CAPABILITIES[p] and p != "ROAD_OP"]
            sys.exit(f"error: {', '.join(unable)} can't report {category.replace('_', ' ')} (see srti_events.py). "
                     f"Providers that can: {', '.join(able)}")
        st["providers"] = {p: None for p in providers}         # the delay comes from the profile
    if args.delay is not None:
        mode = args.delay.strip().lower()
        if mode in ("realistic", "stage"):
            st["delay_mode"] = mode
        else:
            st["delay"] = per_provider(args.delay, "delay")
    if args.confidence is not None:
        st["confidence"] = per_provider(args.confidence, "confidence")
    if args.sensor is not None:
        st["sensor"] = args.sensor
    return st


def describe_steering(kind: str, st: dict | None) -> str:
    providers, sensor, sensor_delay = ev.plan_delays(kind, st)
    who = ", ".join(f"{p} after {d:g} s" for p, d in providers.items()) or "no carmaker"
    return (f"reported by {who}; road operator sensor: {f'yes, after {sensor_delay:g} s' if sensor else 'no'}"
            + (" (realistic delays)" if (st or {}).get("delay_mode") == "realistic" else ""))


def merge_steering(old: dict, new: dict) -> dict:
    out = dict(old)
    for k, v in new.items():
        if k in ("delay", "confidence") and isinstance(v, dict):
            out[k] = {**(old.get(k) or {}), **v}
        else:
            out[k] = v
    return out


def trigger(args) -> None:
    c = connect(args.deployment)
    info = rs.SCENARIOS[args.scenario]
    run_ = current_run(c) if not args.dry_run else {"run_id": 0, "sim_now": None, "sim_start": None}
    sim_now = run_["sim_now"]
    if args.stop:
        rows = list(c.execute('SELECT "scenario_id", "area" FROM ROAD_SAFETY.SCENARIO_LOG WHERE "run_id" = {r!d} '
                              'AND "scenario_type" = {t} AND "ended_at" IS NULL', {"r": run_["run_id"], "t": args.scenario}))
        if not rows:
            sys.exit(f"no active {args.scenario} scenario")
        c.execute('UPDATE ROAD_SAFETY.SCENARIO_LOG SET "ended_at" = {now} WHERE "run_id" = {r!d} AND "scenario_type" = {t} '
                  'AND "ended_at" IS NULL', {"now": lit(sim_now)[1:-1], "r": run_["run_id"], "t": args.scenario})
        banner([f'SIMULATION: stopped "{info["label"]}" (scenario {", ".join(str(r["scenario_id"]) for r in rows)}), '
                f'{sim_now:%H:%M:%S}',
                "  The providers withdraw their events (cancel). Incidents clear when no valid event supports them."])
        return
    steering = steering_from(args, args.scenario, sim_now, run_.get("sim_start"))
    placed = args.area or args.road or args.near or args.at
    if not placed and steering is not None and not args.dry_run:   # steer the running scenario
        row = c.execute('SELECT "scenario_id", "steering" FROM ROAD_SAFETY.SCENARIO_LOG WHERE "run_id" = {r!d} '
                        'AND "scenario_type" = {t} AND "ended_at" IS NULL ORDER BY "scenario_id" DESC LIMIT 1',
                        {"r": run_["run_id"], "t": args.scenario}).fetchone()
        if row:
            merged = merge_steering(json.loads(row["steering"]) if row["steering"] else {}, steering)
            c.execute('UPDATE ROAD_SAFETY.SCENARIO_LOG SET "steering" = {s} WHERE "scenario_id" = {i!d}',
                      {"s": json.dumps(merged), "i": row["scenario_id"]})
            banner([f'SIMULATION: "{info["label"]}" (scenario {row["scenario_id"]}) is now '
                    f'{describe_steering(args.scenario, merged)}, {sim_now:%H:%M:%S}',
                    "  Delays count from now. Providers no longer listed withdraw their events."])
            return
    graph, _ = load_graph(c)
    notes = []
    try:
        spec = args.area
        if args.road or args.near or args.at:
            spec, _, notes = rs.locate(args.scenario, graph, c, road=args.road, near=args.near,
                                       direction=args.direction, at=args.at)
        area = rs.resolve(args.scenario, spec, graph, c)
    except ValueError as e:
        sys.exit(f"error: {e}. Where {args.scenario} can go: {info['areas']}")
    if args.dry_run:
        print(f'would inject "{info["label"]}" on {area.description} ({area.name}, {len(area.segs)} road segments), '
              f'{describe_steering(args.scenario, steering)}')
        for n in notes:
            print(f"  note: {n}")
        return
    if c.execute('SELECT COUNT(*) FROM ROAD_SAFETY.SCENARIO_LOG WHERE "run_id" = {r!d} AND "scenario_type" = {t} '
                 'AND "area" = {a} AND "ended_at" IS NULL',
                 {"r": run_["run_id"], "t": args.scenario, "a": area.name}).fetchval():
        sys.exit(f"{args.scenario} on {area.description} is already running. To change who reports it, leave out "
                 f"the place: trigger {args.scenario} --providers ...")
    sid = int(c.execute('SELECT COALESCE(MAX("scenario_id"), 0) + 1 FROM ROAD_SAFETY.SCENARIO_LOG').fetchval())
    insert(c, "SCENARIO_LOG", [(sid, run_["run_id"], args.scenario, area.name, area.wkt, sim_now, None, wall_now(),
                                args.by, info["injects"], json.dumps(steering) if steering else None)])
    banner([f'SIMULATION: injecting "{info["label"]}" on {area.description}, {sim_now:%H:%M:%S}',
            *[f"  note: {n}" for n in notes],
            f"  scenario {sid} in run {run_['run_id']} (simulation time {sim_now:%Y-%m-%d %H:%M:%S})",
            f"  {describe_steering(args.scenario, steering)}",
            f"  injects: {info['injects']}",
            "  The partners send events and sensor readings only. No hazard is written: confirming it is up to the SQL.",
            f"  stop it with: simulate_road_safety.py trigger {args.scenario} --stop"])


def set_speed(args) -> None:
    c = connect(args.deployment)
    run_ = current_run(c)
    c.execute('UPDATE ROAD_SAFETY.SIM_STATE SET "speed" = {s!f} WHERE "run_id" = {r!d}', {"s": args.factor, "r": run_["run_id"]})
    print(f"SIMULATION speed: x{args.factor:g} from {run_['sim_now']:%H:%M:%S} "
          f"({args.factor:g} simulated seconds per real second)")


def stop_all(args) -> None:
    c = connect(args.deployment)
    run_ = current_run(c)
    n = c.execute('SELECT COUNT(*) FROM ROAD_SAFETY.SCENARIO_LOG WHERE "ended_at" IS NULL').fetchval()
    c.execute('UPDATE ROAD_SAFETY.SCENARIO_LOG SET "ended_at" = {now} WHERE "ended_at" IS NULL',
              {"now": lit(run_["sim_now"])[1:-1]})
    banner([f"SIMULATION: stopped all scenarios ({n}), {run_['sim_now']:%H:%M:%S}",
            "  The providers withdraw their events (cancel). Incidents clear when no valid event supports them."])


def scenarios(args) -> None:
    for k, v in rs.SCENARIOS.items():
        print(f"{k:18s} {v['label']}\n{'':18s} where: {v['areas']}\n"
              f"{'':18s} injects: {v['injects']}\n{'':18s} {describe_steering(k, None)}\n")


COMBINE_DEFAULTS = {"min_providers": 2, "sensor_replaces": 1, "window_min": 30, "heading_max": 60, "friction_max": 0.4,
                    "visibility_max": 150, "blockage_after_s": 600, "reach_min": 10}
OLD_VIEWS = ["V_HAZARDS_LIVE", "V_HAZARD_EVIDENCE", "V_HAZARD_REACH", "V_TRAFFIC_STATE", "V_CONGESTION",
             "V_CONGESTION_WARNINGS", "V_DIVERSIONS"]


def views(args) -> None:
    """V_INCIDENTS_LIVE: the combination (roadsafety/combine.sql) with the default rule, for questions in SQL.
    Drops the views and the routing UDF of the raw-signal version."""
    c = connect(args.deployment)
    for v in OLD_VIEWS:
        c.execute(f"DROP VIEW IF EXISTS ROAD_SAFETY.{v}")
    c.execute("DROP SCRIPT IF EXISTS ROAD_SAFETY.DIVERSIONS")
    sql = c.format.format((ROOT / "roadsafety" / "combine.sql").read_text(), **COMBINE_DEFAULTS).strip().rstrip(";")
    c.execute(f"CREATE OR REPLACE VIEW ROAD_SAFETY.V_INCIDENTS_LIVE AS\n{sql}")
    print("View ROAD_SAFETY.V_INCIDENTS_LIVE created (combine.sql with "
          + ", ".join(f"{k}={v}" for k, v in COMBINE_DEFAULTS.items()) + ")")


def status(args) -> None:
    c = connect(args.deployment)
    for r in c.execute('SELECT * FROM ROAD_SAFETY.SIM_STATE ORDER BY "run_id" DESC LIMIT 3'):
        print({k: str(v) for k, v in r.items()})
    print(f"  SCENARIO_LOG: {c.execute('SELECT COUNT(*) FROM ROAD_SAFETY.SCENARIO_LOG').fetchval():,} rows")
    try:
        for r in c.execute('SELECT "topic", COUNT(*) AS "n", MAX("loaded_at") AS "last_loaded" FROM ROAD_SAFETY.INBOUND_MESSAGES '
                           'GROUP BY 1 ORDER BY 1'):
            print(f"  INBOUND_MESSAGES {r['topic']}: {int(r['n']):,} messages, last loaded {r['last_loaded']}")
    except pyexasol.ExaQueryError:
        print("  INBOUND_MESSAGES: not created yet (the service creates it)")


def reset(args) -> None:
    if not args.yes:
        sys.exit("reset empties SENSOR_READINGS, SCENARIO_LOG and SIM_STATE; add --yes")
    c = connect(args.deployment)
    for t in ("SENSOR_READINGS", "SCENARIO_LOG", "SIM_STATE"):
        c.execute(f"TRUNCATE TABLE ROAD_SAFETY.{t}")
    print("ROAD_SAFETY scenario and state tables emptied (providers, sensors and INBOUND_MESSAGES kept)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--deployment", default="road-safety-demo", help="Exasol Personal deployment name")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="create ROAD_SAFETY with providers and sensors")
    p = sub.add_parser("run", help="run the partners: SRTI events, sensor readings, false alarms")
    p.add_argument("--speed", type=float, default=1.0, help="simulation seconds per wall second")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--weather-day", default="2026-10-27", help="the simulated day: DWD weather and roadworks of it")
    p.add_argument("--start", default="06:00", help="local start time of the simulation")
    p.add_argument("--tick", type=float, default=1.0, help="wall seconds per batch")
    p.add_argument("--duration", type=float, default=0, help="stop after this many simulation seconds")
    p.add_argument("--false-alarms", type=float, default=20.0, help="single unconfirmed reports per simulation hour")
    p.add_argument("--transport", choices=["kafka", "direct"], default="kafka",
                   help="kafka (default): one topic per provider; direct: straight into INBOUND_MESSAGES")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--log", nargs="?", const="all", choices=["all", "events"],
                   help="print every message sent (all) or leave out routine sensor readings (events)")
    sub.add_parser("views", help="create V_INCIDENTS_LIVE (the combination with the default rule)")
    sub.add_parser("status", help="show the current run and message counts")
    p = sub.add_parser("trigger", help="inject a scenario into the running simulation (visibly), or steer it")
    p.add_argument("scenario", type=lambda k: rs.ALIASES.get(k.lower().replace("-", "_"), k.lower().replace("-", "_")),
                   choices=list(rs.SCENARIOS))
    p.add_argument("--area", help="a named area of the first version (see `scenarios`)")
    p.add_argument("--road", help='road number or street name, e.g. "A9", "B2R", "Leopoldstraße"')
    p.add_argument("--near", help='a place: suburb, village, motorway junction, station, landmark or street, e.g. "Garching-Süd"')
    p.add_argument("--direction", help='north/south/east/west, in/out (of the city) or "toward <place>"')
    p.add_argument("--at", help='coordinates "lat,lon"')
    p.add_argument("--providers", help='who reports it, e.g. "OEM_A" or "OEM_A,OEM_D" (default: per scenario, see `scenarios`)')
    p.add_argument("--delay", help='seconds until a provider reports: "30" for all, "OEM_A=20,OEM_D=50", or the profile '
                                   '"realistic" (a real backend\'s pace) instead of the default stage pace (5, 12, 20 s)')
    p.add_argument("--confidence", help='provider confidence 0..1, "0.6" for all or "OEM_A=0.9"')
    p.add_argument("--sensor", action=argparse.BooleanOptionalAction, default=None,
                   help="the road operator's sensor reports it too (--no-sensor: it doesn't)")
    p.add_argument("--dry-run", action="store_true", help="only show where it would go")
    p.add_argument("--stop", action="store_true", help="end the active scenario of this type")
    p.add_argument("--by", default="presenter", help="who triggered it (written to SCENARIO_LOG)")
    sub.add_parser("stop-all", help="end all active scenarios")
    p = sub.add_parser("speed", help="change the simulation speed while it runs")
    p.add_argument("factor", type=float, help="simulated seconds per real second, e.g. 20 for the build-up, 1 for incidents")
    sub.add_parser("scenarios", help="list the scenarios that can be triggered, and who reports them")
    p = sub.add_parser("reset", help="empty the scenario and state tables")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"setup": setup, "run": run, "views": views, "status": status, "reset": reset,
     "trigger": trigger, "stop-all": stop_all, "scenarios": scenarios, "speed": set_speed}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
