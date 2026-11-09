"""Munich Road Conditions: safety-related traffic information (incidents) detected from connected vehicles and road sensors."""
import importlib.util
import math
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
from dash import Dash, Input, Output, Patch, State, ctx, dash_table, dcc, html
from dash.exceptions import PreventUpdate

_HELPER_SPEC = importlib.util.spec_from_file_location(
    "dash_server_generated_exasol_helper", Path(__file__).with_name("dash_server_exasol.py")
)
_HELPER = importlib.util.module_from_spec(_HELPER_SPEC)
_HELPER_SPEC.loader.exec_module(_HELPER)
load_rows = _HELPER.load_rows
has_error = _HELPER.has_error

APP_DIR = Path(__file__).resolve().parent
TITLE = "Munich Road Conditions"
SUBTITLE = "Safety-related traffic information, detected from connected vehicles and road sensors"
CAVEAT = ("The vehicles are probe vehicles, the connected share of the traffic. Traffic volume follows real data (BASt hourly counts, "
          "known hotspots); the road network, roadworks and weather are open data. Incidents, their reach and "
          "diversions are derived by SQL and a routing UDF in Exasol from the vehicle and sensor signals alone.")
ATTRIBUTION = ("Road network (c) OpenStreetMap contributors (ODbL). Traffic counts: Bundesanstalt fuer Strassenwesen "
               "(CC BY 4.0). Roadworks: City of Munich (dl-de/by-2.0), (c) Die Autobahn GmbH des Bundes. Weather: "
               "Deutscher Wetterdienst (CC BY 4.0).")
REFRESH_MS = 5000
FREE_EVERY = 6                # resend the free-flowing roads (the biggest layer) every 6th refresh = 30 s
# traffic map: which roads are drawn depends on the zoom; only the visible area is queried
MAIN_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"}
MID_CLASSES = MAIN_CLASSES | {"secondary", "secondary_link", "tertiary", "tertiary_link"}
ZOOM_MID = 12.0                # from here: free-flowing secondary and tertiary roads, slow or worse on all roads
ZOOM_ALL = 13.5                # from here: every road, roads without probes in grey
NODATA_COLOR = "#cbd5e1"
VIEW0 = {"lat": 48.150, "lon": 11.590, "zoom": 10.0}

INK = "#1f2937"
MUTED = "#6b7280"
LINE = "#e5e7eb"
CARD = {"background": "#ffffff", "border": f"1px solid {LINE}", "borderRadius": "10px", "padding": "14px 16px"}
STATE_STYLE = {  # traffic state -> (colour, width)
    "free": ("#22c55e", 2), "slow": ("#f59e0b", 3), "congested": ("#dc2626", 4), "standstill": ("#7f1d1d", 5),
}
INCIDENT = {  # hazard type -> (label, colour)
    "accident_area": ("Accident", "#dc2626"), "unmanaged_blockage": ("Accident: road blocked", "#7f1d1d"),
    "slippery_road": ("Slippery road", "#ea580c"), "obstacle": ("Obstacle on the road", "#ea580c"),
    "wrong_way_driver": ("Wrong-way driver", "#c026d3"), "reduced_visibility": ("Reduced visibility", "#0d9488"),
    "exceptional_weather": ("Exceptional weather", "#7c3aed"), "roadworks": ("Short-term road works", "#b45309"),
}
ROUTE_COLORS = ["#2563eb", "#60a5fa"]
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]   # categorical slots 1-3 of the validated reference palette
# one fixed colour per provider: categorical slots 1-7 of the validated reference palette, in fixed order
PROVIDER_COLOR = {"OEM_A": "#2a78d6", "OEM_B": "#eb6834", "OEM_C": "#1baf7a", "OEM_D": "#eda100",
                  "OEM_E": "#e87ba4", "OEM_F": "#008300", "NAV": "#6250d6"}
GRID = "#eceae4"
PROVIDER_LABEL = {"OEM_A": "OEM A", "OEM_B": "OEM B", "OEM_C": "OEM C", "OEM_D": "OEM D", "OEM_E": "OEM E",
                  "OEM_F": "OEM F", "NAV": "Navigation", "ROAD_OP": "Road operator"}
LAYERS = [("traffic", "Traffic state"), ("incidents", "Incidents and diversions"), ("sensors", "Road sensors"),
          ("roadworks", "Roadworks (long-term, real)"),
          ("probes", "Probe vehicles"), ("accidents", "Accident history (2016-2025)")]
HAZARD_PARAMS = {"window_min": 15, "min_vehicles": 3, "min_providers": 2, "friction_max": 0.4, "blockage_after_s": 600,
                 "clear_vehicles": 5}
TRAFFIC = {"now_min": 5, "before_min": 10, "slow": 0.7, "congested": 0.4, "standstill": 0.15}

