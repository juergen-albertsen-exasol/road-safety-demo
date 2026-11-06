#!/usr/bin/env python3
"""Simulate connected vehicles and road sensors in Munich for the road safety demo (schema ROAD_SAFETY).

Everything this script writes is SYNTHETIC. The road network, weather and accident history it builds
on are real open data in ROAD_DATA (scripts/prepare_road_data.py). Plan: docs/road-safety-demo-plan.md.

    .venv/bin/python scripts/simulate_road_safety.py setup            # schema, providers, vehicles, sensors
    .venv/bin/python scripts/simulate_road_safety.py run              # background stream, Ctrl-C to stop
    .venv/bin/python scripts/simulate_road_safety.py status
    .venv/bin/python scripts/simulate_road_safety.py reset --yes      # empty the stream tables

`run` options: --speed (simulation seconds per wall second, default 1), --seed, --weather-day
(default 2025-01-10), --start (local time, default 06:30), --tick (wall seconds per batch, default 2).

Background traffic only for now: vehicles drive along the OSM segments, report every 10 s of
simulation time, and produce some isolated noise on purpose (single hard brakings, single ESC
interventions, hazard lights) so the detection rule has something to filter out. Weather follows the
real DWD 10-minute values of the weather day at station 3379 Muenchen-Stadt. Scenario triggers come
in a later step: they will write SCENARIO_LOG and change the signals, never the hazards.

Connects as `sys` to the Exasol Personal deployment (--deployment, default road-safety-demo); the
password is read from the deployment's secrets.json and never printed. Writes only to ROAD_SAFETY.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import signal
import ssl
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyexasol

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
    "secondary": 3.5, "secondary_link": 1, "tertiary": 2, "tertiary_link": 1, "unclassified": 0.8,
    "residential": 0.5, "living_street": 0.1,
}
DEFAULT_KMH = {"motorway": 130, "motorway_link": 80, "trunk": 80, "trunk_link": 60, "living_street": 7,
               "residential": 30}

# Background noise, events per vehicle and hour of simulation time.
NOISE_PER_HOUR = {"hard_braking": 0.5, "esc": 0.05, "abs": 0.03, "hazard_lights": 0.05}
FREEZING_ESC_FACTOR = 3.0      # ESC/ABS a bit more often when the outside temperature is <= 1 C
REPORT_EVERY_S = 10
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


def parse_linestring(wkt: str) -> list[tuple[float, float]]:
    body = wkt[wkt.index("(") + 1: wkt.rindex(")")]
    return [tuple(map(float, p.split())) for p in body.split(",")]


KX = 111320.0 * math.cos(math.radians(48.137))
KY = 110540.0


def load_graph(c):
    segs: dict[int, Segment] = {}
    for r in c.execute('SELECT "segment_id", "from_node", "to_node", "oneway", "road_class", "name", "ref", '
                       '"maxspeed_kmh", "is_tunnel", "geometry_wkt" FROM ROAD_DATA.ROAD_SEGMENTS'):
        coords = parse_linestring(r["geometry_wkt"])
        cum = [0.0]
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            cum.append(cum[-1] + math.hypot((x2 - x1) * KX, (y2 - y1) * KY))
        rc = r["road_class"]
        kmh = float(r["maxspeed_kmh"]) if r["maxspeed_kmh"] is not None else DEFAULT_KMH.get(rc, 50)
        sid = int(r["segment_id"])
        segs[sid] = Segment(sid, int(r["from_node"]), int(r["to_node"]), int(r["oneway"]), rc,
                            r["name"] or "", r["ref"] or "", kmh, bool(r["is_tunnel"]), coords, cum, max(cum[-1], 1.0))
    # node -> segments you can enter from it, with direction (True = from -> to)
    exits: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    for s in segs.values():
        if s.oneway in (0, 1):
            exits[s.from_node].append((s.sid, True))
        if s.oneway in (0, -1):
            exits[s.to_node].append((s.sid, False))
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
    weights = [segs[i].length * CLASS_WEIGHT.get(segs[i].road_class, 0.5) for i in ids]
    fleet = [p for p in PROVIDERS if p[5]]
    vehicles = []
    for vid in range(1, args.vehicles + 1):
        prov = rng.choices([p[0] for p in fleet], [p[5] for p in fleet])[0]
        vtype = rng.choices([t for t, _ in VEHICLE_TYPES], [w for _, w in VEHICLE_TYPES])[0]
        vehicles.append((vid, prov, vtype, rng.choices(ids, weights)[0]))
    insert(c, "VEHICLES", vehicles)
    print(f"  {len(vehicles):,} vehicles from {len(fleet)} providers")

    rows = list(c.execute('SELECT "segment_id", "mid_lon", "mid_lat", "road_class", "name", "ref", "bridge_name", '
                          '"crosses_isar", "is_tunnel", "length_m", "to_node", "oneway" FROM ROAD_DATA.ROAD_SEGMENTS'))
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
    # wrong-way cameras at motorway entries: links whose end joins a motorway
    motorway_from = {int(s.from_node) for s in segs.values() if s.road_class == "motorway"}
    entries = [(r["segment_id"], r["mid_lon"], r["mid_lat"], r["ref"] or r["name"]) for r in rows
               if r["road_class"] == "motorway_link" and r["oneway"] == 1 and int(r["to_node"]) in motorway_from]
    for k, (sid, lon, lat, label) in enumerate(pick_spread(entries, 1500, 25), 1):
        sensors.append((f"WW-{k:02d}", "camera", "motorway_entry", f"Wrong-way camera, entry {label or sid}".strip(),
                        sid, "ROAD_OP", lat, lon))
    # loop detectors on the motorway main carriageways
    main = [(r["segment_id"], r["mid_lon"], r["mid_lat"], r["ref"]) for r in rows if r["road_class"] == "motorway" and r["ref"]]
    for k, (sid, lon, lat, ref) in enumerate(pick_spread(main, 3000, 10), 1):
        sensors.append((f"LD-{k:02d}", "loop_detector", "motorway", f"Loop detector {ref}", sid, "ROAD_OP", lat, lon))
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
    events: dict = field(default_factory=dict)


class Sim:
    def __init__(self, c, args):
        self.c, self.args = c, args
        self.rng = random.Random(args.seed)
        self.segs, self.exits = load_graph(c)
        self.ids = list(self.segs)
        self.weights = [self.segs[i].length * CLASS_WEIGHT.get(self.segs[i].road_class, 0.5) for i in self.ids]
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

    @property
    def now(self) -> datetime:
        return self.t0 + timedelta(seconds=self.t)

    def set_target(self, v: Vehicle, w: dict | None = None) -> None:
        s = self.segs[v.seg]
        kmh = s.kmh
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

    def next_segment(self, v: Vehicle) -> None:
        s = self.segs[v.seg]
        node = s.to_node if v.fwd else s.from_node
        cands = [(sid, fwd) for sid, fwd in self.exits.get(node, ()) if sid != v.seg]
        if not cands:
            if s.oneway == 0:                        # dead end: turn around
                v.fwd, v.pos = not v.fwd, 0.0
                return
            seg = self.segs[self.rng.choices(self.ids, self.weights)[0]]   # left the network: respawn
            v.seg, v.fwd, v.pos = seg.sid, seg.oneway != -1, 0.0
            return
        wts = []
        for sid, _ in cands:
            n = self.segs[sid]
            wt = CLASS_WEIGHT.get(n.road_class, 0.5)
            if (s.name and n.name == s.name) or (s.ref and n.ref == s.ref):
                wt *= 4                              # keep following the same road
            wts.append(wt)
        v.seg, v.fwd = self.rng.choices(cands, wts)[0]
        v.pos = 0.0

    def step(self, dt: float, w: dict, out: list) -> None:
        temp = w["air_temp_c"]
        freezing = temp is not None and temp <= 1
        p = {k: r * dt / 3600 for k, r in NOISE_PER_HOUR.items()}
        if freezing:
            p["esc"] *= FREEZING_ESC_FACTOR
            p["abs"] *= FREEZING_ESC_FACTOR
        self.t += dt
        for v in self.vehicles:
            dv = v.target - v.speed
            v.speed += max(min(dv, 2.0 * dt), -3.0 * dt)
            v.pos += v.speed * dt
            seg = self.segs[v.seg]
            while v.pos >= seg.length:
                v.pos -= seg.length
                self.next_segment(v)
                self.set_target(v, w)
                seg = self.segs[v.seg]
            event = None
            r = self.rng.random()
            if r < p["hard_braking"]:
                event = "hard_braking"
                v.speed *= 0.5
            elif self.dyn[v.provider] and r < p["hard_braking"] + p["esc"]:
                event = "esc"
            elif self.dyn[v.provider] and r < p["hard_braking"] + p["esc"] + p["abs"]:
                event = "abs"
            elif r < p["hard_braking"] + p["esc"] + p["abs"] + p["hazard_lights"]:
                event = "hazard_lights"
            if event or self.t >= v.next_report:
                if not event:
                    v.next_report += REPORT_EVERY_S
                out.append(self.signal(v, w, event))

    def signal(self, v: Vehicle, w: dict, event: str | None) -> tuple:
        seg = self.segs[v.seg]
        lon, lat, hdg = point_at(seg, v.fwd, v.pos)
        dyn, env = self.dyn[v.provider], self.env[v.provider]
        temp = None
        wiper = fog = None
        if env:
            base = w["air_temp_c"] if w["air_temp_c"] is not None else 5.0
            temp = round(base + v.temp_bias + (3.0 if seg.is_tunnel else 0.0) + self.rng.gauss(0, 0.2), 1)
            mm = w["precip_mm"] or 0.0
            wiper = 0 if mm == 0 or seg.is_tunnel else 1 if mm < 0.2 else 2 if mm < 1.0 else 3
            fog = w["visibility_m"] < 200 and self.rng.random() < 0.9
        return (self.run_id, self.now, v.vid, v.provider, round(lat, 6), round(lon, 6), seg.sid, round(hdg),
                round(v.speed * 3.6), (event == "esc") if dyn else None, (event == "abs") if dyn else None,
                event == "hard_braking", event == "hazard_lights", fog, wiper, temp,
                False if dyn else None, wall_now())

    def readings(self, w: dict) -> list[tuple]:
        on_seg = defaultdict(list)
        for v in self.vehicles:
            on_seg[v.seg].append(v.speed * 3.6)
        rows = []
        wet = w["precip_last_hour_mm"] > 0
        for s in self.sensors:
            sid = int(s["segment_id"])
            speeds = on_seg.get(sid, [])
            count, avg = len(speeds), (round(sum(speeds) / len(speeds)) if speeds else None)
            if s["sensor_type"] == "road_weather":
                base = w["temp_5cm_c"] if w["temp_5cm_c"] is not None else (w["air_temp_c"] or 5.0)
                surface = round(base - 0.7 + self.rng.gauss(0, 0.2), 1)      # bridge decks run colder
                friction = round(min(max((0.6 if wet else 0.8) + self.rng.gauss(0, 0.02), 0), 1), 2)
                rows.append((self.run_id, self.now, s["sensor_id"], surface, friction,
                             round(min(w["visibility_m"], 2000)), None, None, None, None, wall_now()))
            else:
                cam = s["sensor_type"] == "camera"
                rows.append((self.run_id, self.now, s["sensor_id"], None, None, None,
                             False if cam else None, False if cam else None, count, avg, wall_now()))
        return rows

    def state(self, status: str, first: bool = False) -> None:
        if first:
            insert(self.c, "SIM_STATE", [(self.run_id, status, self.t0.date().isoformat(), self.t0, self.now,
                                          float(self.args.speed), self.args.seed, len(self.vehicles), wall_now(), wall_now())])
        else:
            self.c.execute('UPDATE ROAD_SAFETY.SIM_STATE SET "status" = {st}, "sim_now" = {now}, "updated_wall" = {wall} '
                           'WHERE "run_id" = {run!d}', {"st": status, "now": lit(self.now)[1:-1],
                                                      "wall": lit(wall_now())[1:-1], "run": self.run_id})


def run(args) -> None:
    c = connect(args.deployment)
    c.execute("UPDATE ROAD_SAFETY.SIM_STATE SET \"status\" = 'stopped' WHERE \"status\" = 'running'")
    print("Loading road network, vehicles, sensors and weather ...")
    sim = Sim(c, args)
    sim.state("running", first=True)
    print(f"SIMULATION run {sim.run_id}: {len(sim.vehicles):,} synthetic vehicles, {len(sim.sensors)} virtual sensors, "
          f"weather of {args.weather_day} (DWD {STATION}), starting {sim.now:%Y-%m-%d %H:%M}, speed x{args.speed:g}")
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    total = 0
    try:
        while not stop["flag"]:
            t_wall = time.monotonic()
            sim_dt = args.tick * args.speed
            out: list[tuple] = []
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
            if args.verbose or int(sim.t) % 60 < sim_dt:
                print(f"  {sim.now:%H:%M:%S}  {len(out):5d} signals this tick, {total:,} total, "
                      f"air {w['air_temp_c']} C, 5 cm {w['temp_5cm_c']} C", flush=True)
            if args.duration and sim.t >= args.duration:
                break
            time.sleep(max(0.0, args.tick - (time.monotonic() - t_wall)))
    except KeyboardInterrupt:
        pass
    finally:
        sim.state("stopped")
        print(f"Stopped at {sim.now:%H:%M:%S}, {total:,} signals written")


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
    p.add_argument("--vehicles", type=int, default=3000)
    p.add_argument("--seed", type=int, default=42)
    p = sub.add_parser("run", help="run the background stream")
    p.add_argument("--speed", type=float, default=1.0, help="simulation seconds per wall second")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--weather-day", default="2025-01-10")
    p.add_argument("--start", default="06:30", help="local start time of the simulation")
    p.add_argument("--tick", type=float, default=2.0, help="wall seconds per batch")
    p.add_argument("--duration", type=float, default=0, help="stop after this many simulation seconds")
    p.add_argument("--verbose", action="store_true")
    sub.add_parser("status", help="show the current run and row counts")
    p = sub.add_parser("reset", help="empty the stream tables")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"setup": setup, "run": run, "status": status, "reset": reset}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
