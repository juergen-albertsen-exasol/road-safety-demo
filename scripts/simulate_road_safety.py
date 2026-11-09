#!/usr/bin/env python3
"""Simulate connected vehicles and road sensors in Munich for the road safety demo (schema ROAD_SAFETY).

Everything this script writes is SYNTHETIC. The road network, weather and accident history it builds
on are real open data in ROAD_DATA (scripts/prepare_road_data.py). Design: docs/plan.md.

    .venv/bin/python scripts/simulate_road_safety.py setup            # schema, providers, vehicles, sensors
    .venv/bin/python scripts/simulate_road_safety.py run              # background stream, Ctrl-C to stop
    .venv/bin/python scripts/simulate_road_safety.py views            # detection views from roadsafety/*.sql
    .venv/bin/python scripts/simulate_road_safety.py status
    .venv/bin/python scripts/simulate_road_safety.py reset --yes      # empty the stream tables

`run` options: --speed (simulation seconds per wall second, default 1), --seed, --weather-day
(default 2026-10-27), --start (local time, default 06:00), --tick (wall seconds per batch, default 2).
--log prints every signal it sends (grey; yellow = background noise, red = caused by a triggered
scenario), the sensor readings (red = alert), and every incident the detection SQL confirms (a red
banner, polled from V_HAZARD_REACH every 3 s). --log events prints only the event signals, sensor
alerts and incidents. For a separate terminal during the demo:

    .venv/bin/python scripts/simulate_road_safety.py run --start 07:30 --log

Vehicles drive along the OSM segments, report every 10 s of simulation time, and produce some
isolated noise on purpose (single hard brakings, single ESC interventions, hazard lights) so the
detection rule has something to filter out. Weather follows the real DWD 10-minute values of the
weather day at station 3379 Muenchen-Stadt.

Scenarios are injected on purpose and visibly, while `run` is running:

    .venv/bin/python scripts/simulate_road_safety.py scenarios                       # what can be triggered
    .venv/bin/python scripts/simulate_road_safety.py trigger wrong_way_driver --road A9 --direction south --near Garching-Süd
    .venv/bin/python scripts/simulate_road_safety.py trigger accident --road Leopoldstraße --near "Münchner Freiheit"
    .venv/bin/python scripts/simulate_road_safety.py trigger fog --near Aubing
    .venv/bin/python scripts/simulate_road_safety.py trigger roadworks --road A99 --near "Kreuz München-Nord" --dry-run
    .venv/bin/python scripts/simulate_road_safety.py trigger slippery_road --area isar-bridges
    .venv/bin/python scripts/simulate_road_safety.py trigger slippery_road --stop
    .venv/bin/python scripts/simulate_road_safety.py stop-all

A trigger writes one row into SCENARIO_LOG and prints a SIMULATION banner. The running simulator
picks it up within one tick and changes the signals of the vehicles and sensors in the area. It never
writes a hazard: whether one appears is decided by the detection SQL (roadsafety/hazards.sql).

Connects as `sys` to the Exasol Personal deployment (--deployment, default road-safety-demo); the
password is read from the deployment's secrets.json and never printed. Writes only to ROAD_SAFETY.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import os
import signal
import ssl
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyexasol

import road_scenarios as rs
import road_traffic as rt

ROOT = Path(__file__).resolve().parent.parent
BERLIN = ZoneInfo("Europe/Berlin")
STATION = 3379

PROVIDERS = [  # id, name, kind, dynamics, environment, fleet share %
    ("OEM_A", "OEM A", "oem", True, True, 24.0),
    ("OEM_B", "OEM B", "oem", True, True, 19.0),
    ("OEM_C", "OEM C", "oem", True, True, 14.0),
    ("OEM_D", "OEM D", "oem", True, True, 11.0),
    ("OEM_E", "OEM E", "oem", True, False, 9.0),
    ("OEM_F", "OEM F", "oem", True, False, 6.0),
    ("NAV", "Navigation service", "navigation", False, False, 17.0),
    ("ROAD_OP", "Road operator", "road_operator", None, None, None),
]
VEHICLE_TYPES = [("car", 85), ("van", 8), ("truck", 5), ("bus", 2)]

CLASS_WEIGHT = {  # how strongly traffic is drawn to a road class
    "motorway": 8, "motorway_link": 2, "trunk": 6, "trunk_link": 2, "primary": 5, "primary_link": 1.5,
    "secondary": 3.5, "secondary_link": 1, "tertiary": 2, "tertiary_link": 1, "unclassified": 0.3,
    "residential": 0.1, "living_street": 0.02,
}
DEFAULT_KMH = {"motorway": 130, "motorway_link": 80, "trunk": 80, "trunk_link": 60, "living_street": 7,
               "residential": 30}

# Background noise, events per vehicle and hour of simulation time.
NOISE_PER_HOUR = {"hard_braking": 0.3, "esc": 0.05, "abs": 0.03, "hazard_lights": 0.05}
NOISE_MIN_SPEED = 5.0          # m/s: no ESC/ABS or hard braking noise while crawling in a jam
FREEZING_ESC_FACTOR = 3.0      # ESC/ABS a bit more often when the outside temperature is <= 1 C
REPORT_EVERY_S = 15
TRAFFIC_EVERY_S = 10
SENSOR_EVERY_S = 60


# --------------------------------------------------------------------------- database
def connect(deployment: str) -> pyexasol.ExaConnection:
    secrets = Path.home() / ".exasol" / "personal" / "deployments" / deployment / "secrets.json"
    if not secrets.exists():
        sys.exit(f"error: {secrets} not found; is the Exasol Personal deployment '{deployment}' installed?")
    password = json.loads(secrets.read_text())["dbPassword"]
    return pyexasol.connect(dsn="127.0.0.1:8563", user="sys", password=password, encryption=True,
                            websocket_sslopt={"cert_reqs": ssl.CERT_NONE}, compression=True,
                            autocommit=True, fetch_dict=True,
                            fetch_mapper=pyexasol.exasol_mapper)


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "'"
    return "'" + str(v).replace("'", "''") + "'"


def insert(c, table: str, rows: list[tuple], chunk: int = 4000) -> None:
    for i in range(0, len(rows), chunk):
        values = ",".join("(" + ",".join(lit(v) for v in r) + ")" for r in rows[i:i + chunk])
        c.execute(f"INSERT INTO ROAD_SAFETY.{table} VALUES {values}")


def wall_now() -> datetime:
    return datetime.now(BERLIN).replace(tzinfo=None)


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

    insert(c, "DATA_PROVIDERS", PROVIDERS)
    rng = random.Random(args.seed)
    segs, _ = load_graph(c)
    ids = list(segs)
    capw = {int(r["segment_id"]): float(r["capacity_vph"]) / 1000
            for r in c.execute('SELECT "segment_id", "capacity_vph" FROM ROAD_DATA.SEGMENT_TRAFFIC')}
    weights = [segs[i].length * CLASS_WEIGHT.get(segs[i].road_class, 0.5) * capw.get(i, 0.5) for i in ids]
    fleet = [p for p in PROVIDERS if p[5]]
    vehicles = []
    for vid in range(1, args.vehicles + 1):
        prov = rng.choices([p[0] for p in fleet], [p[5] for p in fleet])[0]
        vtype = rng.choices([t for t, _ in VEHICLE_TYPES], [w for _, w in VEHICLE_TYPES])[0]
        vehicles.append((vid, prov, vtype, rng.choices(ids, weights)[0]))
    insert(c, "VEHICLES", vehicles)
    print(f"  {len(vehicles):,} vehicles from {len(fleet)} providers")

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


# --------------------------------------------------------------------------- run
@dataclass
class Vehicle:
    vid: int
    provider: str
    vtype: str
    seg: int
    fwd: bool
    pos: float
    speed: float = 0.0          # m/s
    target: float = 0.0
    driver: float = 1.0
    temp_bias: float = 0.0
    next_report: float = 0.0
    special: int = 0            # scenario id that took the vehicle over (crash, wrong-way driver)
    frozen: bool = False        # stands still (crashed)
    wrong: bool = False         # drives against the one-way direction
    airbag: bool = False        # deployed: reported in every signal
    hazard_on: bool = False     # hazard lights stay on
    cool_until: float = 0.0     # no new scenario event before this simulation second
    lonlat: tuple = (0.0, 0.0)


class Sim:
    def __init__(self, c, args):
        self.c, self.args = c, args
        self.rng = random.Random(args.seed)
        self.graph, self.exits = load_graph(c)
        self.segs = self.graph
        self.ids = list(self.segs)
        # probes follow the traffic: road class x capacity (lanes), so busy roads carry more probe vehicles
        self.capw = {int(r["segment_id"]): float(r["capacity_vph"]) / 1000
                     for r in c.execute('SELECT "segment_id", "capacity_vph" FROM ROAD_DATA.SEGMENT_TRAFFIC')}
        self.weights = [self.segs[i].length * self.route_weight(i) for i in self.ids]
        prov = {r["provider_id"]: r for r in c.execute("SELECT * FROM ROAD_SAFETY.DATA_PROVIDERS")}
        self.dyn = {k: bool(v["shares_vehicle_dynamics"]) for k, v in prov.items()}
        self.env = {k: bool(v["shares_environment"]) for k, v in prov.items()}
        self.vehicles = []
        for r in c.execute('SELECT * FROM ROAD_SAFETY.VEHICLES ORDER BY "vehicle_id"'):
            seg = self.segs.get(int(r["home_segment_id"])) or self.segs[self.rng.choice(self.ids)]
            fwd = seg.oneway == 1 or (seg.oneway == 0 and self.rng.random() < 0.5)
            v = Vehicle(int(r["vehicle_id"]), r["provider_id"], r["vehicle_type"], seg.sid, fwd,
                        self.rng.uniform(0, seg.length), driver=self.rng.uniform(0.9, 1.05),
                        temp_bias=self.rng.gauss(0, 0.4), next_report=self.rng.uniform(0, REPORT_EVERY_S))
            self.set_target(v)
            v.speed = v.target
            self.vehicles.append(v)
        if not self.vehicles:
            sys.exit("error: no vehicles; run `setup` first")
        self.sensors = list(c.execute('SELECT * FROM ROAD_SAFETY.INFRA_SENSORS'))
        day = datetime.strptime(args.weather_day, "%Y-%m-%d")
        self.weather = Weather(c, day)
        h, m = map(int, args.start.split(":"))
        self.t0 = day.replace(hour=h, minute=m)
        self.t = 0.0                    # simulation seconds since t0
        self.next_sensor = 0.0
        self.run_id = int(c.execute('SELECT COALESCE(MAX("run_id"), 0) + 1 FROM ROAD_SAFETY.SIM_STATE').fetchval())
        self.running: dict[int, rs.Running] = {}
        self.seg_fx: dict[int, dict] = {}
        self.speed = float(args.speed)
        self.out_src: list[str] = []            # per signal of a tick: "scenario", "noise" or ""
        self.traffic = rt.TrafficModel(c, self.graph)
        self.traffic.update(self.t0, 0.0, {})
        self.traffic_t = 0.0
        for v in self.vehicles:
            self.set_target(v)
            v.speed = v.target

    @property
    def now(self) -> datetime:
        return self.t0 + timedelta(seconds=self.t)

    def route_weight(self, sid: int) -> float:
        return CLASS_WEIGHT.get(self.segs[sid].road_class, 0.5) * self.capw.get(sid, 0.5)

    def set_target(self, v: Vehicle, w: dict | None = None) -> None:
        s = self.segs[v.seg]
        kmh = s.kmh
        if v.wrong:
            v.target = 100 / 3.6
            return
        traffic = getattr(self, "traffic", None)
        main = traffic.kmh(v.seg, v.fwd) if traffic else None
        if main is not None:                         # main road: the speed the traffic allows
            limit = {"truck": 85, "bus": 80, "van": 120}.get(v.vtype, 999)
            v.target = max(min(main * v.driver, limit) / 3.6, 0.5)
            return
        if s.road_class in ("motorway", "trunk"):
            kmh *= self.rng.uniform(0.85, 1.1)
            if v.vtype in ("truck", "bus"):
                kmh = min(kmh, 85)
            elif v.vtype == "van":
                kmh = min(kmh, 120)
        else:
            kmh *= self.rng.uniform(0.6, 0.95)       # city traffic, lights, turning
        if w and w["air_temp_c"] is not None and w["air_temp_c"] <= 1:
            kmh *= 0.95
        v.target = max(kmh * v.driver / 3.6, 2.0)

    def respawn(self, v: Vehicle) -> None:
        seg = self.segs[self.rng.choices(self.ids, self.weights)[0]]
        v.seg, v.fwd, v.pos = seg.sid, seg.oneway != -1, 0.0
        self.set_target(v)

    def next_segment(self, v: Vehicle) -> None:
        s = self.segs[v.seg]
        if v.wrong:                                  # keep going backwards along the carriageway
            cands = [sid for sid in self.graph.into.get(s.from_node, ())
                     if self.segs[sid].road_class == s.road_class and self.segs[sid].oneway == 1]
            if not cands:                            # end of the carriageway: the driver leaves the road
                v.wrong = False
                v.special = 0
                self.respawn(v)
                return
            same = [sid for sid in cands if (s.ref and self.segs[sid].ref == s.ref) or (s.name and self.segs[sid].name == s.name)]
            v.seg, v.fwd, v.pos = (same or cands)[0], False, 0.0
            return
        node = s.to_node if v.fwd else s.from_node
        cands = [(sid, fwd) for sid, fwd in self.exits.get(node, ()) if sid != v.seg]
        if not cands:
            if s.oneway == 0:                        # dead end: turn around
                v.fwd, v.pos = not v.fwd, 0.0
                return
            self.respawn(v)                          # left the network
            return
        wts = []
        for sid, _ in cands:
            n = self.segs[sid]
            wt = self.route_weight(sid)
            if (s.name and n.name == s.name) or (s.ref and n.ref == s.ref):
                wt *= 4                              # keep following the same road
            wts.append(wt)
        v.seg, v.fwd = self.rng.choices(cands, wts)[0]
        v.pos = 0.0

    # ---- scenarios
    def refresh_scenarios(self) -> None:
        rows = list(self.c.execute('SELECT "scenario_id", "scenario_type", "area", "started_at", "ended_at" '
                                   'FROM ROAD_SAFETY.SCENARIO_LOG WHERE "run_id" = {run!d}', {"run": self.run_id}))
        for r in rows:
            sid = int(r["scenario_id"])
            if r["ended_at"] is None and sid not in self.running:
                try:
                    self.running[sid] = rs.Running(r, self)
                    print(f"  >> SIMULATION scenario {sid} active: {r['scenario_type']} on "
                          f"{self.running[sid].area.description}", flush=True)
                except ValueError as e:
                    print(f"  !! scenario {sid} ignored: {e}", flush=True)
            elif r["ended_at"] is not None and sid in self.running:
                self.running.pop(sid).stop(self)
                print(f"  >> SIMULATION scenario {sid} ended: {r['scenario_type']}", flush=True)
        per_seg: dict[int, list] = defaultdict(list)
        for run_ in self.running.values():
            for seg, fx in run_.effects(self).items():
                per_seg[seg].append(fx)
            if run_.kind == "storm":
                self.c.execute('UPDATE ROAD_SAFETY.SCENARIO_LOG SET "area_wkt" = {w} WHERE "scenario_id" = {id!d}',
                               {"w": run_.area.wkt, "id": run_.id})
        self.seg_fx = {seg: rs.merge(fxs) for seg, fxs in per_seg.items()}

    def step(self, dt: float, w: dict, out: list) -> None:
        temp = w["air_temp_c"]
        freezing = temp is not None and temp <= 1
        p = {k: r * dt / 3600 for k, r in NOISE_PER_HOUR.items()}
        if freezing:
            p["esc"] *= FREEZING_ESC_FACTOR
            p["abs"] *= FREEZING_ESC_FACTOR
        self.t += dt
        if self.t - self.traffic_t >= TRAFFIC_EVERY_S:
            caps = {seg: fx["capacity"] for seg, fx in self.seg_fx.items() if "capacity" in fx}
            self.traffic.update(self.now, self.t - self.traffic_t, caps)
            self.traffic_t = self.t
            for v in self.vehicles:
                if not v.special:
                    self.set_target(v, w)
        for v in self.vehicles:
            fx = self.seg_fx.get(v.seg)
            if v.frozen:
                v.speed = 0.0
            else:
                target = v.target * (fx["speed"] if fx and "speed" in fx and not v.wrong else 1.0)
                dv = target - v.speed
                v.speed += max(min(dv, 2.0 * dt), -3.0 * dt)
                v.pos += v.speed * dt
                seg = self.segs[v.seg]
                while v.pos >= seg.length and v.speed > 0:
                    v.pos -= seg.length
                    self.next_segment(v)
                    self.set_target(v, w)
                    seg = self.segs[v.seg]
                    fx = self.seg_fx.get(v.seg)
            if v.special:
                v.lonlat = point_at(self.segs[v.seg], v.fwd, v.pos)[:2]
            events: set[str] = set()
            scenario = bool(v.special)                               # crashed or wrong-way vehicle
            if fx and not v.special and self.t >= v.cool_until:      # scenario effects
                r = self.rng.random()
                if r < fx.get("p_slip", 0) * dt:
                    if self.dyn[v.provider]:
                        events.add("esc" if self.rng.random() < 0.6 else "abs")
                    v.speed *= 0.85
                elif r < (fx.get("p_slip", 0) + fx.get("p_brake", 0)) * dt:
                    events.add("hard_braking")
                    v.speed *= 0.5
                    if self.rng.random() < 0.3:
                        events.add("hazard_lights")
                elif r < (fx.get("p_slip", 0) + fx.get("p_brake", 0) + fx.get("p_hazard", 0)) * dt:
                    events.add("hazard_lights")
                if events:
                    v.cool_until = self.t + 60
                    scenario = True
            if not events and not v.special:                            # background noise
                r = self.rng.random()
                moving = v.speed > NOISE_MIN_SPEED
                if moving and r < p["hard_braking"]:
                    events.add("hard_braking")
                    v.speed *= 0.5
                elif moving and self.dyn[v.provider] and r < p["hard_braking"] + p["esc"]:
                    events.add("esc")
                elif moving and self.dyn[v.provider] and r < p["hard_braking"] + p["esc"] + p["abs"]:
                    events.add("abs")
                elif r < p["hard_braking"] + p["esc"] + p["abs"] + p["hazard_lights"]:
                    events.add("hazard_lights")
            if events or self.t >= v.next_report:
                if self.t >= v.next_report:
                    v.next_report += REPORT_EVERY_S
                out.append(self.signal(v, w, events, fx))
                self.out_src.append("scenario" if scenario else ("noise" if events else ""))

    def signal(self, v: Vehicle, w: dict, events: set, fx: dict | None) -> tuple:
        seg = self.segs[v.seg]
        lon, lat, hdg = point_at(seg, v.fwd, v.pos)
        dyn, env = self.dyn[v.provider], self.env[v.provider]
        fx = fx or {}
        temp = wiper = fog = None
        if env:
            base = fx.get("temp", w["air_temp_c"] if w["air_temp_c"] is not None else 5.0)
            temp = round(base + v.temp_bias + (3.0 if seg.is_tunnel else 0.0) + self.rng.gauss(0, 0.2), 1)
            mm = w["precip_mm"] or 0.0
            wiper = 0 if mm == 0 or seg.is_tunnel else 1 if mm < 0.2 else 2 if mm < 1.0 else 3
            if "wiper" in fx and not seg.is_tunnel:
                wiper = max(wiper, fx["wiper"])
            fog = (w["visibility_m"] < 200 or fx.get("fog", False)) and self.rng.random() < 0.9
        return (self.run_id, self.now, v.vid, v.provider, round(lat, 6), round(lon, 6), seg.sid, round(hdg),
                round(v.speed * 3.6), ("esc" in events) if dyn else None, ("abs" in events) if dyn else None,
                "hard_braking" in events, "hazard_lights" in events or v.hazard_on, fog, wiper, temp,
                v.airbag if dyn else None, wall_now())

    def readings(self, w: dict) -> list[tuple]:
        on_seg = defaultdict(list)
        for v in self.vehicles:
            on_seg[v.seg].append(v.speed * 3.6)
        rows = []
        wet = w["precip_last_hour_mm"] > 0
        wrong_way = [r for r in self.running.values() if r.kind == "wrong_way_driver"]
        for s in self.sensors:
            sid = int(s["segment_id"])
            fx = self.seg_fx.get(sid, {})
            speeds = on_seg.get(sid, [])
            count, avg = len(speeds), (round(sum(speeds) / len(speeds)) if speeds else None)
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
                rows.append((self.run_id, self.now, s["sensor_id"], surface, round(min(max(friction, 0), 1), 2),
                             round(visibility), None, None, None, None, wall_now(), None))
            elif s["sensor_type"] == "warning_trailer":   # a road works site: the trailer reports itself
                rows.append((self.run_id, self.now, s["sensor_id"], None, None, None, None, None, count, avg, wall_now(), True))
            else:
                cam = s["sensor_type"] == "camera"
                alarm = cam and s["location_kind"] == "motorway_entry" and \
                    any(r.camera_alarm((s["lon"], s["lat"])) for r in wrong_way)
                rows.append((self.run_id, self.now, s["sensor_id"], None, None, None,
                             alarm if cam else None, bool(fx.get("stationary")) if cam else None, count, avg, wall_now(), None))
        return rows

    def state(self, status: str, first: bool = False) -> None:
        if first:
            insert(self.c, "SIM_STATE", [(self.run_id, status, self.t0.date().isoformat(), self.t0, self.now,
                                          float(self.args.speed), self.args.seed, len(self.vehicles), wall_now(), wall_now())])
        else:
            self.c.execute('UPDATE ROAD_SAFETY.SIM_STATE SET "status" = {st}, "sim_now" = {now}, "updated_wall" = {wall} '
                           'WHERE "run_id" = {run!d}', {"st": status, "now": lit(self.now)[1:-1],
                                                      "wall": lit(wall_now())[1:-1], "run": self.run_id})


class TelemetryLog:
    """Prints every signal the simulator sends, the sensor readings, and the incidents Exasol detects.

    grey: normal telemetry; yellow: an event signal that is background noise (a single hard braking ...),
    which detection is expected to ignore; red: a signal caused by a triggered scenario, the evidence that
    should lead to an incident; red banner: an incident confirmed by the detection SQL (V_HAZARD_REACH).
    """
    GREY, YELLOW, RED, BOLD, RESET = "\033[90m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
    EVENTS = (("esc", 9, "ESC"), ("abs", 10, "ABS"), ("hard", 11, "HARD BRAKING"), ("hazard", 12, "HAZARD LIGHTS"),
              ("airbag", 16, "AIRBAG"))

    def __init__(self, sim, mode: str):
        self.sim, self.mode = sim, mode
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.lock = threading.Lock()
        self.sensors = {s["sensor_id"]: s for s in sim.sensors}

    def c(self, code, text):
        return f"{code}{text}{self.RESET}" if self.color else text

    def out(self, lines):
        with self.lock:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()

    def legend(self):
        self.out([self.c(self.BOLD, "Telemetry log: ") + self.c(self.GREY, "grey = normal signal, ")
                  + self.c(self.YELLOW, "yellow = noise (one-off event, ignored by detection), ")
                  + self.c(self.RED, "red = caused by a triggered event, ")
                  + self.c(self.RED + self.BOLD, "banner = incident detected by Exasol")])

    def signals(self, rows, srcs):
        lines = []
        for r, src in zip(rows, srcs):
            if self.mode == "events" and not src:
                continue
            seg = self.sim.segs.get(int(r[6]))
            road = (seg.ref.split(";")[0] if seg and seg.ref and seg.road_class.startswith("motorway") else
                    (seg.name or seg.ref) if seg else "") or "-"
            ev = [lbl for _, i, lbl in self.EVENTS if r[i]]
            veh = next((v for v in self.sim.vehicles if v.vid == r[2]), None) if src == "scenario" else None
            if veh is not None and veh.wrong:
                ev.append("DRIVING AGAINST THE TRAFFIC")
            env = []
            if r[15] is not None:
                env.append(f"{float(r[15]):5.1f}C")
            if r[14]:
                env.append(f"wipers {r[14]}")
            if r[13]:
                env.append("FOG LIGHTS" if src == "scenario" else "fog lights")
            line = (f"{r[1]:%H:%M:%S} veh {r[2]:>5} {r[3]:<7} {road[:26]:<26} {r[8]:>3} km/h {r[7]:>3}°  "
                    f"{r[4]:.5f},{r[5]:.5f}  {' '.join(env)}" + ("  " + ", ".join(ev) if ev else ""))
            lines.append(self.c(self.RED + self.BOLD if src == "scenario" else self.YELLOW if src == "noise" else self.GREY, line))
        if lines:
            self.out(lines)

    def readings(self, rows):
        lines = []
        for r in rows:
            s = self.sensors.get(r[2]) or next((x for x in self.sim.sensors if x["sensor_id"] == r[2]), {})
            surface, friction, vis, ww, stat, count, avg, works = r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[11]
            alarm = []
            if friction is not None and friction < 0.4 and surface is not None and surface <= 0:
                alarm.append(f"LOW FRICTION {friction:.2f} at {surface:.1f}C")
            if vis is not None and vis < 150:
                alarm.append(f"VISIBILITY {vis:.0f} m")
            if ww:
                alarm.append("WRONG-WAY VEHICLE")
            if stat:
                alarm.append("STATIONARY VEHICLE")
            if works:
                alarm.append("ROAD WORKS SET UP")
            if self.mode == "events" and not alarm:
                continue
            vals = (f"surface {surface:.1f}C friction {friction:.2f} visibility {vis:.0f} m" if surface is not None
                    else f"{count or 0} vehicles" + (f", {avg} km/h" if avg is not None else ""))
            line = f"{r[1]:%H:%M:%S} SENSOR {r[2]:<7} {str(s.get('sensor_name', ''))[:44]:<44} {vals}" + \
                   ("  " + ", ".join(alarm) if alarm else "")
            lines.append(self.c(self.RED + self.BOLD if alarm else self.GREY, line))
        if lines:
            self.out(lines)

    def incident(self, h, ended=False):
        def n(x, word):
            x = int(x or 0)
            return f"{x} {word}{'' if x == 1 else 's'}"
        bar = "=" * 100
        if ended:
            self.out([self.c(self.RED, f">>> INCIDENT ENDED: {h['srti_category']} on {h['road'] or 'unnamed road'}")])
            return
        provs = ", ".join(p for p in str(h.get("provider_list") or "").split(",") if p)
        text = [bar,
                f">>> INCIDENT DETECTED BY EXASOL: {h['srti_category'].upper()} on {h['road'] or 'unnamed road'}"
                + (f" (cause: {h['cause']})" if h.get("cause") else ""),
                f"    first signal {str(h['first_signal'])[11:19]}, confirmed {str(h['confirmed_at'])[11:19]} "
                f"({int(h['seconds_to_confirm'])} s later); evidence: {n(h['signals'], 'signal')} from "
                f"{n(h['vehicles'], 'vehicle')} of {n(h['providers'], 'provider')} ({provs or '-'})"
                + (f" and {n(h['sensors'], 'road sensor')}" if int(h['sensors']) else ""),
                f"    at {float(h['lat']):.5f},{float(h['lon']):.5f}; the warning reaches "
                f"{int(h['vehicles_reached'] or 0)} vehicles within 2 km. It is now on the map.",
                bar]
        self.out([self.c(self.RED + self.BOLD, ln) for ln in text])


class IncidentWatcher(threading.Thread):
    """Polls the detection result (ROAD_SAFETY.V_HAZARD_REACH) and logs incidents as they appear and end.
    Only the detection SQL decides; the simulator never writes a hazard."""

    def __init__(self, deployment: str, log: TelemetryLog, every_s: float = 3.0):
        super().__init__(daemon=True)
        self.c, self.log, self.every = connect(deployment), log, every_s
        self.seen: dict = {}

    def run(self):
        while True:
            try:
                rows = {r["hazard_id"]: r for r in self.c.execute(
                    'SELECT "hazard_id", "srti_category", "road", "lat", "lon", "first_signal", "confirmed_at", '
                    '"seconds_to_confirm", "vehicles", "providers", "sensors", "signals", "provider_list", "cause", '
                    '"vehicles_reached" FROM ROAD_SAFETY.V_HAZARD_REACH')}
                for k, h in rows.items():
                    if k not in self.seen:
                        self.log.incident(h)
                for k in [k for k in self.seen if k not in rows]:
                    self.log.incident(self.seen[k], ended=True)
                self.seen = rows
            except Exception as e:  # noqa: BLE001 - keep watching
                self.log.out([f"(incident watcher: {str(e).strip().splitlines()[1:2] or e})"])
            time.sleep(self.every)


def run(args) -> None:
    c = connect(args.deployment)
    c.execute("UPDATE ROAD_SAFETY.SIM_STATE SET \"status\" = 'stopped' WHERE \"status\" = 'running'")
    c.execute('UPDATE ROAD_SAFETY.SCENARIO_LOG SET "ended_at" = "started_at" WHERE "ended_at" IS NULL')
    print("Loading road network, vehicles, sensors and weather ...")
    sim = Sim(c, args)
    sim.state("running", first=True)
    print(f"SIMULATION run {sim.run_id}: {len(sim.vehicles):,} synthetic vehicles, {len(sim.sensors)} virtual sensors, "
          f"weather of {args.weather_day} (DWD {STATION}), starting {sim.now:%Y-%m-%d %H:%M}, speed x{args.speed:g}")
    print("  The vehicles are probe vehicles: the connected share of the traffic. Load, queues and speeds come from "
          "the traffic model (BASt rush-hour curve, hotspots, real roadworks).")
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    total = 0
    log = TelemetryLog(sim, args.log) if args.log else None
    if log:
        IncidentWatcher(args.deployment, log).start()
        log.legend()
    try:
        while not stop["flag"]:
            t_wall = time.monotonic()
            sim.refresh_scenarios()
            sim.speed = float(c.execute('SELECT "speed" FROM ROAD_SAFETY.SIM_STATE WHERE "run_id" = {r!d}',
                                        {"r": sim.run_id}).fetchval() or sim.speed)
            sim_dt = args.tick * sim.speed
            out: list[tuple] = []
            sim.out_src = []
            steps = max(1, math.ceil(sim_dt))
            for _ in range(steps):
                w = sim.weather.at(sim.now)
                sim.step(sim_dt / steps, w, out)
            readings = []
            if sim.t >= sim.next_sensor:
                readings = sim.readings(w)
                sim.next_sensor += SENSOR_EVERY_S
            insert(c, "VEHICLE_SIGNALS", out)
            if readings:
                insert(c, "SENSOR_READINGS", readings)
            sim.state("running")
            total += len(out)
            if log:
                log.signals(out, sim.out_src)
                log.readings(readings)
            if not log and (args.verbose or int(sim.t) % 60 < sim_dt):
                active = ", ".join(f"{r.kind}" for r in sim.running.values()) or "none"
                print(f"  {sim.now:%H:%M:%S}  x{sim.speed:g}  {len(out):5d} signals this tick, {total:,} total, "
                      f"air {w['air_temp_c']:.1f} C, traffic: {sim.traffic.summary()}, scenarios: {active}", flush=True)
            if args.duration and sim.t >= args.duration:
                break
            time.sleep(max(0.0, args.tick - (time.monotonic() - t_wall)))
    except KeyboardInterrupt:
        pass
    finally:
        sim.state("stopped")
        print(f"Stopped at {sim.now:%H:%M:%S}, {total:,} signals written")


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


def trigger(args) -> None:
    c = connect(args.deployment)
    info = rs.SCENARIOS[args.scenario]
    run_ = current_run(c) if not args.dry_run else {"run_id": 0, "sim_now": None}
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
                "  The vehicles and sensors behave normally again. Hazards expire when no new signals confirm them."])
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
        print(f'would inject "{info["label"]}" on {area.description} ({area.name}, {len(area.segs)} road segments)')
        for n in notes:
            print(f"  note: {n}")
        return
    if c.execute('SELECT COUNT(*) FROM ROAD_SAFETY.SCENARIO_LOG WHERE "run_id" = {r!d} AND "scenario_type" = {t} '
                 'AND "area" = {a} AND "ended_at" IS NULL',
                 {"r": run_["run_id"], "t": args.scenario, "a": area.name}).fetchval():
        sys.exit(f"{args.scenario} on {area.description} is already running")
    sid = int(c.execute('SELECT COALESCE(MAX("scenario_id"), 0) + 1 FROM ROAD_SAFETY.SCENARIO_LOG').fetchval())
    insert(c, "SCENARIO_LOG", [(sid, run_["run_id"], args.scenario, area.name, area.wkt, sim_now, None, wall_now(),
                                args.by, info["injects"])])
    banner([f'SIMULATION: injecting "{info["label"]}" on {area.description}, {sim_now:%H:%M:%S}',
            *[f"  note: {n}" for n in notes],
            f"  scenario {sid} in run {run_['run_id']} (simulation time {sim_now:%Y-%m-%d %H:%M:%S}), "
            f"{len(area.segs)} road segments" if area.segs else f"  scenario {sid} in run {run_['run_id']}",
            f"  injects: {info['injects']}",
            "  writes synthetic signals into ROAD_SAFETY only. No hazard is written: detection is up to the SQL.",
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
            "  The vehicles and sensors behave normally again. Hazards expire when no new signals confirm them."])


def scenarios(args) -> None:
    for k, v in rs.SCENARIOS.items():
        print(f"{k:18s} {v['label']}\n{'':18s} where: {v['areas']}\n"
              f"{'':18s} injects: {v['injects']}\n")


HAZARD_DEFAULTS = {"window_min": 15, "min_vehicles": 3, "min_providers": 2, "friction_max": 0.4,
                   "blockage_after_s": 600, "clear_vehicles": 5}
REACH_DEFAULTS = {"reach_m": 2000, "toward_deg": 60}
TRAFFIC_DEFAULTS = {"now_min": 5, "before_min": 10, "slow": 0.7, "congested": 0.4, "standstill": 0.15}
CONGESTION_DEFAULTS = {"merge_m": 1500}


def render(c, name: str, **params) -> str:
    """Fill a roadsafety/*.sql template with pyexasol placeholders."""
    return c.format.format((ROOT / "roadsafety" / f"{name}.sql").read_text(), **params).strip().rstrip(";")


def views(args) -> None:
    c = connect(args.deployment)
    hazards = render(c, "hazards", **HAZARD_DEFAULTS)
    c.execute(f"CREATE OR REPLACE VIEW ROAD_SAFETY.V_HAZARDS_LIVE AS\n{hazards}")
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_dashboard_queries import compose_after_hazards
    evidence = compose_after_hazards((ROOT / "roadsafety" / "hazards.sql").read_text(),
                                     (ROOT / "roadsafety" / "evidence.sql").read_text())
    c.execute("CREATE OR REPLACE VIEW ROAD_SAFETY.V_HAZARD_EVIDENCE AS\n"
              + c.format.format(evidence, **HAZARD_DEFAULTS).strip().rstrip(";"))
    reach = render(c, "reach", hazards_sql="SELECT * FROM ROAD_SAFETY.V_HAZARDS_LIVE", **REACH_DEFAULTS)
    c.execute(f"CREATE OR REPLACE VIEW ROAD_SAFETY.V_HAZARD_REACH AS\n{reach}")
    traffic = render(c, "traffic", **TRAFFIC_DEFAULTS)
    c.execute(f"CREATE OR REPLACE VIEW ROAD_SAFETY.V_TRAFFIC_STATE AS\n{traffic}")
    congestion = render(c, "congestion", traffic_sql="SELECT * FROM ROAD_SAFETY.V_TRAFFIC_STATE", **CONGESTION_DEFAULTS)
    c.execute(f"CREATE OR REPLACE VIEW ROAD_SAFETY.V_CONGESTION AS\n{congestion}")
    warnings = render(c, "warnings", traffic_sql="SELECT * FROM ROAD_SAFETY.V_TRAFFIC_STATE")
    c.execute(f"CREATE OR REPLACE VIEW ROAD_SAFETY.V_CONGESTION_WARNINGS AS\n{warnings}")
    udf = (ROOT / "roadsafety" / "diversions_udf.sql").read_text()
    c.execute(udf[udf.index("CREATE OR REPLACE"):].rstrip().rstrip("/").rstrip())
    diversions = render(c, "diversions", traffic_sql="SELECT * FROM ROAD_SAFETY.V_TRAFFIC_STATE")
    c.execute(f"CREATE OR REPLACE VIEW ROAD_SAFETY.V_DIVERSIONS AS\n{diversions}")
    c.execute("GRANT EXECUTE ON SCHEMA ROAD_SAFETY TO dash_readonly")
    print("UDF ROAD_SAFETY.DIVERSIONS and view V_DIVERSIONS created (EXECUTE granted to dash_readonly)")
    print("Views ROAD_SAFETY.V_HAZARDS_LIVE, V_HAZARD_EVIDENCE, V_HAZARD_REACH, V_TRAFFIC_STATE, V_CONGESTION, V_CONGESTION_WARNINGS, V_DIVERSIONS "
          "created with the default thresholds:")
    print("  " + ", ".join(f"{k}={v}" for k, v in {**HAZARD_DEFAULTS, **REACH_DEFAULTS, **TRAFFIC_DEFAULTS,
                                                   **CONGESTION_DEFAULTS}.items()))


def status(args) -> None:
    c = connect(args.deployment)
    for r in c.execute('SELECT * FROM ROAD_SAFETY.SIM_STATE ORDER BY "run_id" DESC LIMIT 3'):
        print({k: str(v) for k, v in r.items()})
    for t in ("VEHICLE_SIGNALS", "SENSOR_READINGS", "SCENARIO_LOG"):
        print(f"  {t}: {c.execute(f'SELECT COUNT(*) FROM ROAD_SAFETY.{t}').fetchval():,} rows")


def reset(args) -> None:
    if not args.yes:
        sys.exit("reset empties VEHICLE_SIGNALS, SENSOR_READINGS, SCENARIO_LOG and SIM_STATE; add --yes")
    c = connect(args.deployment)
    for t in ("VEHICLE_SIGNALS", "SENSOR_READINGS", "SCENARIO_LOG", "SIM_STATE"):
        c.execute(f"TRUNCATE TABLE ROAD_SAFETY.{t}")
    print("ROAD_SAFETY stream tables emptied (providers, vehicles and sensors kept)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--deployment", default="road-safety-demo", help="Exasol Personal deployment name")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup", help="create ROAD_SAFETY with providers, vehicles and sensors")
    p.add_argument("--vehicles", type=int, default=6000)
    p.add_argument("--seed", type=int, default=42)
    p = sub.add_parser("run", help="run the background stream")
    p.add_argument("--speed", type=float, default=1.0, help="simulation seconds per wall second")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--weather-day", default="2026-10-27", help="the simulated day: DWD weather and roadworks of it")
    p.add_argument("--start", default="06:00", help="local start time of the simulation")
    p.add_argument("--tick", type=float, default=2.0, help="wall seconds per batch")
    p.add_argument("--duration", type=float, default=0, help="stop after this many simulation seconds")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--log", nargs="?", const="all", choices=["all", "events"],
                   help="print every signal sent (all) or only event signals and sensor alerts (events), "
                        "and every incident Exasol detects")
    sub.add_parser("views", help="create V_HAZARDS_LIVE and V_HAZARD_REACH from roadsafety/*.sql")
    sub.add_parser("status", help="show the current run and row counts")
    p = sub.add_parser("trigger", help="inject a scenario into the running simulation (visibly)")
    p.add_argument("scenario", type=lambda k: rs.ALIASES.get(k.lower().replace("-", "_"), k.lower().replace("-", "_")),
                   choices=list(rs.SCENARIOS))
    p.add_argument("--area", help="a named area of the first version (see `scenarios`)")
    p.add_argument("--road", help='road number or street name, e.g. "A9", "B2R", "Leopoldstraße"')
    p.add_argument("--near", help='a place: suburb, village, motorway junction, station, landmark or street, e.g. "Garching-Süd"')
    p.add_argument("--direction", help='north/south/east/west, in/out (of the city) or "toward <place>"')
    p.add_argument("--at", help='coordinates "lat,lon"')
    p.add_argument("--dry-run", action="store_true", help="only show where it would go")
    p.add_argument("--stop", action="store_true", help="end the active scenario of this type")
    p.add_argument("--by", default="presenter", help="who triggered it (written to SCENARIO_LOG)")
    sub.add_parser("stop-all", help="end all active scenarios")
    p = sub.add_parser("speed", help="change the simulation speed while it runs")
    p.add_argument("factor", type=float, help="simulated seconds per real second, e.g. 20 for the build-up, 1 for incidents")
    sub.add_parser("scenarios", help="list the scenarios that can be triggered")
    p = sub.add_parser("reset", help="empty the stream tables")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"setup": setup, "run": run, "views": views, "status": status, "reset": reset,
     "trigger": trigger, "stop-all": stop_all, "scenarios": scenarios, "speed": set_speed}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