INDEX_STRING = """<!DOCTYPE html>
<html><head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}
<style>
body { margin: 0; background: #f4f5f7; color: #1f2937;
       font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.page { max-width: 1500px; margin: 0 auto; padding: 18px 24px 40px; }
h1 { font-size: 26px; margin: 0; letter-spacing: -0.01em; }
.sub { color: #6b7280; margin-top: 4px; font-size: 14px; }
.clock { color: #374151; font-size: 13px; margin-top: 6px; font-variant-numeric: tabular-nums; }
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.kpi-label { font-size: 12px; color: #6b7280; }
.kpi-value { font-size: 24px; font-weight: 650; margin-top: 2px; font-variant-numeric: tabular-nums; }
.kpi-note { font-size: 11px; color: #9ca3af; margin-top: 2px; }
.grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 14px; margin-top: 14px; }
@media (max-width: 1000px) { .grid, .kpis { grid-template-columns: 1fr; } .page { padding: 14px; } }
.side { max-height: 700px; overflow-y: auto; }
.section-title { font-weight: 650; font-size: 14px; margin: 4px 0 6px; }
.inc { border: 1px solid #fecaca; background: #fff7f7; border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
.inc-title { font-weight: 700; font-size: 15px; }
.inc-meta { font-size: 12px; color: #4b5563; margin-top: 3px; }
.div-box { margin-top: 8px; padding: 8px 10px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; font-size: 13px; }
.div-box b { color: #1d4ed8; }
.trg-box { margin-top: 8px; padding: 6px 10px; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; font-size: 12px; }
.trg-box summary { cursor: pointer; font-weight: 600; color: #991b1b; }
.trg-row { margin-top: 2px; color: #374151; font-variant-numeric: tabular-nums; }
.trg-time { color: #6b7280; }
.trg-note { margin-top: 4px; color: #6b7280; font-size: 11px; }
.row { display: flex; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f3f4f6; font-size: 13px; }
.badge { font-size: 11px; font-weight: 600; color: #fff; border-radius: 10px; padding: 1px 7px; white-space: nowrap; height: 16px; }
.row-main { flex: 1; }
.row-sub { font-size: 12px; color: #6b7280; margin-top: 1px; }
.checks label, .checks .dash-options-list-option { display: flex !important; flex-direction: row !important;
  align-items: flex-start; gap: 6px; margin: 3px 0; font-size: 13px; }
.checks .dash-options-list-option-wrapper, .checks input { flex: none; display: inline-flex; margin-top: 2px; }
.checks .dash-options-list-option-text { display: inline; }
.controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: #4b5563; margin: 4px 6px 2px; }
.legend span i { display: inline-block; width: 18px; height: 4px; vertical-align: middle; margin-right: 4px; border-radius: 2px; }
.caveat { margin-top: 14px; font-size: 13px; color: #6b7280; border-left: 3px solid #d1d5db; padding-left: 10px; }
.attrib { margin-top: 6px; font-size: 11px; color: #9ca3af; }
details.sql { margin-top: 14px; background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 10px 16px; }
details.sql summary { cursor: pointer; font-weight: 600; }
.sql-item { margin-top: 12px; }
.sql-meta { font-size: 12px; color: #6b7280; margin-bottom: 4px; }
.sql-text { margin: 0; white-space: pre-wrap; font-size: 12px; background: #f9fafb; border: 1px solid #e5e7eb; padding: 8px 10px; border-radius: 6px; max-height: 420px; overflow-y: auto; }
.err { color: #991b1b; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 8px 12px; margin-top: 10px; font-size: 13px; }
.muted { color: #6b7280; font-size: 13px; }
.tabs-row { margin-top: 12px; }
.data-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }
.data-grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }
@media (max-width: 1000px) { .data-grid, .data-grid2 { grid-template-columns: 1fr; } }
.part-title { font-size: 18px; font-weight: 700; margin: 22px 0 2px; }
.part-sub { font-size: 13px; color: #6b7280; }
.tbl-wrap { max-height: 360px; overflow: auto; }
table.tbl { border-collapse: collapse; width: 100%; font-size: 12px; font-variant-numeric: tabular-nums; }
table.tbl th { text-align: left; color: #6b7280; font-weight: 600; border-bottom: 1px solid #e5e7eb; padding: 4px 6px; position: sticky; top: 0; background: #fff; }
table.tbl td { border-bottom: 1px solid #f3f4f6; padding: 3px 6px; vertical-align: top; }
</style></head>
<body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"""


# ---------- SQL with a log ----------
def render_sql(text, params):
    def sub(match):
        key, kind = match.group(1), match.group(2)
        if key not in params:
            return match.group(0)
        value = params[key]
        return "'" + str(value).replace("'", "''") + "'" if kind == "s" else str(value)
    return re.sub(r"\{(\w+)!(\w)\}", sub, text)


def run_sql(server, metadata, name, params, log):
    rel = f"queries/{name}.sql"
    started = time.perf_counter()
    rows = load_rows(server, metadata, __file__, rel, params=params)
    ms = (time.perf_counter() - started) * 1000
    error = rows[0]["_error"] if has_error(rows) else None
    log.append({"statement": rel, "rows": 0 if error else len(rows), "ms": round(ms),
                "sql": render_sql((APP_DIR / rel).read_text(), params), "error": error})
    if error:
        raise RuntimeError(f"{rel}: {error}")
    return [{k.lower(): v for k, v in r.items()} for r in rows]


def sql_foldout(log):
    items = []
    for i, entry in enumerate(log or [], 1):
        outcome = f"error: {entry['error']}" if entry["error"] else f"{entry['rows']:,} rows"
        items.append(html.Div([html.Div(f"#{i} {entry['statement']} - {outcome} - {entry['ms']:,} ms", className="sql-meta"),
                               html.Pre(entry["sql"], className="sql-text")], className="sql-item"))
    n = len(log or [])
    return html.Details([html.Summary(f"SQL executed ({n} statement{'s' if n != 1 else ''} in the last refresh)")] + items,
                        className="sql")


# ---------- helpers ----------
def ts(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("T", " ")[:19])


EVIDENCE_MATCH = {  # evidence kind of the detection -> does this signal carry it?
    "slip": lambda r: yes(r.get("esc_active")) == "yes" or yes(r.get("abs_active")) == "yes",
    "hard_brake": lambda r: yes(r.get("hard_braking")) == "yes",
    "hazard_lights": lambda r: yes(r.get("hazard_lights")) == "yes" or yes(r.get("stationary_vehicle")) == "yes",
    "airbag": lambda r: yes(r.get("airbag")) == "yes",
    "fog": lambda r: yes(r.get("fog_lights")) == "yes" or (r.get("visibility_m") is not None and num(r["visibility_m"]) < 150),
    "wiper_max": lambda r: num(r.get("wiper_level")) >= 3,
    "wrong_way": lambda r: True,
    "works": lambda r: yes(r.get("works_active")) == "yes",
}
CLASS_LABEL = {"evidence": "caused an incident", "noise": "background noise", "regular": "regular"}


def classify(rows, evidence):
    """Class of every stream row: evidence of a detected incident, noise (an event nothing confirms), or regular."""
    idx = {}
    for e in evidence:
        key = ("vehicle", str(int(num(e["vehicle_id"])))) if e["source"] == "vehicle" else ("sensor", e["sensor_id"])
        idx.setdefault(key, []).append((ts(e["ts"]), e["kind"], e["srti_category"]))
    for r in rows:
        t, hit = ts(r["ts"]), None
        for et, kind, srti in idx.get((r["source"], str(r["id"])), ()):
            if et <= t <= et + timedelta(seconds=30) and EVIDENCE_MATCH.get(kind, lambda _r: False)(r):
                hit = srti
                break
        r["incident"] = hit or ""
        r["class"] = "evidence" if hit else ("noise" if int(num(r.get("is_event"))) else "regular")
    return rows


