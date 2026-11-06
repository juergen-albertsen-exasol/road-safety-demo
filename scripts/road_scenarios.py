"""Scenarios for the road safety simulator: where they happen and what they change.

A scenario never writes a hazard. It only changes how the synthetic vehicles and sensors in its
area behave (ESC interventions, braking, fog lights, low friction readings ...). Whether a hazard
appears is decided by the detection SQL in roadsafety/hazards.sql alone.

Used by scripts/simulate_road_safety.py: `trigger` resolves the area and writes SCENARIO_LOG; the
running simulator reads SCENARIO_LOG every tick and applies the effects below.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import shapely
from shapely import wkt as shp_wkt
from shapely.geometry import LineString, Point, box

KX = 111320.0 * math.cos(math.radians(48.137))
KY = 110540.0

# type -> label (the SRTI wording), default area, what is injected (for the banner)
SCENARIOS = {
    "slippery_road": {
        "label": "temporary slippery road", "default_area": "isar-bridges",
        "injects": "vehicles crossing get ESC/ABS interventions and slow down, their outside temperature "
                   "reads about -1 C; the road weather sensors report low friction and a surface below 0 C",
        "areas": "isar-bridges, or one bridge by name (e.g. reichenbachbruecke)",
    },
    "accident": {
        "label": "accident", "default_area": "a99-nord",
        "injects": "2 vehicles stop with airbag deployed and hazard lights on; the accident closes 2 of 3 lanes, so a "
                   "queue builds behind it; traffic arriving at the queue brakes hard and switches on hazard lights",
        "areas": "a99-nord (A99 at Kreuz München-Nord), landshuter-allee (Mittlerer Ring), or a street name",
    },
    "oil_spill": {
        "label": "temporary slippery road (oil spill)", "default_area": "a9-freimann",
        "injects": "on about 300 m, vehicles get ESC/ABS interventions and slow down; the outside temperature stays "
                   "normal, so it is not ice; nothing else changes",
        "areas": "a9-freimann (A9 into the city)",
    },
    "wrong_way_driver": {
        "label": "wrong-way driver", "default_area": "a9-freimann",
        "injects": "1 vehicle drives against the direction of travel on the A9; oncoming traffic brakes and "
                   "switches on hazard lights; wrong-way cameras within 800 m detect it",
        "areas": "a9-freimann",
    },
    "fog": {
        "label": "reduced visibility (fog)", "default_area": "north",
        "injects": "vehicles in the area switch on fog lights and slow down; road weather sensors there "
                   "report visibility below 150 m",
        "areas": "north (Freimann to the A99)",
    },
    "storm": {
        "label": "exceptional weather (storm)", "default_area": "west-east",
        "injects": "a storm band about 3 km wide moves across the city from west to east in 20 minutes: "
                   "wipers at maximum, slower driving",
        "areas": "west-east",
    },
    "animal": {
        "label": "animal on the road", "default_area": "forstenrieder-park",
        "injects": "vehicles passing one spot at the forest edge brake hard",
        "areas": "forstenrieder-park",
    },
    "debris": {
        "label": "debris on the road", "default_area": "a96",
        "injects": "vehicles passing one spot on the A96 swerve and brake hard, some switch on hazard lights",
        "areas": "a96",
    },
}

STORM_SECONDS = 1200
FOG_BOX = (11.45, 48.19, 11.75, 48.26)     # lon_min, lat_min, lon_max, lat_max
FREIMANN = (11.6125, 48.2040)
A99_NORD = (11.6500, 48.2170)       # A99 east of Kreuz München-Nord, toward Aschheim
A9_OIL = (11.6120, 48.1950)         # A9 into the city at Freimann
A96_SPOT = (11.4700, 48.1240)


def dist_m(a, b) -> float:
    return math.hypot((a[0] - b[0]) * KX, (a[1] - b[1]) * KY)


def norm(s: str) -> str:
    return (s or "").lower().replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss") \
        .replace(" ", "").replace("-", "")


@dataclass
class Area:
    name: str
    description: str            # for the banner, e.g. "13 Isar bridges"
    segs: set = field(default_factory=set)
    polygon: object = None      # shapely, lon/lat: the dashed outline on the map
    point: tuple | None = None
    start_seg: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def wkt(self) -> str | None:
        return shapely.to_wkt(self.polygon, rounding_precision=6, trim=True) if self.polygon is not None else None


def outline(segs, graph) -> object:
    lines = [LineString(graph[s].coords) for s in segs]
    return shapely.union_all(lines).buffer(0.0005).simplify(0.0001) if lines else None


def nearest(graph, point, pred):
    best, bd = None, float("inf")
    for s in graph.values():
        if pred(s):
            d = dist_m(s.mid, point)
            if d < bd:
                best, bd = s, d
    return best


def resolve(kind: str, area: str | None, graph, c=None) -> Area:
    """Turn a scenario type and area name into road segments and an outline."""
    area = area or SCENARIOS[kind]["default_area"]
    a = norm(area)
    if kind == "slippery_road":
        if a in ("isarbridges", "isar"):
            segs = {s.sid for s in graph.values() if s.crosses_isar}
        else:
            segs = {s.sid for s in graph.values() if s.crosses_isar and a in norm(s.bridge_name or s.name)}
        if not segs:
            raise ValueError(f"no Isar bridge matches '{area}'")
        names = {graph[s].bridge_name or graph[s].name or graph[s].ref for s in segs}
        desc = f"{len(names)} Isar bridges" if len(names) > 1 else f"the {names.pop()}"
        return Area(area, desc, segs, outline(segs, graph))
    if kind == "accident":
        if a == "a99nord":
            crash = nearest(graph, A99_NORD, lambda s: s.road_class == "motorway" and s.oneway == 1 and s.ref == "A 99"
                            and 60 <= s.heading <= 160 and s.length >= 80)
            if crash is None:
                raise ValueError("no A99 segment found east of Kreuz München-Nord")
            label = "the A99 at Kreuz München-Nord (toward Aschheim)"
        else:
            cands = [s for s in graph.values() if norm(s.name) == a and s.oneway == 1 and not s.is_tunnel
                     and s.road_class in ("trunk", "primary") and s.length >= 80]
            if not cands:
                raise ValueError(f"no one-way main road segment named '{area}'")
            crash = max(cands, key=lambda s: (s.length, -s.sid))
            label = f"{crash.name} (Mittlerer Ring)" if a == "landshuterallee" else crash.name
        upstream, frontier = set(), {crash.from_node}
        for _ in range(3):                         # up to 3 segments back, along the same road
            nxt = set()
            for node in frontier:
                for sid in graph.into.get(node, ()):
                    s = graph[sid]
                    if sid != crash.sid and s.oneway == 1 and (s.name == crash.name or s.road_class == crash.road_class):
                        upstream.add(sid)
                        nxt.add(s.from_node)
            frontier = nxt
        segs = {crash.sid} | upstream
        return Area(area, label, segs, outline({crash.sid}, graph), crash.mid, crash.sid, {"upstream": upstream})
    if kind == "oil_spill":
        start = nearest(graph, A9_OIL, lambda s: s.road_class == "motorway" and s.oneway == 1 and s.ref == "A 9"
                        and 150 <= s.heading <= 250)
        if start is None:
            raise ValueError("no A9 segment found at Freimann")
        segs, cur, length = {start.sid}, start, start.length
        while length < 300:                          # about 300 m in the direction of travel
            nxt = [sid for sid, fwd in graph.exits.get(cur.to_node, ()) if fwd and graph[sid].ref == "A 9"
                   and graph[sid].road_class == "motorway"]
            if not nxt:
                break
            cur = graph[nxt[0]]
            segs.add(cur.sid)
            length += cur.length
        return Area(area, "the A9 into the city at Freimann", segs, outline(segs, graph), start.mid, start.sid)
    if kind == "wrong_way_driver":
        start = nearest(graph, FREIMANN, lambda s: s.road_class == "motorway" and s.oneway == 1 and s.ref == "A 9")
        if start is None:
            raise ValueError("no A9 segment found")
        return Area(area, "the A9 at Freimann", {start.sid}, outline({start.sid}, graph), start.mid, start.sid)
    if kind == "fog":
        poly = box(*FOG_BOX)
        segs = {s.sid for s in graph.values() if FOG_BOX[0] <= s.mid[0] <= FOG_BOX[2] and FOG_BOX[1] <= s.mid[1] <= FOG_BOX[3]}
        return Area(area, "the north of the city (Freimann to the A99)", segs, poly)
    if kind == "storm":
        return Area(area, "a band moving from west to east across the city", set(), storm_band(0))
    if kind == "animal":
        forest = None
        if c is not None:
            row = c.execute("SELECT \"geometry_wkt\" FROM ROAD_DATA.ROAD_AREAS WHERE \"name\" = 'Forstenrieder Park'").fetchone()
            forest = shp_wkt.loads(row["geometry_wkt"]) if row else None
        if forest is None:
            raise ValueError("Forstenrieder Park not found in ROAD_DATA.ROAD_AREAS")
        edge = forest.buffer(0.0006)
        shapely.prepare(edge)
        rank = {"motorway": 5, "trunk": 4, "primary": 3, "secondary": 2, "tertiary": 1}
        cands = [s for s in graph.values() if s.road_class in rank and shapely.intersects(edge, LineString(s.coords))]
        if not cands:
            raise ValueError("no main road along Forstenrieder Park")
        spot = max(cands, key=lambda s: (rank[s.road_class], s.length, -s.sid))
        return point_area(area, f"the edge of Forstenrieder Park ({spot.name or spot.ref})", spot, graph)
    if kind == "debris":
        spot = nearest(graph, A96_SPOT, lambda s: s.road_class == "motorway" and s.ref == "A 96" and s.oneway == 1)
        if spot is None:
            raise ValueError("no A96 segment found")
        return point_area(area, "the A96", spot, graph, same_direction=True)
    raise ValueError(f"unknown scenario '{kind}'")


def point_area(name, desc, spot, graph, same_direction=False) -> Area:
    """Segments of the same road within 150 m of one spot."""
    def same_road(s):
        return (spot.name and s.name == spot.name) or (spot.ref and s.ref == spot.ref)
    segs = set()
    for s in graph.values():
        if same_road(s) and dist_m(s.mid, spot.mid) <= 150:
            if same_direction and abs((s.heading - spot.heading + 180) % 360 - 180) > 45:
                continue
            segs.add(s.sid)
    segs.add(spot.sid)
    return Area(name, desc, segs, Point(spot.mid).buffer(0.0012), spot.mid, spot.sid)


def storm_band(elapsed_s: float):
    lon = 11.36 + (11.84 - 11.36) * min(1.0, max(0.0, elapsed_s) / STORM_SECONDS)
    return box(lon - 0.02, 48.03, lon + 0.02, 48.25)


# --------------------------------------------------------------------------- running scenarios
class Running:
    """A scenario applied by the running simulator."""

    def __init__(self, row: dict, sim):
        self.id = int(row["scenario_id"])
        self.kind = row["scenario_type"]
        self.started_t = (row["started_at"] - sim.t0).total_seconds()
        self.area = resolve(self.kind, row["area"], sim.graph, sim.c)
        self.special: list = []                     # vehicles taken over by the scenario
        self.rng = random.Random(self.id * 7919)
        if self.kind == "accident":
            crash = sim.graph[self.area.start_seg]
            cands = [v for v in sim.vehicles if sim.dyn[v.provider] and not v.special]
            first = self.rng.choice(cands)
            second = self.rng.choice([v for v in cands if v.provider != first.provider])
            for k, v in enumerate((first, second)):
                v.special, v.frozen, v.airbag, v.hazard_on = self.id, True, True, True
                v.seg, v.fwd, v.pos, v.speed, v.target = crash.sid, True, crash.length * 0.5 + 8 * k, 0.0, 0.0
                v.next_report = sim.t                # report at once
                self.special.append(v)
        if self.kind == "wrong_way_driver":
            start = sim.graph[self.area.start_seg]
            v = self.rng.choice([v for v in sim.vehicles if sim.dyn[v.provider] and v.vtype == "car" and not v.special])
            v.special, v.wrong = self.id, True
            v.seg, v.fwd, v.pos = start.sid, False, 0.0      # backwards on a one-way carriageway
            v.speed = v.target = 100 / 3.6
            v.next_report = sim.t
            self.special.append(v)

    def stop(self, sim) -> None:
        for v in self.special:
            v.special = 0
            v.frozen = v.wrong = v.airbag = v.hazard_on = False
            sim.respawn(v)

    def effects(self, sim) -> dict:
        """segment id -> effect for this tick."""
        k = self.kind
        if k == "slippery_road":
            fx = {"speed": 0.7, "p_slip": 0.1, "temp": -1.0, "friction": (0.15, 0.3), "surface_max": -1.0}
            return {s: fx for s in self.area.segs}
        if k == "accident":
            crash = self.area.start_seg
            # the queue behind the accident (from the traffic model): arriving traffic brakes, some hazard lights
            out = {}
            for key, bottleneck in sim.traffic.queued.items():
                if bottleneck[0] == crash:
                    out[key[0]] = {"p_brake": 0.004, "p_hazard": 0.006}
            for s in self.area.extra["upstream"]:
                out[s] = {"p_brake": 0.05, "p_hazard": 0.04}
            out[crash] = {"speed": 0.5, "p_brake": 0.08, "p_hazard": 0.06, "stationary": True, "capacity": 0.33}
            return out
        if k == "oil_spill":
            fx = {"speed": 0.85, "p_slip": 0.15, "capacity": 0.85}
            return {s: fx for s in self.area.segs}
        if k == "wrong_way_driver":
            out = {}
            for v in self.special:
                seg = sim.graph[v.seg]
                for sid in {seg.sid, *sim.graph.into.get(seg.from_node, ())}:
                    out[sid] = {"p_brake": 0.05, "p_hazard": 0.05}
            return out
        if k == "fog":
            fx = {"speed": 0.6, "fog": True, "visibility": (60, 140)}
            return {s: fx for s in self.area.segs}
        if k == "storm":
            band = storm_band(sim.t - self.started_t)
            self.area.polygon = band
            x0, _, x1, _ = band.bounds
            fx = {"speed": 0.8, "wiper": 3, "p_hazard": 0.002}
            return {s.sid: fx for s in sim.graph.values() if x0 <= s.mid[0] <= x1 and 48.03 <= s.mid[1] <= 48.25}
        if k == "animal":
            fx = {"speed": 0.6, "p_brake": 0.15}
            return {s: fx for s in self.area.segs}
        if k == "debris":
            fx = {"speed": 0.6, "p_brake": 0.12, "p_hazard": 0.03}
            return {s: fx for s in self.area.segs}
        return {}

    def camera_alarm(self, sensor_pos) -> bool:
        """Wrong-way cameras within 800 m of the wrong-way driver see it."""
        return any(dist_m(v.lonlat, sensor_pos) <= 800 for v in self.special if v.wrong)


def merge(effects: list[dict]) -> dict:
    out: dict = {}
    for fx in effects:
        for key, val in fx.items():
            if key == "speed":
                out[key] = min(out.get(key, 1.0), val)
            elif key.startswith("p_"):
                out[key] = max(out.get(key, 0.0), val)
            else:
                out[key] = val
    return out
