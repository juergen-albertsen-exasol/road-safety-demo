"""Scenarios for the road safety simulator: where they happen and what they change.

A scenario never writes a hazard. It only changes how the synthetic vehicles and sensors in its
area behave (ESC interventions, braking, fog lights, low friction readings ...). Whether a hazard
appears is decided by the detection SQL in roadsafety/hazards.sql alone.

Used by scripts/simulate_road_safety.py: `trigger` resolves the area and writes SCENARIO_LOG; the
running simulator reads SCENARIO_LOG every tick and applies the effects below.

Where: every scenario can be placed anywhere it makes sense. `locate()` turns a road (number or street
name), a place name (ROAD_DATA.PLACES: suburbs, villages, motorway junctions, stations, landmarks; or a
street), a direction and/or coordinates into one road segment and driving direction, checks that the
scenario fits there (a wrong-way driver needs a one-way carriageway, ...) and stores it as
"seg:<segment id>:<f|b>" (or "pt:<lat>,<lon>" for fog and storm) in SCENARIO_LOG.area. The few named
areas of the first version (a99-nord, isar-bridges, ...) still work.
"""
from __future__ import annotations

import difflib
import math
import random
import re
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
        "areas": "any road (a stretch of about 500 m), isar-bridges, or one bridge by name (e.g. reichenbachbruecke)",
    },
    "accident": {
        "label": "accident", "default_area": "a99-nord",
        "injects": "2 vehicles stop with airbag deployed and hazard lights on; the accident closes 2 of 3 lanes, so a "
                   "queue builds behind it; traffic arriving at the queue brakes hard and switches on hazard lights",
        "areas": "any main road (motorway to tertiary); named: a99-nord, landshuter-allee",
    },
    "oil_spill": {
        "label": "temporary slippery road (oil spill)", "default_area": "a9-freimann",
        "injects": "on about 500 m, vehicles get ESC/ABS interventions and slow down; the outside temperature stays "
                   "normal, so it is not ice; nothing else changes",
        "areas": "any road (about 500 m in the direction of travel); named: a9-freimann",
    },
    "wrong_way_driver": {
        "label": "wrong-way driver", "default_area": "a9-freimann",
        "injects": "1 vehicle drives against the direction of travel on a one-way carriageway; oncoming traffic "
                   "brakes and switches on hazard lights; wrong-way cameras within 800 m detect it",
        "areas": "any one-way carriageway of a motorway, trunk or primary road; named: a9-freimann",
    },
    "fog": {
        "label": "reduced visibility (fog)", "default_area": "north",
        "injects": "vehicles in the area switch on fog lights and slow down; road weather sensors there "
                   "report visibility below 150 m",
        "areas": "an area of about 6 x 5 km around any place; named: north (Freimann to the A99)",
    },
    "storm": {
        "label": "exceptional weather (storm)", "default_area": "west-east",
        "injects": "a storm band about 3 km wide moves from west to east in 20 minutes: wipers at maximum, "
                   "slower driving",
        "areas": "a band moving across any place (about 10 km); named: west-east (the whole city)",
    },
    "animal": {
        "label": "animal on the road", "default_area": "forstenrieder-park",
        "injects": "vehicles passing one spot brake hard",
        "areas": "any road, best along a forest or park; named: forstenrieder-park",
    },
    "debris": {
        "label": "debris on the road", "default_area": "a96",
        "injects": "vehicles passing one spot swerve and brake hard, some switch on hazard lights",
        "areas": "any road (one direction); named: a96",
    },
    "roadworks": {
        "label": "short-term road works", "default_area": None,
        "injects": "a mobile road works site closes a lane on about 300 m: the road operator's warning trailer "
                   "reports it, vehicles slow down and some brake hard",
        "areas": "any road (one direction)",
    },
}
ALIASES = {"wrong_way": "wrong_way_driver", "wrong_driver": "wrong_way_driver", "ghost_driver": "wrong_way_driver",
           "ice": "slippery_road", "black_ice": "slippery_road", "slippery": "slippery_road", "oil": "oil_spill",
           "road_works": "roadworks", "works": "roadworks", "visibility": "fog", "weather": "storm",
           "obstacle": "debris", "crash": "accident"}

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
    """Turn a scenario type and area into road segments and an outline."""
    area = area or SCENARIOS[kind]["default_area"]
    if area is None:
        raise ValueError(f"{kind} needs a place: --road, --near or --at")
    if area.startswith(("seg:", "pt:")):
        return build(kind, area, graph, c)
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
        while length < 500:                          # about 500 m in the direction of travel
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