def stream_cells(r):
    """One stream row as table cells (strings)."""
    ev = [lbl for k, lbl in (("esc_active", "ESC"), ("abs_active", "ABS"), ("hard_braking", "hard braking"),
                             ("hazard_lights", "hazard lights"), ("airbag", "AIRBAG")) if yes(r.get(k)) == "yes"]
    alarm = sensor_alarm({**r, "ts": r["ts"]}) if r["source"] == "sensor" else ""
    if alarm:
        ev.append(alarm)
    if r["source"] == "sensor":
        if r.get("surface_temp_c") is not None:
            env = f"surface {num(r['surface_temp_c']):.1f} C, friction {num(r['friction']):.2f}, visibility {int(num(r['visibility_m']))} m"
        else:
            env = "" if r.get("speed_kmh") is None else f"avg {int(num(r['speed_kmh']))} km/h"
    else:
        bits = []
        if r.get("outside_temp_c") is not None:
            bits.append(f"{num(r['outside_temp_c']):.1f} C")
        if r.get("wiper_level") is not None and int(num(r["wiper_level"])):
            bits.append(f"wipers {int(num(r['wiper_level']))}")
        if yes(r.get("fog_lights")) == "yes":
            bits.append("fog lights")
        env = ", ".join(bits)
    return {"time": str(r["ts"])[11:19], "class": CLASS_LABEL[r["class"]], "incident": r["incident"],
            "source": r["source"], "id": str(r["id"]), "provider": PROVIDER_LABEL.get(r["provider_id"], r["provider_id"]),
            "road": r.get("road") or "", "kmh": "" if r.get("speed_kmh") is None or r["source"] == "sensor" else int(num(r["speed_kmh"])),
            "heading": "" if r.get("heading_deg") is None else int(num(r["heading_deg"])),
            "events": ", ".join(ev), "readings": env, "position": f"{num(r['lat']):.5f}, {num(r['lon']):.5f}"}


STREAM_COLS = [("time", "time"), ("class", "class"), ("incident", "incident"), ("source", "source"), ("id", "vehicle / sensor"),
               ("provider", "provider"), ("road", "road"), ("kmh", "km/h"), ("heading", "heading"), ("events", "events / alerts"),
               ("readings", "environment / readings"), ("position", "position")]


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def dist_m(lat1, lon1, lat2, lon2):
    return math.hypot((lat1 - lat2) * 110540, (lon1 - lon2) * 74300)


_NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


def wkt_coords(wkt):
    out = []
    for part in re.findall(r"\(([^()]+)\)", wkt or ""):
        pts = re.findall(rf"({_NUM})\s+({_NUM})", part)
        if pts:
            out.append([(float(x), float(y)) for x, y in pts])
    return out


def wkt_geojson_lines(wkt):
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "MultiLineString", "coordinates": [[list(p) for p in ring] for ring in wkt_coords(wkt)]}}


_GEOM = {}
_CLASS = {}
_MID = {}          # segment id -> (lat, lon) of its midpoint, for the visible-area filter
_LAST = {}         # the traffic map's latest query results, reused when only the view changes
_STATIC = {}


def geometry(server, metadata, ids, log):
    """Segment geometry, cached per process: the whole network once, anything missing on demand."""
    if not _GEOM:
        for r in run_sql(server, metadata, "main_segments", {"first_id": 0, "last_id": 10_000_000}, log):
            sid = int(r["segment_id"])
            _GEOM[sid] = wkt_coords(r["geometry_wkt"])[0]
            _CLASS[sid] = r["road_class"]
            _MID[sid] = (num(r["mid_lat"]), num(r["mid_lon"]))
    missing = [i for i in ids if i not in _GEOM]
    if missing:
        for r in run_sql(server, metadata, "segments", {"ids": ", ".join(str(int(i)) for i in missing[:3000])}, log):
            _GEOM[int(r["segment_id"])] = wkt_coords(r["geometry_wkt"])[0]
    return _GEOM


HAZARD_TTL_S = 10.0          # the detection queries (about 2 s each) run at most this often, shared by all tabs
_HZ = {"t": 0.0, "hazards": [], "evidence": [], "log": []}
_HZ_LOCK = threading.Lock()


def detection(server, metadata, log):
    """Detected hazards (with reach) and their evidence, cached for HAZARD_TTL_S and shared by the map and the stream."""
    with _HZ_LOCK:
        if time.time() - _HZ["t"] >= HAZARD_TTL_S:
            own = []
            hazards = run_sql(server, metadata, "hazards_live", {**HAZARD_PARAMS, "reach_m": 2000, "toward_deg": 60}, own)
            evidence = run_sql(server, metadata, "hazard_evidence", HAZARD_PARAMS, own) if hazards else []
            _HZ.update(t=time.time(), hazards=hazards, evidence=evidence, log=own)
        log.extend(_HZ["log"])
        return _HZ["hazards"], _HZ["evidence"]


def static(server, metadata, log):
    if "roadworks" not in _STATIC:
        _STATIC["roadworks"] = run_sql(server, metadata, "roadworks_now", {}, log)
        _STATIC["accidents"] = run_sql(server, metadata, "hotspots", {"min_accidents": 15}, log)
    return _STATIC


def route_roads(rows):
    """Named roads of a route in driving order, without repeats."""
    names = []
    for r in sorted(rows, key=lambda r: int(r["seq"])):
        n = r["road"]
        if n and (not names or names[-1] != n):
            names.append(n)
    return names


def lines_trace(segs, geom, color, width, name, simplify=False):
    lat, lon = [], []
    for sid in segs:
        pts = geom.get(sid)
        if pts:
            if simplify and len(pts) > 3:                 # start, middle, end: enough for segments of at most 300 m
                pts = [pts[0], pts[len(pts) // 2], pts[-1]]
            lon += [p[0] for p in pts] + [None]
            lat += [p[1] for p in pts] + [None]
    return go.Scattermap(lat=lat, lon=lon, mode="lines", line=dict(width=width, color=color), name=name, hoverinfo="skip")


def empty_points(name):
    return go.Scattermap(lat=[], lon=[], mode="markers", name=name, hoverinfo="skip")


# Fixed trace slots, always in this order (empty when a layer is off or has no data), so that a refresh can
# send only the slots that change: slot 0, the free-flowing roads, is by far the biggest and changes slowly.
SLOTS = ["nodata", "free", "slow", "congested", "standstill", "accidents", "probes", "roadworks",
         "diversion1", "diversion2", "sensors", "sensor_alarm", "evidence"] + [f"incident:{t}" for t in INCIDENT]


def map_figure(d, layers, with_free=True, view=None):
    geom = d["geom"]
    traces = {}
    by_state = {k: [] for k in STATE_STYLE}
    nodata = []
    v = view or VIEW0
    zoom = v["zoom"]
    if "traffic" in layers:
        free_ok = MAIN_CLASSES if zoom < ZOOM_MID else MID_CLASSES if zoom < ZOOM_ALL else None
        busy_ok = MID_CLASSES if zoom < ZOOM_MID else None
        seen = set()
        for r in d["traffic"]:
            sid, st = int(r["segment_id"]), r["state"]
            seen.add(sid)
            ok = free_ok if st == "free" else busy_ok
            if st in by_state and (ok is None or r["road_class"] in ok):
                by_state[st].append(sid)
        if zoom >= ZOOM_ALL and with_free and v.get("box"):
            lat0, lat1, lon0, lon1 = v["box"]
            nodata = [sid for sid, (la, lo) in _MID.items()
                      if sid not in seen and lat0 <= la <= lat1 and lon0 <= lo <= lon1]
    traces["nodata"] = lines_trace(nodata, geom, NODATA_COLOR, 1.5, "No probes right now")
    for st in ("free", "slow", "congested", "standstill"):
        if st == "free" and not with_free:
            continue
        color, width = STATE_STYLE[st]
        traces[st] = lines_trace(by_state[st], geom, color, width, f"Traffic: {st}", simplify=True)
    rows = d["static"]["accidents"] if "accidents" in layers else []
    traces["accidents"] = go.Scattermap(lat=[num(r["mid_lat"]) for r in rows], lon=[num(r["mid_lon"]) for r in rows],
                                        mode="markers", name="Accident history (2016-2025)",
                                        marker=dict(size=[5 + min(num(r["accidents"]) / 6, 12) for r in rows],
                                                    color="#991b1b", opacity=0.3),
                                        text=[f"{r['road'] or ''}: {int(num(r['accidents']))} injury accidents 2016-2025"
                                              for r in rows], hoverinfo="text")
    rows = d["vehicles"] if "probes" in layers else []
    traces["probes"] = go.Scattermap(lat=[num(r["lat"]) for r in rows], lon=[num(r["lon"]) for r in rows], mode="markers",
                                     name="Probe vehicles", marker=dict(size=5 if zoom < 13 else 8, color="#475569", opacity=0.6),
                                     text=[vehicle_text(r, full=zoom >= 12) for r in rows], hoverinfo="text")
    rows = []
    if "roadworks" in layers:
        seen = set()
        for r in d["static"]["roadworks"]:
            if r["roadworks"] not in seen:
                seen.add(r["roadworks"])
                rows.append(r)
    traces["roadworks"] = go.Scattermap(lat=[num(r["mid_lat"]) for r in rows], lon=[num(r["mid_lon"]) for r in rows],
                                        mode="markers", name="Roadworks (real)", marker=dict(size=7, color="#d97706", opacity=0.8),
                                        text=[f"{r['roadworks']} (capacity {num(r['capacity_factor']):.0%})" for r in rows],
                                        hoverinfo="text")
    routes = {1: [], 2: []}
    map_layers = []
    if "incidents" in layers:
        for acc, rr in d["routes"].items():
            for no, (kind, rows) in rr.items():
                if kind == "diversion" and no in routes:
                    routes[no] += [int(r["segment_id"]) for r in sorted(rows, key=lambda r: int(r["seq"]))]
    traces["diversion1"] = lines_trace(routes[1], geom, ROUTE_COLORS[0], 7, "Diversion 1")
    traces["diversion2"] = lines_trace(routes[2], geom, ROUTE_COLORS[1], 5, "Diversion 2")
    sens = d.get("sensors", []) if "sensors" in layers else []
    normal = [r for r in sens if not sensor_alarm(r)]
    alarm = [r for r in sens if sensor_alarm(r)]
    traces["sensors"] = go.Scattermap(
        lat=[num(r["lat"]) for r in normal], lon=[num(r["lon"]) for r in normal], mode="markers", name="Road sensors",
        marker=dict(size=10, color=[SENSOR_KIND.get(r["sensor_type"], ("", "#111827"))[1] for r in normal], opacity=0.9),
        text=[sensor_text(r) for r in normal], hoverinfo="text")
    traces["sensor_alarm"] = go.Scattermap(
        lat=[num(r["lat"]) for r in alarm], lon=[num(r["lon"]) for r in alarm], mode="markers", name="Sensor alerts",
        marker=dict(size=16, color="#dc2626", opacity=0.95), text=[sensor_text(r) for r in alarm], hoverinfo="text")
    ev = d.get("evidence", []) if "incidents" in layers else []
    traces["evidence"] = go.Scattermap(
        lat=[num(e["lat"]) for e in ev], lon=[num(e["lon"]) for e in ev], mode="markers", name="Signals that caused an incident",
        marker=dict(size=9, color=[INCIDENT.get(e["hazard_type"], ("", "#dc2626"))[1] for e in ev], opacity=0.9),
        text=[evidence_text(e) for e in ev], hoverinfo="text")
    inc = [h for h in d["hazards"] if h["hazard_type"] in INCIDENT] if "incidents" in layers else []
    for t, (label, color) in INCIDENT.items():
        hs = [h for h in inc if h["hazard_type"] == t]
        traces[f"incident:{t}"] = go.Scattermap(
            lat=[num(h["lat"]) for h in hs], lon=[num(h["lon"]) for h in hs], mode="markers+text", name=label,
            marker=dict(size=22, color=color), text=[label.upper() for _ in hs], textposition="top right",
            textfont=dict(color=color, size=13),
            hovertext=[incident_text(h, label) for h in hs], hoverinfo="text")
    if not with_free:
        return traces, map_layers
    fig = go.Figure([traces[k] for k in SLOTS])
    fig.update_layout(
        map=dict(style="carto-positron", center=dict(lat=(view or VIEW0)["lat"], lon=(view or VIEW0)["lon"]),
                 zoom=(view or VIEW0)["zoom"], layers=map_layers),
        margin=dict(l=0, r=0, t=0, b=0), height=680, uirevision="keep", showlegend=False, paper_bgcolor="#ffffff")
    return fig


EVIDENCE_LABEL = {"slip": "ESC/ABS intervention", "hard_brake": "hard braking", "hazard_lights": "hazard lights",
                  "airbag": "airbag deployed", "fog": "fog lights / low visibility", "wiper_max": "wipers at maximum",
                  "wrong_way": "driving against the traffic", "works": "road works set up"}


def evidence_who(e):
    return (f"vehicle {int(num(e['vehicle_id']))} ({PROVIDER_LABEL.get(e['provider_id'], e['provider_id'])})"
            if e["source"] == "vehicle" else f"road sensor {e['sensor_id']}")


def trigger_box(h, evidence, max_rows=12):
    """The signals that triggered the incident (up to its confirmation), and how many confirmed it since."""
    ev = sorted((e for e in evidence if e["hazard_id"] == h["hazard_id"]), key=lambda e: (str(e["ts"]), evidence_who(e)))
    if not ev:
        return html.Div()
    conf = ts(h["confirmed_at"])
    before = [e for e in ev if ts(e["ts"]) <= conf]
    after = len(ev) - len(before)
    rows = [html.Div([html.Span(str(e["ts"])[11:19], className="trg-time"), " ", evidence_who(e), ": ",
                      html.B(EVIDENCE_LABEL.get(e["kind"], e["kind"]))], className="trg-row") for e in before[:max_rows]]
    if len(before) > max_rows:
        rows.append(html.Div(f"... and {len(before) - max_rows} more", className="trg-row"))
    return html.Details([
        html.Summary(f"Triggered by {plural(len(before), 'signal')} up to the confirmation at {str(h['confirmed_at'])[11:19]}"
                     + (f"; {plural(after, 'more signal')} confirmed it since" if after else "")),
        html.Div(rows),
        html.Div("The detection rule fires once enough vehicles, providers or sensors report the same kind of event "
                 "close together; these are the signals it counted.", className="trg-note"),
    ], className="trg-box", open=True)


def evidence_text(e):
    who = (f"vehicle {int(num(e['vehicle_id']))} ({PROVIDER_LABEL.get(e['provider_id'], e['provider_id'])})"
           if e["source"] == "vehicle" else f"road sensor {e['sensor_id']}")
    return (f"<b>Signal that caused an incident</b><br>{who}: {EVIDENCE_LABEL.get(e['kind'], e['kind'])} "
            f"at {str(e['ts'])[11:19]}<br>evidence for: {e['srti_category']}")


def plural(n, word):
    n = int(num(n))
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def vehicle_text(r, full=True):
    """What one probe vehicle reported in its newest signal, for the hover (short when zoomed out)."""
    prov = PROVIDER_LABEL.get(r["provider_id"], r["provider_id"])
    if not full:
        return (f"Vehicle {r['vehicle_id']} ({prov}), {int(num(r['speed_kmh']))} km/h"
                + (f" on {r['road']}" if r.get("road") else "") + "<br>zoom in for its signals")
    lines = [f"<b>Vehicle {r['vehicle_id']}</b> ({r.get('vehicle_type') or 'car'}, {prov})",
             f"newest signal {str(r['ts'])[11:19]}: {int(num(r['speed_kmh']))} km/h, heading {int(num(r['heading_deg']))}°"
             + (f" on {r['road']}" if r.get("road") else "")]
    env = []
    if r.get("outside_temp_c") is not None:
        env.append(f"outside {num(r['outside_temp_c']):.1f} °C")
    if r.get("wiper_level") is not None:
        env.append(f"wipers {['off', 'interval', 'normal', 'fast'][int(num(r['wiper_level']))]}")
    if yes(r.get("fog_lights")) == "yes":
        env.append("fog lights on")
    if env:
        lines.append(", ".join(env))
    ev = [lbl for k, lbl in (("esc_active", "ESC"), ("abs_active", "ABS"), ("hard_braking", "hard braking"),
                             ("hazard_lights", "hazard lights"), ("airbag", "AIRBAG")) if yes(r.get(k)) == "yes"]
    if ev:
        lines.append("<b>" + ", ".join(ev) + "</b>")
    if r.get("esc_active") is None:
        lines.append("shares position and speed only")
    return "<br>".join(lines)


def incident_text(h, label):
    """The evidence chain of a detected incident, for the hover."""
    provs = ", ".join(PROVIDER_LABEL.get(p, p) for p in str(h.get("provider_list") or "").split(",") if p)
    n_s = int(num(h["sensors"]))
    lines = [f"<b>{label.upper()}</b> {h['road'] or ''}",
             f"SRTI category: {h['srti_category']}" + (f" ({h['cause']})" if h.get("cause") else ""),
             f"first signal {str(h['first_signal'])[11:19]}, confirmed {str(h['confirmed_at'])[11:19]} "
             f"({int(num(h['seconds_to_confirm']))} s later)",
             f"evidence: {plural(h['signals'], 'signal')} from {plural(h['vehicles'], 'vehicle')}"
             + (f" of {plural(h['providers'], 'provider')} ({provs})" if provs else "")
             + (f" and {n_s} road sensor{'s' if n_s != 1 else ''}" if n_s else ""),
             f"confidence {num(h['confidence']):.0%}, warning reaches {int(num(h.get('vehicles_reached')))} vehicles"]
    return "<br>".join(lines)


def map_patch(traces, map_layers):
    """Only the slots after the free-flowing roads, plus the incident areas: a small update."""
    p = Patch()
    for i, k in enumerate(SLOTS):
        if k not in ("free", "nodata"):
            p["data"][i] = traces[k].to_plotly_json()
    p["layout"]["map"]["layers"] = map_layers
    return p


def table(rows, cols, max_rows=200):
    """cols: [(key, header, formatter or None)]"""
    head = html.Tr([html.Th(h) for _, h, _ in cols])
    body = []
    for r in rows[:max_rows]:
        cells = []
        for k, _, fmt in cols:
            v = r.get(k)
            cells.append(html.Td("" if v is None else (fmt(v) if fmt else str(v))))
        body.append(html.Tr(cells))
    return html.Div(html.Table([html.Thead(head), html.Tbody(body)], className="tbl"), className="tbl-wrap")


def yes(v):
    return "yes" if v in (True, "true", "TRUE", 1, "1") else ("" if v in (None, "") else "no")


SENSOR_KIND = {"road_weather": ("Road weather station", "#0d9488"), "camera": ("Camera", "#334155"),
               "loop_detector": ("Loop detector", "#64748b"), "warning_trailer": ("Road works warning trailer", "#b45309")}


def sensor_alarm(r):
    """What the newest reading reports as a problem, or ''."""
    if r.get("ts") is None:
        return ""
    if r.get("friction") is not None and num(r["friction"]) < 0.4 and num(r.get("surface_temp_c"), 9) <= 0:
        return "low friction on a frozen surface"
    if r.get("visibility_m") is not None and num(r["visibility_m"]) < 150:
        return "visibility below 150 m"
    if yes(r.get("wrong_way_detected")) == "yes":
        return "wrong-way vehicle detected"
    if yes(r.get("stationary_vehicle")) == "yes":
        return "stationary vehicle"
    if yes(r.get("works_active")) == "yes":
        return "road works set up"
    return ""


def sensor_text(r):
    kind = SENSOR_KIND.get(r["sensor_type"], (r["sensor_type"], ""))[0]
    bits = [f"<b>{r['sensor_name']}</b>", f"{kind} {r['sensor_id']}, road operator"]
    if r.get("ts") is None:
        bits.append("no reading in the last 3 minutes")
        return "<br>".join(bits)
    bits.append(f"newest reading {str(r['ts'])[11:19]}:")
    if r.get("surface_temp_c") is not None:
        bits.append(f"surface {num(r['surface_temp_c']):.1f} °C, friction {num(r['friction']):.2f}, "
                    f"visibility {int(num(r['visibility_m'])):,} m")
    if r["sensor_type"] == "camera":
        bits.append(f"wrong-way vehicle: {yes(r.get('wrong_way_detected')) or 'no'}, "
                    f"stationary vehicle: {yes(r.get('stationary_vehicle')) or 'no'}")
    if r.get("vehicle_count") is not None:
        bits.append(f"{int(num(r['vehicle_count']))} probe vehicles on the segment" +
                    (f", {int(num(r['avg_speed_kmh']))} km/h" if r.get("avg_speed_kmh") is not None else ""))
    alarm = sensor_alarm(r)
    bits.append(f"<b>ALERT: {alarm}</b>" if alarm else "normal")
    return "<br>".join(bits)


def flags(r):
    out = []
    for k, label in (("esc_active", "ESC"), ("abs_active", "ABS"), ("hard_braking", "hard braking"),
                     ("hazard_lights", "hazard lights"), ("airbag", "AIRBAG"), ("fog_lights", "fog lights")):
        if r.get(k) in (True, "true", "TRUE", 1):
            out.append(label)
    return ", ".join(out)


def chart_layout(fig, height, xtitle="", ytitle=""):
    fig.update_layout(height=height, margin=dict(l=8, r=12, t=8, b=36), plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
                      font=dict(size=12, color=INK), bargap=0.25, showlegend=False,
                      xaxis=dict(title=xtitle, gridcolor=GRID, zeroline=False),
                      yaxis=dict(title=ytitle, gridcolor=GRID, zeroline=False, rangemode="tozero"))
    return fig


def bar_h(labels, values, hover, height=220):
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker=dict(color=SERIES[0], line=dict(width=0)),
                           text=[f"{v:,.0f}" for v in values], textposition="outside", textfont=dict(color=MUTED),
                           hovertext=hover, hoverinfo="text"))
    chart_layout(fig, height)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    return fig