def point_area(name, desc, spot, graph, same_direction=False, radius=150.0, fwd=True) -> Area:
    """Segments of the same road within `radius` m of one spot (only the spot's driving direction if asked)."""
    def same_road_(s):
        return (spot.name and s.name == spot.name) or (spot.ref and s.ref == spot.ref)
    segs = set()
    for s in graph.values():
        if same_road_(s) and dist_m(s.mid, spot.mid) <= radius:
            if same_direction and spot.oneway != 0 and abs((s.heading - spot.heading + 180) % 360 - 180) > 45:
                continue
            segs.add(s.sid)
    segs.add(spot.sid)
    return Area(name, desc, segs, Point(spot.mid).buffer(0.0012), spot.mid, spot.sid)


def storm_band(elapsed_s: float, centre: tuple | None = None):
    """The storm band at this moment: across the whole city, or about 10 km across a place (lon, lat)."""
    x0, x1, y0, y1 = (11.36, 11.84, 48.03, 48.25) if centre is None else \
        (centre[0] - 0.07, centre[0] + 0.07, centre[1] - 0.045, centre[1] + 0.045)
    lon = x0 + (x1 - x0) * min(1.0, max(0.0, elapsed_s) / STORM_SECONDS)
    return box(lon - 0.02, y0, lon + 0.02, y1)