def telemetry_view(relayout, prev):
    """Center, zoom and visible box of the map, from the browser's last pan or zoom."""
    v = dict(prev or VIEW0)
    r = relayout or {}
    if isinstance(r.get("map.center"), dict):
        v["lat"], v["lon"] = num(r["map.center"].get("lat"), v["lat"]), num(r["map.center"].get("lon"), v["lon"])
    if r.get("map.zoom") is not None:
        v["zoom"] = num(r["map.zoom"], v["zoom"])
    derived = (r.get("map._derived") or {}).get("coordinates")
    if derived:
        lons, lats = [c[0] for c in derived], [c[1] for c in derived]
        v["box"] = [min(lats), max(lats), min(lons), max(lons)]
    elif "map.center" in r or "map.zoom" in r or "box" not in v:
        # no corners from the browser: estimate them for a map of about 1400 x 560 px (512 px tiles)
        deg_px = 360 / (512 * 2 ** v["zoom"])
        half_lon = 700 * deg_px
        half_lat = 280 * deg_px * math.cos(math.radians(v["lat"]))
        v["box"] = [v["lat"] - half_lat, v["lat"] + half_lat, v["lon"] - half_lon, v["lon"] + half_lon]
    return v


def box_params(v, pad=0.15):
    lat0, lat1, lon0, lon1 = v["box"]
    dl, dn = (lat1 - lat0) * pad, (lon1 - lon0) * pad
    return {"lat_min": lat0 - dl, "lat_max": lat1 + dl, "lon_min": lon0 - dn, "lon_max": lon1 + dn}


def kpi(label, value, note=""):
    return html.Div([html.Div(label, className="kpi-label"), html.Div(value, className="kpi-value"),
                     html.Div(note, className="kpi-note")], style=CARD)


def badge(text, color):
    return html.Span(text, className="badge", style={"background": color})


# ---------- the app ----------
def create_dash_app(server, url_base_pathname, metadata):
    app = Dash(__name__, server=server, routes_pathname_prefix="/",
               requests_pathname_prefix=url_base_pathname.rstrip("/") + "/", title=TITLE)
    app.index_string = INDEX_STRING

    legend = html.Div([html.Span([html.I(style={"background": STATE_STYLE[s][0]}), s]) for s in STATE_STYLE] + [
        html.Span([html.I(style={"background": NODATA_COLOR}), "no probe right now"]),
        html.Span([html.I(style={"background": ROUTE_COLORS[0]}), "diversion"]),
        *[html.Span([html.I(style={"background": c, "borderRadius": "50%"}), lbl.lower()]) for lbl, c in SENSOR_KIND.values()],
        html.Span([html.I(style={"background": "#dc2626", "borderRadius": "50%"}), "sensor alert"]),
        html.Span([html.I(style={"background": "#ea580c", "borderRadius": "50%", "height": "8px", "width": "8px"}),
                   "signal that caused an incident (colour of the incident)"]),
        html.Span([html.I(style={"background": "#d97706"}), "roadworks"]),
    ], className="legend")

    app.layout = html.Div([
        html.H1(TITLE),
        html.Div(SUBTITLE, className="sub"),
        html.Div(id="clock", className="clock"),
        dcc.Tabs(id="tabs", value="map", className="tabs-row", children=[
            dcc.Tab(label="Incident map", value="map"),
            dcc.Tab(label="Signal stream", value="stream"),
        ]),
        html.Div(id="tab-map", children=[
        html.Div(id="kpis", className="kpis"),
        html.Div([
            html.Div([dcc.Graph(id="map", config={"displayModeBar": False, "scrollZoom": True}),
                      dcc.Store(id="map-view", data=VIEW0), legend],
                     style={**CARD, "padding": "6px"}),
            html.Div([
                html.Div("Incidents", className="section-title"),
                html.Div(id="incidents"),
            ], className="side", style=CARD),
        ], className="grid"),
        html.Div(html.Div([
            html.Div([html.Div("Layers", className="section-title"),
                      dcc.Checklist(id="layers", className="checks",
                                    value=["traffic", "incidents", "sensors", "probes"],
                                    options=[{"label": lbl, "value": k} for k, lbl in LAYERS])]),
            html.Div([html.Div("Refresh", className="section-title"),
                      dcc.Checklist(id="live", className="checks", value=["live"],
                                    options=[{"label": f"Live (every {REFRESH_MS // 1000} s)", "value": "live"}]),
                      html.Div("Traffic state from the probe speeds of the last 5 minutes against the free-flow speed: "
                               "slow below 70 %, congested below 40 %, standstill below 15 %. Every road class, for the "
                               "visible area only: zoomed out, free-flowing main roads and anything slow on the bigger "
                               "roads; zoomed in, every street, and in grey the streets without a probe right now.",
                               className="muted",
                               style={"marginTop": "8px"})]),
        ], className="controls"), style={**CARD, "marginTop": "14px"}),
        html.Div(id="sql"),
        ]),
        html.Div(id="tab-stream", style={"display": "none"}, children=[
            html.Div("Signal stream", className="part-title"),
            html.Div("Every signal the connected vehicles and road sensors send, newest first. Each one is classed against "
                     "the incidents the detection SQL has confirmed: caused an incident (it is evidence of one), background "
                     "noise (an event such as a single hard braking that nothing confirms), or regular (position, speed and "
                     "environment). A signal that turns out to be evidence changes class as soon as the incident is confirmed.",
                     className="part-sub"),
            html.Div([
                html.Div([html.Div("Class", className="section-title"),
                          dcc.Checklist(id="stream-class", className="checks", value=["evidence", "noise", "regular"],
                                        options=[{"label": CLASS_LABEL[k], "value": k} for k in ("evidence", "noise", "regular")])]),
                html.Div([html.Div("Source", className="section-title"),
                          dcc.Checklist(id="stream-source", className="checks", value=["vehicle", "sensor"],
                                        options=[{"label": "vehicles", "value": "vehicle"}, {"label": "road sensors", "value": "sensor"}])]),
                html.Div([html.Div("Time window", className="section-title"),
                          dcc.RadioItems(id="stream-window", className="checks", value=60,
                                         options=[{"label": "last 30 s", "value": 30}, {"label": "last minute", "value": 60},
                                                  {"label": "last 5 minutes", "value": 300}, {"label": "last 15 minutes", "value": 900}])]),
                html.Div(id="stream-status", className="muted"),
            ], className="controls", style={**CARD, "marginTop": "8px"}),
            html.Div(dash_table.DataTable(
                id="stream-table2", columns=[{"name": n, "id": k} for k, n in STREAM_COLS], data=[],
                filter_action="native", sort_action="native", page_action="native", page_size=40,
                style_table={"overflowX": "auto"},
                style_cell={"fontFamily": "inherit", "fontSize": "12px", "padding": "3px 6px", "textAlign": "left",
                            "whiteSpace": "nowrap", "border": "none", "borderBottom": f"1px solid {LINE}"},
                style_header={"fontWeight": 600, "color": MUTED, "background": "#fff", "borderBottom": f"1px solid {LINE}"},
                style_filter={"background": "#f9fafb"},
                style_data_conditional=[
                    {"if": {"filter_query": '{class} = "caused an incident"'}, "backgroundColor": "#fee2e2", "color": "#991b1b",
                     "fontWeight": 600},
                    {"if": {"filter_query": '{class} = "background noise"'}, "backgroundColor": "#fef9c3", "color": "#854d0e"},
                    {"if": {"filter_query": '{class} = "regular"'}, "color": "#6b7280"},
                ]), style={**CARD, "marginTop": "14px", "padding": "6px"}),
            html.Div("Filter any column by typing in the row under the header, e.g. A 9 under road, >80 under km/h, "
                     "OEM B under provider or hard under events.", className="muted", style={"marginTop": "6px"}),
            html.Div(id="stream-sql"),
        ]),
        html.Div(CAVEAT, className="caveat"),
        html.Div(ATTRIBUTION, className="attrib"),
        dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),
    ], className="page")

    @app.callback(Output("tick", "disabled"), Input("live", "value"))
    def toggle_live(live):
        return "live" not in (live or [])

    @app.callback(Output("tab-map", "style"), Output("tab-stream", "style"), Input("tabs", "value"))
    def show_tab(tab):
        return tuple({"display": "block" if tab == k else "none"} for k in ("map", "stream"))

    @app.callback(Output("stream-table2", "data"), Output("stream-status", "children"), Output("stream-sql", "children"),
                  Input("tick", "n_intervals"), Input("tabs", "value"), Input("stream-class", "value"),
                  Input("stream-source", "value"), Input("stream-window", "value"))
    def refresh_stream(_n, tab, classes, sources, window):
        if tab != "stream":
            raise PreventUpdate
        classes, sources = set(classes or []), set(sources or [])
        log = []
        try:
            _, evidence = detection(server, metadata, log)
            ids = sorted({str(int(num(e["vehicle_id"]))) for e in evidence if e["source"] == "vehicle"})
            rows = run_sql(server, metadata, "signal_stream", {
                "seconds": int(window or 60), "max_rows": 1000 if "regular" in classes else 3000, "vehicles": int("vehicle" in sources),
                "sensors": int("sensor" in sources), "only_events": int("regular" not in classes),
                "evidence_ids": ",".join(f"'{i}'" for i in ids) or "'-'"}, log)
        except RuntimeError as e:
            return [], html.Div(str(e), className="err"), sql_foldout(log)
        rows = [r for r in classify(rows, evidence) if r["class"] in classes]
        n = {k: sum(1 for r in rows if r["class"] == k) for k in CLASS_LABEL}
        cap = 1000 if "regular" in classes else 3000
        status = (f"{len(rows):,} signals" + (f" (the newest {cap:,})" if len(rows) >= cap else "") + ": "
                  f"{n['evidence']:,} caused an incident, {n['noise']:,} background noise, {n['regular']:,} regular; "
                  f"refreshed every {REFRESH_MS // 1000} s")
        return [stream_cells(r) for r in rows], status, sql_foldout(log)

    @app.callback(Output("clock", "children"), Input("tick", "n_intervals"))
    def clock(_n):
        try:
            sim = run_sql(server, metadata, "sim", {}, [])
        except RuntimeError as e:
            return html.Div(str(e), className="err")
        if not sim:
            return html.Div("No data yet.", className="err")
        now = ts(sim[0]["sim_now"])
        return f"Data as of {now:%H:%M:%S}, {now:%a %d %b %Y}"

    @app.callback(Output("map-view", "data"), Input("map", "relayoutData"), State("map-view", "data"))
    def keep_map_view(relayout, view):
        if not any(k.startswith(("map.center", "map.zoom")) for k in (relayout or {})):
            raise PreventUpdate
        return telemetry_view(relayout, view)

    @app.callback(
        Output("kpis", "children"), Output("map", "figure"),
        Output("incidents", "children"), Output("sql", "children"),
        Input("tick", "n_intervals"), Input("layers", "value"), Input("tabs", "value"),
        Input("map-view", "data"),
    )
    def refresh(_n, layers, tab, view):
        if tab != "map" and ctx.triggered_id in ("tick", "tabs", "map-view"):
            raise PreventUpdate                      # the map tab is hidden: don't query for it
        view = telemetry_view(None, view)            # adds the visible box if it's missing
        only_view = ctx.triggered_id == "map-view" and "congestion" in _LAST
        log = []
        layers = set(layers or [])
        try:
            sim = run_sql(server, metadata, "sim", {}, log)
            if not sim:
                raise RuntimeError("No data yet.")
            sim = sim[0]
            now = ts(sim["sim_now"])
            traffic = run_sql(server, metadata, "traffic_view", {**TRAFFIC, **box_params(view, pad=0.1)}, log)
            if only_view:                            # panned or zoomed: the rest is still current
                congestion, hazards, diversions, kpis, evidence = (
                    _LAST[k] for k in ("congestion", "hazards", "diversions", "kpis", "evidence"))
            else:
                congestion = run_sql(server, metadata, "congestion", {**TRAFFIC, "merge_m": 1500}, log)   # queues behind incidents
                hazards, evidence = detection(server, metadata, log)
                diversions = run_sql(server, metadata, "diversions", TRAFFIC, log) if any(
                    h["hazard_type"] in ("accident_area", "unmanaged_blockage") for h in hazards) else []
                kpis = run_sql(server, metadata, "kpis", {}, log)
                _LAST.update(congestion=congestion, hazards=hazards, diversions=diversions, kpis=kpis, evidence=evidence)
            vehicles = run_sql(server, metadata, "vehicles", box_params(view, pad=0.05), log) if "probes" in layers else []
            sensors = run_sql(server, metadata, "sensor_latest", {}, log) if "sensors" in layers else []
            st = static(server, metadata, log)
            route_ids = {int(r["segment_id"]) for r in diversions}
            geom = geometry(server, metadata, sorted(route_ids), log)
        except RuntimeError as e:
            empty = go.Figure().update_layout(map=dict(style="carto-positron", center=dict(lat=48.15, lon=11.59), zoom=10),
                                              margin=dict(l=0, r=0, t=0, b=0), height=680, uirevision="keep")
            return [html.Div(str(e), className="err")], empty, [], sql_foldout(log)

        # routes per accident segment: {acc: {route_no: (kind, rows)}}
        routes = {}
        for r in diversions:
            acc, no = int(r["acc_seg"]), int(r["route_no"])
            routes.setdefault(acc, {}).setdefault(no, (r["route_kind"], []))[1].append(r)

        # KPIs
        incidents = [h for h in hazards if h["hazard_type"] in INCIDENT]
        signals_60 = sum(num(r["signals"]) for r in kpis)
        vehicles_60 = sum(num(r["vehicles"]) for r in kpis)
        kpi_row = [
            kpi("Incidents", f"{len(incidents)}", "detected from vehicle and sensor signals"),
            kpi("Vehicles warned", f"{int(sum(num(h['vehicles_reached']) for h in incidents)):,}",
                "heading toward an incident, within 2 km"),
            kpi("Probe vehicles", f"{int(vehicles_60):,}", f"{signals_60 / 60:,.0f} signals per second"),
        ]

        # incident cards with diversions
        inc_cards = []
        for h in sorted(incidents, key=lambda h: str(h["confirmed_at"])):
            label, color = INCIDENT[h["hazard_type"]]
            lat, lon = num(h["lat"]), num(h["lon"])
            queue = [c for c in congestion if c["reason"] in ("accident", "slippery road")
                     and dist_m(lat, lon, num(c["lat"]), num(c["lon"])) < 6000]
            q_km = sum(num(c["length_km"]) for c in queue)
            q_delay = max((num(c["delay_min"]) for c in queue), default=0)
            body = [html.Div([label, f" - {h['road'] or ''}"], className="inc-title", style={"color": color}),
                    html.Div(f"Detected {str(h['confirmed_at'])[11:19]}, {int(num(h['seconds_to_confirm']))} s after the "
                             f"first signal, from {plural(h['vehicles'], 'vehicle')} of {plural(h['providers'], 'provider')}"
                             + (f" and {plural(h['sensors'], 'road sensor')}" if int(num(h['sensors'])) else "")
                             + (f" - cause: {h['cause']}" if h.get('cause') else ""), className="inc-meta"),
                    html.Div(f"Queue behind it: {q_km:.1f} km, +{q_delay:.0f} min" if queue else "No queue yet",
                             className="inc-meta"),
                    html.Div(f"Warning reaches {int(num(h['vehicles_reached']))} vehicles within 2 km", className="inc-meta"),
                    trigger_box(h, evidence)]
            if h["hazard_type"] in ("accident_area", "unmanaged_blockage") and routes:
                def acc_dist(a):
                    pts = _GEOM.get(a)
                    return dist_m(lat, lon, pts[0][1], pts[0][0]) if pts else 1e9
                rr = routes[min(routes, key=acc_dist)]
                through = next((v for k, v in rr.items() if v[0] == "through"), None)
                divs = [(no, v) for no, v in sorted(rr.items()) if v[0] == "diversion"]
                if divs and through:
                    t_through = num(through[1][0]["total_s"]) / 60
                    lines = []
                    for no, (_, rows) in divs:
                        t_div = num(rows[0]["total_s"]) / 60
                        diff = t_through - t_div
                        verdict = (f"saves {diff:.0f} min" if diff >= 1 else
                                   "about as fast as the queue" if diff > -1 else f"{-diff:.0f} min slower than the queue, for now")
                        lines.append(html.Div([html.B(f"Diversion {no}: "), " > ".join(route_roads(rows)[:6]),
                                               f" - {t_div:.0f} min, {num(rows[0]['total_km']):.1f} km ({verdict})"]))
                    lines.append(html.Div(f"Through the queue: {t_through:.0f} min, "
                                          f"{num(through[1][0]['total_km']):.1f} km on {' > '.join(route_roads(through[1])[:3])}",
                                          className="inc-meta"))
                    body.append(html.Div(lines, className="div-box"))
                elif through:
                    body.append(html.Div("No diversion found on the road network.", className="div-box"))
            inc_cards.append(html.Div(body, className="inc", style={"borderColor": color}))
        if not inc_cards:
            inc_cards = [html.Div("No incidents.", className="muted")]

        data = {"geom": geom, "traffic": traffic, "hazards": hazards, "sensors": sensors, "evidence": evidence,
                "routes": routes, "vehicles": vehicles, "static": st}
        # the whole figure on the first load, on layer or tab changes and every FREE_EVERY refreshes; else a patch
        full = ctx.triggered_id != "tick" or (_n or 0) % FREE_EVERY == 0
        map_out = map_figure(data, layers, view=view) if full else map_patch(*map_figure(data, layers, with_free=False, view=view))
        return (kpi_row, map_out, inc_cards, sql_foldout(log))

    return app