# --------------------------------------------------------------------------- anywhere: locate and build
CENTRE = (11.575, 48.137)
MAIN = {"motorway", "trunk", "primary", "secondary", "tertiary"}
LINKS = {"motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
MINOR = {"unclassified", "residential", "living_street"}
ROAD_KINDS = {  # scenario -> road classes where the spot may be
    "accident": MAIN, "wrong_way_driver": {"motorway", "trunk", "primary"},
    "slippery_road": MAIN | LINKS | MINOR, "oil_spill": MAIN | LINKS | MINOR, "animal": MAIN | LINKS | MINOR,
    "debris": MAIN | LINKS | MINOR, "roadworks": MAIN | LINKS | MINOR,
}
AREA_KINDS = {"fog", "storm"}
COMPASS = {"north": 0, "northbound": 0, "n": 0, "east": 90, "eastbound": 90, "e": 90, "south": 180,
           "southbound": 180, "s": 180, "west": 270, "westbound": 270, "w": 270}
INBOUND = {"in", "inbound", "intothecity", "tothecity", "citybound", "stadteinwaerts", "zentrum", "centre", "center"}
OUTBOUND = {"out", "outbound", "outofthecity", "stadtauswaerts"}
NEAR_MAX_M = 4000.0             # the road must pass within this distance of the named place
FAR = {  # destinations outside the study area, for "toward <place>" (lon, lat)
    "salzburg": (13.04, 47.80), "rosenheim": (12.13, 47.86), "nuernberg": (11.08, 49.45), "nuremberg": (11.08, 49.45),
    "ingolstadt": (11.43, 48.77), "stuttgart": (9.18, 48.78), "augsburg": (10.90, 48.37), "lindau": (9.68, 47.55),
    "garmisch": (11.10, 47.49), "garmischpartenkirchen": (11.10, 47.49), "starnberg": (11.34, 48.00),
    "landshut": (12.15, 48.54), "deggendorf": (12.96, 48.84), "passau": (13.43, 48.57), "muehldorf": (12.52, 48.25),
    "flughafen": (11.786, 48.354), "airport": (11.786, 48.354), "freising": (11.75, 48.40), "regensburg": (12.10, 49.01),
    "innsbruck": (11.40, 47.27), "berlin": (13.40, 52.52), "lindaubodensee": (9.68, 47.55),
}


@dataclass
class Spot:
    sid: int
    fwd: bool
    description: str
    notes: list = field(default_factory=list)

    @property
    def spec(self) -> str:
        return f"seg:{self.sid}:{'f' if self.fwd else 'b'}"


def bearing(a, b) -> float:
    return math.degrees(math.atan2((b[0] - a[0]) * KX, (b[1] - a[1]) * KY)) % 360


def angle_off(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def compass_word(hdg: float) -> str:
    return ["northbound", "eastbound", "southbound", "westbound"][int(((hdg + 45) % 360) // 90)]


def road_key(text: str) -> str:
    """'A9', 'a 9', 'A-9' -> 'a9'; 'B2R' or 'Mittlerer Ring' -> 'b2r'."""
    t = norm(text)
    return "b2r" if t in ("mittlererring", "b2r", "ring") else t


def seg_refs(s) -> set:
    return {road_key(r) for r in (s.ref or "").split(";") if r.strip()}


_PLACES: list | None = None


def places(c) -> list:
    global _PLACES
    if _PLACES is None:
        _PLACES = [] if c is None else [
            {**r, "key": norm(r["name"]), "pos": (float(r["lon"]), float(r["lat"]))}
            for r in c.execute('SELECT "name", "kind", "ref", "lat", "lon" FROM ROAD_DATA.PLACES')]
    return _PLACES


def find_place(name: str, graph, c) -> tuple[str, tuple]:
    """A place name -> (the name found, (lon, lat)). Places first, then street names."""
    key = norm(name)
    if key in FAR:
        return name, FAR[key]
    ps = places(c)
    rank = {"motorway_junction": 0, "city": 1, "town": 1, "village": 2, "suburb": 2, "borough": 2, "quarter": 3,
            "neighbourhood": 4, "station": 5, "landmark": 6, "hamlet": 7}
    exact = [p for p in ps if p["key"] == key]
    if exact:
        p = min(exact, key=lambda p: rank.get(p["kind"], 9))
        return p["name"], p["pos"]
    streets = {}
    for s in graph.values():
        if s.name and norm(s.name) == key:
            streets.setdefault(s.name, []).append(s.mid)
    if streets:
        nm, pts = next(iter(streets.items()))
        pts.sort(key=lambda m: dist_m(m, CENTRE))
        return nm, pts[len(pts) // 2]
    part = [p for p in ps if key in p["key"] or (len(p["key"]) >= 4 and p["key"] in key)]
    if part:
        p = min(part, key=lambda p: (rank.get(p["kind"], 9), len(p["key"])))
        return p["name"], p["pos"]
    close = difflib.get_close_matches(key, list({p["key"] for p in ps}), n=1, cutoff=0.75)
    if close:
        p = next(p for p in ps if p["key"] == close[0])
        return p["name"], p["pos"]
    raise ValueError(f"no place called '{name}' found (places, motorway junctions, stations, landmarks, streets)")


def nearest_place(pos, c, max_m=3000.0) -> str | None:
    best, bd = None, max_m
    for p in places(c):
        if p["kind"] in ("motorway_junction", "suburb", "quarter", "village", "town", "neighbourhood", "station", "landmark"):
            d = dist_m(p["pos"], pos)
            if d < bd:
                best, bd = p["name"], d
    return best


def road_label(s) -> str:
    if "2R" in (s.ref or ""):
        return f"Mittlerer Ring ({s.name})" if s.name else "Mittlerer Ring"
    return s.ref.split(";")[0] if s.ref and s.road_class in ("motorway", "trunk", "motorway_link") else (s.name or s.ref or "an unnamed road")


def describe(graph, sid: int, fwd: bool, c) -> str:
    s = graph[sid]
    hdg = s.heading if fwd else (s.heading + 180) % 360
    near = nearest_place(s.mid, c)
    two_way = s.oneway == 0 and s.road_class not in ("motorway", "trunk")
    lab = road_label(s)
    lab = f"the {lab}" if (s.ref and lab == s.ref.split(";")[0]) or lab.startswith("Mittlerer Ring") else lab
    return f"{lab}{'' if two_way else ' ' + compass_word(hdg)}" + (f" near {near}" if near else "")


def locate(kind: str, graph, c, road: str | None = None, near: str | None = None, direction: str | None = None,
           at: str | None = None) -> tuple[str, str, list]:
    """Find where a scenario goes. Returns (area spec, description, notes) or raises ValueError with a hint."""
    notes = []
    anchor, anchor_name = None, None
    if at:
        lat, lon = (float(x) for x in at.replace(" ", "").split(","))
        anchor, anchor_name = (lon, lat), f"{lat:.4f}, {lon:.4f}"
    elif near:
        anchor_name, anchor = find_place(near, graph, c)
    if kind in AREA_KINDS:
        if anchor is None and road:
            anchor_name, anchor = find_place(road, graph, c)
        if anchor is None:
            raise ValueError(f"{kind} needs a place: --near or --at")
        return f"pt:{anchor[1]:.5f},{anchor[0]:.5f}", f"around {anchor_name}", notes
    classes = ROAD_KINDS[kind]
    want = road_key(road) if road else None
    cands = []
    for s in graph.values():
        if s.road_class not in classes or s.length < 40:
            continue
        if want and not (want in seg_refs(s) or (s.name and norm(s.name) == want)):
            continue
        for fwd in (True, False):
            if (fwd and s.oneway == -1) or (not fwd and s.oneway == 1):
                continue
            if kind == "wrong_way_driver" and (s.oneway != 1 or not fwd):
                continue
            cands.append((s, fwd))
    if not cands:
        if want and kind == "wrong_way_driver":
            raise ValueError(f"'{road}' has no one-way carriageway of a motorway, trunk or primary road: a wrong-way driver "
                             f"needs a road with separated carriageways (e.g. a motorway or the Mittlerer Ring)")
        if want:
            hint = ""
            try:
                found, _ = find_place(road, graph, c)
                hint = f"; '{found}' is a place, not a road: use --near \"{found}\""
            except ValueError:
                pass
            raise ValueError(f"no road '{road}' for cars found that fits a {kind.replace('_', ' ')}{hint}")
        raise ValueError("no road found")
    if direction:
        d = norm(direction)
        m = re.match(r"\s*(towards?|to|nach|richtung)\s+(.+)", direction, re.I)
        if m and d not in INBOUND:
            _, tpos = find_place(m.group(2), graph, c)
            want_hdg = lambda s: bearing(s.mid, tpos)          # noqa: E731
        elif d in INBOUND:
            want_hdg = lambda s: bearing(s.mid, CENTRE)        # noqa: E731
        elif d in OUTBOUND:
            want_hdg = lambda s: (bearing(s.mid, CENTRE) + 180) % 360   # noqa: E731
        elif d in COMPASS:
            want_hdg = lambda s, h=COMPASS[d]: h                # noqa: E731
        else:
            raise ValueError(f"unknown direction '{direction}': use north/south/east/west, in/out of the city, or toward <place>")
        kept = [(s, f) for s, f in cands if angle_off(s.heading if f else (s.heading + 180) % 360, want_hdg(s)) <= 70]
        if not kept:
            raise ValueError(f"no carriageway going {direction} found here")
        cands = kept
    if anchor is not None:
        s, fwd = min(cands, key=lambda sf: (dist_m(sf[0].mid, anchor), -sf[0].length))
        d = dist_m(s.mid, anchor)
        if d > NEAR_MAX_M:
            raise ValueError(f"{road_label(s) if want else 'no fitting road'} doesn't pass near {anchor_name} "
                             f"(nearest {d / 1000:.1f} km)")
    elif want:
        s, fwd = min(cands, key=lambda sf: dist_m(sf[0].mid, CENTRE))
        notes.append("no place given: the spot closest to the city centre")
    else:
        raise ValueError("say where: --road, --near or --at")
    if s.road_class in MINOR:
        notes.append("a minor street: few probe vehicles pass, detection can take several minutes")
    if kind == "animal" and c is not None:
        ok = c.execute('SELECT COUNT(*) FROM ROAD_DATA.ROAD_AREAS WHERE "kind" IN (\'forest\', \'park\') '
                       'AND ABS("centroid_lat" - {la!f}) < 0.03 AND ABS("centroid_lon" - {lo!f}) < 0.045',
                       {"la": s.mid[1], "lo": s.mid[0]}).fetchval()
        if not ok:
            notes.append("no forest or park nearby: an animal is unusual here")
    return Spot(s.sid, fwd, describe(graph, s.sid, fwd, c)).spec, describe(graph, s.sid, fwd, c), notes


def arrivals(graph) -> dict:
    """node -> [(sid, forward)] of driving directions that end at the node."""
    if not hasattr(graph, "_arrivals"):
        arr: dict = {}
        for node, lst in graph.exits.items():
            for sid, fwd in lst:
                s = graph[sid]
                arr.setdefault(s.to_node if fwd else s.from_node, []).append((sid, fwd))
        graph._arrivals = arr
    return graph._arrivals


def same_road(a, b) -> bool:
    return bool((a.ref and seg_refs(a) & seg_refs(b)) or (a.name and a.name == b.name))


def along(graph, sid: int, fwd: bool, metres: float, upstream: bool = False) -> list:
    """Driving directions that follow (or lead into) this one along the same road, for about `metres`."""
    out, cur, length = [], (sid, fwd), 0.0
    seen = {sid}
    while length < metres:
        s = graph[cur[0]]
        if upstream:
            node = s.from_node if cur[1] else s.to_node
            nxt = [k for k in arrivals(graph).get(node, ()) if k[0] not in seen]
        else:
            node = s.to_node if cur[1] else s.from_node
            nxt = [k for k in graph.exits.get(node, ()) if k[0] not in seen]
        nxt = [k for k in nxt if same_road(graph[k[0]], s)] or [k for k in nxt if graph[k[0]].road_class == s.road_class]
        if not nxt:
            break
        cur = min(nxt, key=lambda k: angle_off(graph[k[0]].heading if k[1] else (graph[k[0]].heading + 180) % 360,
                                               s.heading if cur[1] else (s.heading + 180) % 360))
        seen.add(cur[0])
        out.append(cur)
        length += graph[cur[0]].length
    return out


def build(kind: str, spec: str, graph, c=None) -> Area:
    """The area of a scenario placed with locate()."""
    if spec.startswith("pt:"):
        lat, lon = (float(x) for x in spec[3:].split(","))
        name = nearest_place((lon, lat), c, 6000) or f"{lat:.3f}, {lon:.3f}"
        if kind == "fog":
            bx = (lon - 0.04, lat - 0.025, lon + 0.04, lat + 0.025)
            segs = {s.sid for s in graph.values() if bx[0] <= s.mid[0] <= bx[2] and bx[1] <= s.mid[1] <= bx[3]}
            return Area(spec, f"the area around {name}", segs, box(*bx), (lon, lat))
        if kind == "storm":
            return Area(spec, f"a band moving from west to east across {name}", set(), storm_band(0, (lon, lat)),
                        (lon, lat), extra={"centre": (lon, lat)})
        raise ValueError(f"{kind} needs a road segment")
    _, sid, d = spec.split(":")
    sid, fwd = int(sid), d == "f"
    if sid not in graph:
        raise ValueError(f"segment {sid} not in the road network")
    s = graph[sid]
    desc = describe(graph, sid, fwd, c)
    if kind == "accident":
        up = along(graph, sid, fwd, 600, upstream=True)
        upstream = {k[0] for k in up}
        return Area(spec, desc, {sid} | upstream, outline({sid}, graph), s.mid, sid, {"upstream": upstream, "fwd": fwd})
    if kind == "wrong_way_driver":
        return Area(spec, desc, {sid}, outline({sid}, graph), s.mid, sid, {"fwd": fwd})
    if kind in ("oil_spill", "roadworks"):
        metres = 500 if kind == "oil_spill" else 300
        segs = {sid} | {k[0] for k in along(graph, sid, fwd, metres - s.length)}
        return Area(spec, desc, segs, outline(segs, graph), s.mid, sid, {"fwd": fwd})
    if kind == "slippery_road":
        if s.crosses_isar or s.bridge_name:
            key = s.bridge_name or s.name
            segs = {x.sid for x in graph.values() if (x.bridge_name or x.name) == key and (x.crosses_isar or x.bridge_name)}
            return Area(spec, f"the {key}", segs, outline(segs, graph), s.mid, sid)
        a = point_area(spec, desc, s, graph, radius=250)
        return a
    if kind == "animal":
        return point_area(spec, desc, s, graph)
    if kind == "debris":
        return point_area(spec, desc, s, graph, same_direction=True, fwd=fwd)
    raise ValueError(f"unknown scenario '{kind}'")


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
            fwd = self.area.extra.get("fwd", True)
            for k, v in enumerate((first, second)):
                v.special, v.frozen, v.airbag, v.hazard_on = self.id, True, True, True
                v.seg, v.fwd, v.pos, v.speed, v.target = crash.sid, fwd, crash.length * 0.5 + 8 * k, 0.0, 0.0
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
        self.trailer = None
        if self.kind == "roadworks":                 # the road operator's warning trailer at the start of the site
            s = sim.graph[self.area.start_seg]
            self.trailer = {"sensor_id": f"WT-{self.id}", "sensor_type": "warning_trailer", "location_kind": "mobile",
                            "sensor_name": f"Warning trailer, road works on {self.area.description}",
                            "segment_id": s.sid, "provider_id": "ROAD_OP", "lat": s.mid[1], "lon": s.mid[0]}
            if not sim.c.execute('SELECT COUNT(*) FROM ROAD_SAFETY.INFRA_SENSORS WHERE "sensor_id" = {i}',
                                 {"i": self.trailer["sensor_id"]}).fetchval():
                t = self.trailer
                sim.c.execute('INSERT INTO ROAD_SAFETY.INFRA_SENSORS VALUES ({a}, {b}, {c}, {d}, {e!d}, {f}, {g!f}, {h!f})',
                              {"a": t["sensor_id"], "b": t["sensor_type"], "c": t["location_kind"], "d": t["sensor_name"][:200],
                               "e": t["segment_id"], "f": t["provider_id"], "g": t["lat"], "h": t["lon"]})
            sim.sensors.append(self.trailer)

    def stop(self, sim) -> None:
        for v in self.special:
            v.special = 0
            v.frozen = v.wrong = v.airbag = v.hazard_on = False
            sim.respawn(v)
        if self.trailer in sim.sensors:
            sim.sensors.remove(self.trailer)         # the site is cleared: the trailer stops reporting

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
                if not v.wrong:                      # left the road at the end of the carriageway
                    continue
                seg = sim.graph[v.seg]
                for sid in {seg.sid, *sim.graph.into.get(seg.from_node, ())}:
                    out[sid] = {"p_brake": 0.05, "p_hazard": 0.05}
            return out
        if k == "fog":
            fx = {"speed": 0.6, "fog": True, "visibility": (60, 140)}
            return {s: fx for s in self.area.segs}
        if k == "storm":
            band = storm_band(sim.t - self.started_t, self.area.extra.get("centre"))
            self.area.polygon = band
            x0, _, x1, _ = band.bounds
            fx = {"speed": 0.8, "wiper": 3, "p_hazard": 0.002}
            _, y0, _, y1 = band.bounds
            return {s.sid: fx for s in sim.graph.values() if x0 <= s.mid[0] <= x1 and y0 <= s.mid[1] <= y1}
        if k == "animal":
            fx = {"speed": 0.6, "p_brake": 0.15}
            return {s: fx for s in self.area.segs}
        if k == "debris":
            fx = {"speed": 0.6, "p_brake": 0.12, "p_hazard": 0.03}
            return {s: fx for s in self.area.segs}
        if k == "roadworks":
            fx = {"speed": 0.55, "p_brake": 0.01, "capacity": 0.6}
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
