"""Munich Road Conditions: safety-related traffic information confirmed from several providers' SRTI events and road sensors."""
import importlib.util
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
from dash import Dash, Input, Output, Patch, ctx, dash_table, dcc, html
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
SUBTITLE = ("Safety-related traffic information, confirmed from the SRTI events of several carmakers and the road "
            "operator's sensors, as in Data for Road Safety")
CAVEAT = ("The carmakers, the navigation service and the road operator are simulated: they send SRTI events and sensor "
          "readings through Kafka, never raw vehicle data. Exasol loads, combines and publishes them with SQL. Traffic "
          "volume is modelled from open data (BASt hourly counts, known hotspots), not measured: nobody shares vehicle "
          "positions. The road network, roadworks, accident history and weather are open data.")
ATTRIBUTION = ("Road network (c) OpenStreetMap contributors (ODbL). Traffic counts: Bundesanstalt fuer Strassenwesen "
               "(CC BY 4.0). Roadworks: City of Munich (dl-de/by-2.0), (c) Die Autobahn GmbH des Bundes. Accidents: "
               "Unfallatlas (dl-de/by-2.0). Weather: Deutscher Wetterdienst (CC BY 4.0).")
REFRESH_MS = 5000
FULL_EVERY = 12               # resend the traffic volume roads (the biggest layer) every 12th refresh = 60 s
VIEW0 = {"lat": 48.150, "lon": 11.590, "zoom": 10.0}
DEFAULT_RULE = {"min_providers": 2, "sensor_replaces": 1}
COMBINE = {"window_min": 30, "heading_max": 60, "friction_max": 0.4, "visibility_max": 150,
           "blockage_after_s": 600, "reach_min": 10}

INK = "#1f2937"
MUTED = "#6b7280"
LINE = "#e5e7eb"
CARD = {"background": "#ffffff", "border": f"1px solid {LINE}", "borderRadius": "10px", "padding": "14px 16px"}
CATEGORY = {  # SRTI category -> (label, colour)
    "unprotected_accident_area": ("Accident", "#dc2626"), "unmanaged_blockage": ("Accident: road blocked", "#7f1d1d"),
    "temporary_slippery_road": ("Slippery road", "#ea580c"), "obstacle_on_road": ("Obstacle on the road", "#c2410c"),
    "wrong_way_driver": ("Wrong-way driver", "#c026d3"), "reduced_visibility": ("Reduced visibility", "#0d9488"),
    "exceptional_weather": ("Exceptional weather", "#7c3aed"), "short_term_road_works": ("Short-term road works", "#b45309"),
}
# one fixed colour per provider: categorical slots 1-7 of the validated reference palette, in fixed order
PROVIDER_COLOR = {"OEM_A": "#2a78d6", "OEM_B": "#eb6834", "OEM_C": "#1baf7a", "OEM_D": "#eda100",
                  "OEM_E": "#e87ba4", "OEM_F": "#008300", "NAV": "#6250d6", "ROAD_OP": "#334155"}
PROVIDER_LABEL = {"OEM_A": "OEM A", "OEM_B": "OEM B", "OEM_C": "OEM C", "OEM_D": "OEM D", "OEM_E": "OEM E",
                  "OEM_F": "OEM F", "NAV": "Navigation", "ROAD_OP": "Road operator", "SERVICE": "This service"}
SINGLE_COLOR = "#9ca3af"
VOLUME = {  # load class -> (lower bound of load, colour, width, label)
    "quiet": (0.0, "#d6dae0", 1.5, "quiet"), "busy": (0.55, "#f2c36b", 2.5, "busy"), "heavy": (0.85, "#e8915a", 3.5, "very busy"),
}
LAYERS = [("events", "Reports from providers"), ("incidents", "Confirmed incidents"), ("sensors", "Road sensors"),
          ("volume", "Traffic volume (modelled)"), ("roadworks", "Roadworks (long-term, real)"),
          ("accidents", "Accident history (2016-2025)")]
SENSOR_KIND = {"road_weather": ("Road weather station", "#0d9488"), "camera": ("Camera", "#334155"),
               "loop_detector": ("Loop detector", "#64748b"), "warning_trailer": ("Road works warning trailer", "#b45309")}
STATUS_COLOR = {"new": "#dc2626", "update": "#2563eb", "cleared": "#16a34a"}

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
.grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }
@media (max-width: 1000px) { .grid, .grid2, .kpis { grid-template-columns: 1fr; } .page { padding: 14px; } }
.side { max-height: 700px; overflow-y: auto; }
.section-title { font-weight: 650; font-size: 14px; margin: 4px 0 6px; }
.inc { border: 1px solid #fecaca; background: #fff7f7; border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
.inc-title { font-weight: 700; font-size: 15px; }
.inc-meta { font-size: 12px; color: #4b5563; margin-top: 3px; }
.inc-pub { font-size: 12px; margin-top: 6px; padding: 4px 8px; border-radius: 6px; background: #ecfdf5; color: #065f46; }
.inc-nopub { font-size: 12px; margin-top: 6px; padding: 4px 8px; border-radius: 6px; background: #f3f4f6; color: #374151; }
.trg-box { margin-top: 8px; padding: 6px 10px; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; font-size: 12px; }
.trg-box summary { cursor: pointer; font-weight: 600; color: #991b1b; }
.trg-row { margin-top: 2px; color: #374151; font-variant-numeric: tabular-nums; }
.trg-time { color: #6b7280; }
.badge { font-size: 11px; font-weight: 600; color: #fff; border-radius: 10px; padding: 1px 7px; white-space: nowrap; margin-right: 4px; }
.checks label, .checks .dash-options-list-option { display: flex !important; flex-direction: row !important;
  align-items: flex-start; gap: 6px; margin: 3px 0; font-size: 13px; }
.checks .dash-options-list-option-wrapper, .checks input { flex: none; display: inline-flex; margin-top: 2px; }
.checks .dash-options-list-option-text { display: inline; }
.controls { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
@media (max-width: 1000px) { .controls { grid-template-columns: 1fr; } }
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
.part-title { font-size: 18px; font-weight: 700; margin: 22px 0 2px; }
.part-sub { font-size: 13px; color: #6b7280; }
.tbl-wrap { max-height: 320px; overflow: auto; }
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


def hms(v):
    t = ts(v)
    return f"{t:%H:%M:%S}" if t else ""


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def plural(n, word):
    n = int(num(n))
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def yes(v):
    return v in (True, "true", "TRUE", 1, "1")


def dist_m(lat1, lon1, lat2, lon2):
    return math.hypot((lat1 - lat2) * 110540, (lon1 - lon2) * 74320)


_NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


def wkt_coords(wkt):
    out = []
    for part in re.findall(r"\(([^()]+)\)", wkt or ""):
        pts = re.findall(rf"({_NUM})\s+({_NUM})", part)
        if pts:
            out.append([(float(x), float(y)) for x, y in pts])
    return out


_GEOM = {}
_STATIC = {}
_VOLUME = {"hour": None, "rows": []}
_CACHE = {"t": 0.0, "key": None, "data": None, "log": []}
_LOCK = threading.Lock()
CACHE_TTL_S = 4.0            # all open pages share one set of query results for this long


def geometry(server, metadata, log):
    """Geometry of the main roads, once per process."""
    if not _GEOM:
        for r in run_sql(server, metadata, "volume_segments", {}, log):
            pts = wkt_coords(r["geometry_wkt"])
            if pts:
                p = pts[0]
                _GEOM[int(r["segment_id"])] = [p[0], p[len(p) // 2], p[-1]] if len(p) > 3 else p
    return _GEOM


def static(server, metadata, log):
    if "roadworks" not in _STATIC:
        _STATIC["roadworks"] = run_sql(server, metadata, "roadworks_now", {}, log)
        _STATIC["accidents"] = run_sql(server, metadata, "hotspots", {"min_accidents": 15}, log)
    return _STATIC


def volume(server, metadata, hour, log):
    """Traffic volume per main road at this hour: changes once an hour."""
    if _VOLUME["hour"] != hour:
        _VOLUME.update(hour=hour, rows=run_sql(server, metadata, "traffic_volume", {}, log))
    return _VOLUME["rows"]


def fetch(server, metadata, rule, layers):
    """Everything a refresh needs, shared by all open pages for CACHE_TTL_S."""
    key = (rule["min_providers"], rule["sensor_replaces"], "sensors" in layers)
    with _LOCK:
        if _CACHE["key"] == key and time.time() - _CACHE["t"] < CACHE_TTL_S:
            return _CACHE["data"], list(_CACHE["log"])
        log = []
        sim = run_sql(server, metadata, "sim", {}, log)
        if not sim:
            raise RuntimeError("No data yet: start the simulator and the road safety service.")
        now = ts(sim[0]["sim_now"])
        data = {
            "sim": sim[0], "now": now,
            "live": run_sql(server, metadata, "incidents_live", {**COMBINE, **rule}, log),
            "events": run_sql(server, metadata, "events_now", {"window_min": COMBINE["window_min"]}, log),
            "published": run_sql(server, metadata, "published", {}, log),
            "outbound": run_sql(server, metadata, "outbound", {"max_rows": 14}, log),
            "kpis": run_sql(server, metadata, "kpis", {}, log),
            "sensors": run_sql(server, metadata, "sensor_latest", {}, log) if "sensors" in layers else [],
            "offers": run_sql(server, metadata, "offers", {}, log),
            "volume": volume(server, metadata, now.hour, log),
            "static": static(server, metadata, log),
            "geom": geometry(server, metadata, log),
        }
        _CACHE.update(t=time.time(), key=key, data=data, log=log)
        return data, list(log)


def lines_trace(segs, geom, color, width, name):
    lat, lon = [], []
    for sid in segs:
        pts = geom.get(sid)
        if pts:
            lon += [p[0] for p in pts] + [None]
            lat += [p[1] for p in pts] + [None]
    return go.Scattermap(lat=lat, lon=lon, mode="lines", line=dict(width=width, color=color), name=name, hoverinfo="skip")


def sensor_alarm(r):
    """What the newest reading reports as a problem, or ''."""
    if r.get("ts") is None:
        return ""
    if r.get("friction") is not None and num(r["friction"]) < 0.4 and num(r.get("surface_temp_c"), 9) <= 0:
        return "low friction on a frozen surface"
    if r.get("visibility_m") is not None and num(r["visibility_m"]) < 150:
        return "visibility below 150 m"
    if yes(r.get("wrong_way_detected")):
        return "wrong-way vehicle detected"
    if yes(r.get("stationary_vehicle")):
        return "stationary vehicle"
    if yes(r.get("works_active")):
        return "road works set up"
    return ""


def sensor_text(r):
    kind = SENSOR_KIND.get(r["sensor_type"], (r["sensor_type"], ""))[0]
    bits = [f"<b>{r['sensor_name']}</b>", f"{kind} {r['sensor_id']}, road operator"]
    if r.get("ts") is None:
        bits.append("no reading in the last 3 minutes")
        return "<br>".join(bits)
    bits.append(f"newest reading {hms(r['ts'])}:")
    if r.get("surface_temp_c") is not None:
        bits.append(f"surface {num(r['surface_temp_c']):.1f} °C, friction {num(r['friction']):.2f}, "
                    f"visibility {int(num(r['visibility_m'])):,} m")
    if r.get("vehicle_count") is not None:
        bits.append(f"{int(num(r['vehicle_count']))} vehicles on the segment (modelled)" +
                    (f", {int(num(r['avg_speed_kmh']))} km/h" if r.get("avg_speed_kmh") is not None else ""))
    alarm = sensor_alarm(r)
    bits.append(f"<b>ALERT: {alarm}</b>" if alarm else "normal")
    return "<br>".join(bits)


def cat_label(c):
    return CATEGORY.get(c, (str(c).replace("_", " "), "#dc2626"))[0]


def event_text(e, confirmed):
    prov = PROVIDER_LABEL.get(e["provider"], e["provider"])
    lines = [f"<b>{cat_label(e['srti_category'])}</b>" + (f" ({e['cause']})" if e.get("cause") else "")
             + f" reported by {prov}",
             f"{e['road'] or 'road not matched'}, first {hms(e['first_at'])}, latest {hms(e['detected_at'])} "
             f"(version {int(num(e['version']))})",
             f"confidence {num(e['confidence']):.2f}, seen by {plural(e['observations'], 'vehicle')} of its fleet"
             + (f", sensor {e['sensor_id']}" if e.get("sensor_id") else ""),
             f"valid until {hms(e['valid_until'])}; topic {e['topic']}",
             "<b>part of a confirmed incident</b>" if confirmed else "<i>single report: nobody else reports it (yet)</i>"]
    return "<br>".join(lines)


def reach_text(h, minutes=COMBINE["reach_min"]):
    if h.get("reach_vehicles") is None:
        return "reach unknown"
    n = int(num(h["reach_vehicles"]))
    return f"about {n:,} vehicles in the area" if h.get("reach_kind") == "in_area" else \
        f"about {n:,} vehicles pass it in the next {minutes} minutes"


def match_published(h, published):
    """The published incident (INCIDENTS) that a live combination row corresponds to."""
    base = h["base_category"]
    best = None
    for p in published:
        if p["status"] != "active" or p["base_category"] != base:
            continue
        d = dist_m(num(h["lat"]), num(h["lon"]), num(p["lat"]), num(p["lon"]))
        if d <= num(h["radius_m"], 300) and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def incident_text(h, pub):
    provs = ", ".join(PROVIDER_LABEL.get(p, p) for p in str(h.get("provider_list") or "").split(",") if p)
    lines = [f"<b>{cat_label(h['srti_category']).upper()}</b> {h['road'] or ''}",
             f"SRTI category {h['srti_category']}" + (f", cause {h['cause']}" if h.get("cause") else ""),
             f"first report {hms(h['first_report_at'])}; confirmed by " + rule_text(h),
             f"evidence: {plural(h['reports'], 'report')}" + (f" from {provs}" if provs else "")
             + (f"; sensors {h['sensor_list']}" if h.get("sensor_list") else ""),
             f"confidence {num(h['confidence']):.0%}; {reach_text(h)}",
             f"published as {pub['incident_id']} (version {int(num(pub['version']))})" if pub else "not published by the service"]
    return "<br>".join(lines)


def rule_text(h):
    p = int(num(h["providers"]))
    return {"providers": plural(p, "provider"), "providers_and_sensor": f"{plural(p, 'provider')} and a road operator sensor",
            "road_operator": "the road operator's sensor"}.get(h.get("rule"), "-")


# Fixed trace slots, always in this order (empty when a layer is off or has no data), so that a refresh can
# send only the slots that change: the traffic volume roads are by far the biggest and change once an hour.
SLOTS = ["vol:quiet", "vol:busy", "vol:heavy", "accidents", "roadworks", "sensors", "sensor_alarm", "singles"] + \
        [f"prov:{p}" for p in PROVIDER_COLOR] + [f"inc:{c}" for c in CATEGORY]
VOLUME_SLOTS = {"vol:quiet", "vol:busy", "vol:heavy"}


def map_traces(d, layers, members):
    geom = d["geom"]
    tr = {}
    by_class = {k: [] for k in VOLUME}
    if "volume" in layers:
        for r in d["volume"]:
            load = num(r["load"])
            k = "heavy" if load >= VOLUME["heavy"][0] else "busy" if load >= VOLUME["busy"][0] else "quiet"
            by_class[k].append(int(r["segment_id"]))
    for k, (_, color, width, label) in VOLUME.items():
        tr[f"vol:{k}"] = lines_trace(by_class[k], geom, color, width, f"Traffic volume: {label}")
    rows = d["static"]["accidents"] if "accidents" in layers else []
    tr["accidents"] = go.Scattermap(lat=[num(r["mid_lat"]) for r in rows], lon=[num(r["mid_lon"]) for r in rows],
                                    mode="markers", name="Accident history (2016-2025)",
                                    marker=dict(size=[5 + min(num(r["accidents"]) / 6, 12) for r in rows],
                                                color="#991b1b", opacity=0.25),
                                    text=[f"{r['road'] or ''}: {int(num(r['accidents']))} injury accidents 2016-2025"
                                          for r in rows], hoverinfo="text")
    rows, seen = [], set()
    for r in (d["static"]["roadworks"] if "roadworks" in layers else []):
        if r["roadworks"] not in seen:
            seen.add(r["roadworks"])
            rows.append(r)
    tr["roadworks"] = go.Scattermap(lat=[num(r["mid_lat"]) for r in rows], lon=[num(r["mid_lon"]) for r in rows],
                                    mode="markers", name="Roadworks (real)", marker=dict(size=7, color="#d97706", opacity=0.7),
                                    text=[f"{r['roadworks']} (capacity {num(r['capacity_factor']):.0%})" for r in rows],
                                    hoverinfo="text")
    sens = d["sensors"] if "sensors" in layers else []
    normal = [r for r in sens if not sensor_alarm(r)]
    alarm = [r for r in sens if sensor_alarm(r)]
    tr["sensors"] = go.Scattermap(
        lat=[num(r["lat"]) for r in normal], lon=[num(r["lon"]) for r in normal], mode="markers", name="Road sensors",
        marker=dict(size=8, color=[SENSOR_KIND.get(r["sensor_type"], ("", "#111827"))[1] for r in normal], opacity=0.8),
        text=[sensor_text(r) for r in normal], hoverinfo="text")
    tr["sensor_alarm"] = go.Scattermap(
        lat=[num(r["lat"]) for r in alarm], lon=[num(r["lon"]) for r in alarm], mode="markers", name="Sensor alerts",
        marker=dict(size=14, color="#334155", opacity=0.95), text=[sensor_text(r) for r in alarm], hoverinfo="text")
    evs = [e for e in d["events"] if e["provider"] != "ROAD_OP"] if "events" in layers else []
    single = [e for e in evs if e["event_id"] not in members]
    tr["singles"] = go.Scattermap(
        lat=[num(e["lat"]) for e in single], lon=[num(e["lon"]) for e in single], mode="markers",
        name="Single reports (not confirmed)", marker=dict(size=10, color=SINGLE_COLOR, opacity=0.85),
        text=[event_text(e, False) for e in single], hoverinfo="text")
    for p, color in PROVIDER_COLOR.items():
        es = [e for e in evs if e["event_id"] in members and e["provider"] == p]
        tr[f"prov:{p}"] = go.Scattermap(
            lat=[num(e["lat"]) for e in es], lon=[num(e["lon"]) for e in es], mode="markers",
            name=f"Reports of {PROVIDER_LABEL[p]}", marker=dict(size=11, color=color, opacity=0.95),
            text=[event_text(e, True) for e in es], hoverinfo="text")
    inc = [h for h in d["live"] if yes(h["confirmed"])] if "incidents" in layers else []
    for c, (label, color) in CATEGORY.items():
        hs = [h for h in inc if h["srti_category"] == c]
        tr[f"inc:{c}"] = go.Scattermap(
            lat=[num(h["lat"]) for h in hs], lon=[num(h["lon"]) for h in hs], mode="markers+text", name=label,
            marker=dict(size=24, color=color, opacity=0.9), text=[label.upper() for _ in hs], textposition="top right",
            textfont=dict(color=color, size=13),
            hovertext=[incident_text(h, match_published(h, d["published"])) for h in hs], hoverinfo="text")
    return tr


def map_figure(traces):
    fig = go.Figure([traces[k] for k in SLOTS])
    fig.update_layout(map=dict(style="carto-positron", center=dict(lat=VIEW0["lat"], lon=VIEW0["lon"]), zoom=VIEW0["zoom"]),
                      margin=dict(l=0, r=0, t=0, b=0), height=680, uirevision="keep", showlegend=False,
                      paper_bgcolor="#ffffff")
    return fig


def map_patch(traces):
    p = Patch()
    for i, k in enumerate(SLOTS):
        if k not in VOLUME_SLOTS:
            p["data"][i] = traces[k].to_plotly_json()
    return p


def table(rows, cols, max_rows=200):
    """cols: [(key, header, formatter or None)]"""
    head = html.Tr([html.Th(h) for _, h, _ in cols])
    body = []
    for r in rows[:max_rows]:
        cells = []
        for k, _, fmt in cols:
            v = r.get(k)
            cells.append(html.Td("" if v is None else (fmt(v, r) if fmt else str(v))))
        body.append(html.Tr(cells))
    return html.Div(html.Table([html.Thead(head), html.Tbody(body)], className="tbl"), className="tbl-wrap")


def kpi(label, value, note=""):
    return html.Div([html.Div(label, className="kpi-label"), html.Div(value, className="kpi-value"),
                     html.Div(note, className="kpi-note")], style=CARD)


def badge(text, color):
    return html.Span(text, className="badge", style={"background": color})


def provider_badges(lst):
    return [badge(PROVIDER_LABEL.get(p, p), PROVIDER_COLOR.get(p, MUTED)) for p in str(lst or "").split(",") if p]


def evidence_box(h, events_by_id):
    rows = []
    for m in str(h.get("members") or "").split(","):
        if not m:
            continue
        if m.startswith("sensor:"):
            _, sid, _cat = m.split(":", 2)
            rows.append(html.Div([badge("Road operator", PROVIDER_COLOR["ROAD_OP"]), f"sensor {sid} alerts"], className="trg-row"))
            continue
        e = events_by_id.get(m)
        if not e:
            continue
        rows.append(html.Div([html.Span(hms(e["first_at"]), className="trg-time"), " ",
                              badge(PROVIDER_LABEL.get(e["provider"], e["provider"]), PROVIDER_COLOR.get(e["provider"], MUTED)),
                              f"event {e['event_id'].split('-')[-1]}, version {int(num(e['version']))}, "
                              f"confidence {num(e['confidence']):.2f}, seen by {plural(e['observations'], 'vehicle')}"
                              + (f", sensor {e['sensor_id']}" if e.get("sensor_id") else "")], className="trg-row"))
    return html.Details([html.Summary(f"Evidence: {plural(len(rows), 'report')}"), html.Div(rows)], className="trg-box")


# ---------- the app ----------
def create_dash_app(server, url_base_pathname, metadata):
    app = Dash(__name__, server=server, routes_pathname_prefix="/",
               requests_pathname_prefix=url_base_pathname.rstrip("/") + "/", title=TITLE)
    app.index_string = INDEX_STRING

    legend = html.Div(
        [html.Span([html.I(style={"background": PROVIDER_COLOR[p], "borderRadius": "50%", "height": "9px", "width": "9px"}),
                    PROVIDER_LABEL[p]]) for p in PROVIDER_COLOR if p != "ROAD_OP"]
        + [html.Span([html.I(style={"background": SINGLE_COLOR, "borderRadius": "50%", "height": "9px", "width": "9px"}),
                      "single report, not confirmed"]),
           *[html.Span([html.I(style={"background": c, "borderRadius": "50%"}), lbl.lower()]) for lbl, c in SENSOR_KIND.values()],
           *[html.Span([html.I(style={"background": v[1]}), f"traffic: {v[3]}"]) for v in VOLUME.values()],
           html.Span([html.I(style={"background": "#d97706"}), "roadworks"])],
        className="legend")

    app.layout = html.Div([
        html.H1(TITLE),
        html.Div(SUBTITLE, className="sub"),
        html.Div(id="clock", className="clock"),
        dcc.Tabs(id="tabs", value="map", className="tabs-row", children=[
            dcc.Tab(label="Incident map", value="map"),
            dcc.Tab(label="Event stream", value="stream"),
        ]),
        html.Div(id="tab-map", children=[
            html.Div(id="kpis", className="kpis"),
            html.Div([
                html.Div([dcc.Graph(id="map", config={"displayModeBar": False, "scrollZoom": True}), legend],
                         style={**CARD, "padding": "6px"}),
                html.Div([html.Div("Incidents", className="section-title"), html.Div(id="incidents")],
                         className="side", style=CARD),
            ], className="grid"),
            html.Div(html.Div([
                html.Div([html.Div("Rule", className="section-title"),
                          html.Div("Different providers needed", className="muted"),
                          dcc.Slider(id="min-providers", min=1, max=4, step=1, value=DEFAULT_RULE["min_providers"],
                                     marks={i: str(i) for i in range(1, 5)}),
                          dcc.Checklist(id="sensor-replaces", className="checks", value=["yes"],
                                        options=[{"label": "a road operator sensor may replace one provider", "value": "yes"}]),
                          html.Div(id="rule-note", className="muted", style={"marginTop": "6px"})]),
                html.Div([html.Div("Layers", className="section-title"),
                          dcc.Checklist(id="layers", className="checks",
                                        value=["events", "incidents", "sensors", "volume"],
                                        options=[{"label": lbl, "value": k} for k, lbl in LAYERS])]),
                html.Div([html.Div("Refresh", className="section-title"),
                          dcc.Checklist(id="live", className="checks", value=["live"],
                                        options=[{"label": f"Live (every {REFRESH_MS // 1000} s)", "value": "live"}]),
                          html.Div("Traffic volume per main road at this hour, against its capacity: quiet below 55 %, "
                                   "busy below 85 %, very busy above. Modelled from open data, not measured.",
                                   className="muted", style={"marginTop": "8px"})]),
            ], className="controls"), style={**CARD, "marginTop": "14px"}),
            html.Div([
                html.Div([html.Div("Published to srti.confirmed", className="section-title"),
                          html.Div("Every new, changed or cleared incident goes out as a warning, logged in "
                                   "OUTBOUND_MESSAGES.", className="muted"),
                          html.Div(id="outbound")], style=CARD),
                html.Div([html.Div("Data offers and contracts", className="section-title"),
                          html.Div("What this service receives and passes on, under which usage policy. Metadata only: "
                                   "no connector enforces it in this iteration.", className="muted"),
                          html.Div(id="offers")], style=CARD),
            ], className="grid2"),
            html.Div(id="sql"),
        ]),
        html.Div(id="tab-stream", style={"display": "none"}, children=[
            html.Div("Event stream", className="part-title"),
            html.Div("Every SRTI event message the providers sent, newest first, as Exasol received it through Kafka: new, "
                     "update (a newer version of the same event) and cancel (withdrawn). Classed against the incidents "
                     "confirmed right now: part of an incident, a single report nobody else confirms, withdrawn, or "
                     "expired.", className="part-sub"),
            html.Div([
                html.Div([html.Div("Time window", className="section-title"),
                          dcc.RadioItems(id="stream-window", className="checks", value=5,
                                         options=[{"label": "last minute", "value": 1}, {"label": "last 5 minutes", "value": 5},
                                                  {"label": "last 15 minutes", "value": 15}, {"label": "last 30 minutes", "value": 30}])]),
                html.Div(id="stream-status", className="muted"),
            ], className="controls", style={**CARD, "marginTop": "8px"}),
            html.Div(dash_table.DataTable(
                id="stream-table", data=[],
                columns=[{"name": n, "id": k} for k, n in STREAM_COLS],
                filter_action="native", sort_action="native", page_action="native", page_size=40,
                style_table={"overflowX": "auto"},
                style_cell={"fontFamily": "inherit", "fontSize": "12px", "padding": "3px 6px", "textAlign": "left",
                            "whiteSpace": "nowrap", "border": "none", "borderBottom": f"1px solid {LINE}"},
                style_header={"fontWeight": 600, "color": MUTED, "background": "#fff", "borderBottom": f"1px solid {LINE}"},
                style_filter={"background": "#f9fafb"},
                style_data_conditional=[
                    {"if": {"filter_query": '{class} = "part of an incident"'}, "backgroundColor": "#fee2e2",
                     "color": "#991b1b", "fontWeight": 600},
                    {"if": {"filter_query": '{class} = "single report"'}, "backgroundColor": "#f3f4f6", "color": "#374151"},
                    {"if": {"filter_query": '{class} = "withdrawn" || {class} = "expired"'}, "color": "#9ca3af"},
                ]), style={**CARD, "marginTop": "14px", "padding": "6px"}),
            html.Div("Filter any column by typing in the row under the header, e.g. OEM B under provider, cancel under "
                     "status or A 9 under road.", className="muted", style={"marginTop": "6px"}),
            html.Div(id="stream-sql"),
        ]),
        html.Div(CAVEAT, className="caveat"),
        html.Div(ATTRIBUTION, className="attrib"),
        dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),
    ], className="page")

    def rule_of(min_providers, sensor_replaces):
        return {"min_providers": int(min_providers or 2), "sensor_replaces": 1 if "yes" in (sensor_replaces or []) else 0}

    @app.callback(Output("tick", "disabled"), Input("live", "value"))
    def toggle_live(live):
        return "live" not in (live or [])

    @app.callback(Output("tab-map", "style"), Output("tab-stream", "style"), Input("tabs", "value"))
    def show_tab(tab):
        return tuple({"display": "block" if tab == k else "none"} for k in ("map", "stream"))

    @app.callback(Output("clock", "children"), Input("tick", "n_intervals"))
    def clock(_n):
        try:
            sim = run_sql(server, metadata, "sim", {}, [])
        except RuntimeError as e:
            return html.Div(str(e), className="err")
        if not sim:
            return html.Div("No data yet: start the simulator and the road safety service.", className="err")
        now = ts(sim[0]["sim_now"])
        state = "" if sim[0]["status"] == "running" else " (the simulation is stopped: frozen)"
        return f"Data as of {now:%H:%M:%S}, {now:%a %d %b %Y}{state}"

    @app.callback(
        Output("kpis", "children"), Output("map", "figure"), Output("incidents", "children"),
        Output("outbound", "children"), Output("offers", "children"), Output("rule-note", "children"),
        Output("sql", "children"),
        Input("tick", "n_intervals"), Input("layers", "value"), Input("tabs", "value"),
        Input("min-providers", "value"), Input("sensor-replaces", "value"),
    )
    def refresh(n, layers, tab, min_providers, sensor_replaces):
        if tab != "map" and ctx.triggered_id in ("tick", "tabs"):
            raise PreventUpdate                      # the map tab is hidden: don't query for it
        layers = set(layers or [])
        rule = rule_of(min_providers, sensor_replaces)
        try:
            d, log = fetch(server, metadata, rule, layers)
        except RuntimeError as e:
            empty = go.Figure().update_layout(map=dict(style="carto-positron", center=dict(lat=VIEW0["lat"], lon=VIEW0["lon"]),
                                                       zoom=VIEW0["zoom"]), margin=dict(l=0, r=0, t=0, b=0), height=680,
                                              uirevision="keep")
            return [html.Div(str(e), className="err")], empty, [], [], [], "", sql_foldout([])

        confirmed = [h for h in d["live"] if yes(h["confirmed"])]
        members = set()
        for h in confirmed:
            members.update(m for m in str(h["members"] or "").split(",") if m)
        provider_events = [e for e in d["events"] if e["provider"] != "ROAD_OP"]
        singles = [e for e in provider_events if e["event_id"] not in members]
        recv = {r["provider"]: r for r in d["kpis"]}
        reports_15 = sum(int(num(r["events"])) for p, r in recv.items() if p not in ("SENSORS", "ROAD_OP"))
        published_n = sum(int(num(p["messages"])) for p in d["published"])
        kpi_row = [
            kpi("Confirmed incidents", f"{len(confirmed)}", "from several providers and road sensors"),
            kpi("Vehicles warned", f"{int(sum(num(h['reach_vehicles']) for h in confirmed)):,}",
                f"pass an incident in the next {COMBINE['reach_min']} min, or are in a fog or storm area"),
            kpi("Reports in the last 15 min", f"{reports_15:,}", "SRTI events from carmakers and the navigation service"),
            kpi("Single reports now", f"{len(singles)}", "valid, but nobody else reports them: not confirmed"),
            kpi("Warnings published", f"{published_n:,}", "to srti.confirmed in this run (new, update, cleared)"),
        ]

        events_by_id = {e["event_id"]: e for e in d["events"]}
        cards = []
        for h in sorted(confirmed, key=lambda h: str(h["first_report_at"])):
            label, color = CATEGORY.get(h["srti_category"], (h["srti_category"], "#dc2626"))
            pub = match_published(h, d["published"])
            first = ts(h["first_report_at"])
            body = [html.Div([label, f" - {h['road'] or 'road not matched'}"], className="inc-title", style={"color": color}),
                    html.Div([*provider_badges(h["provider_list"]),
                              *([badge("Road operator: " + str(h["sensor_list"]), PROVIDER_COLOR["ROAD_OP"])]
                                if h.get("sensor_list") else [])], style={"marginTop": "4px"}),
                    html.Div(f"Confirmed by {rule_text(h)}" + (f"; cause: {h['cause']}" if h.get("cause") else "")
                             + f"; confidence {num(h['confidence']):.0%}", className="inc-meta"),
                    html.Div(f"First report {first:%H:%M:%S}; valid until {hms(h['valid_until'])}; {reach_text(h)}"
                             if first else reach_text(h), className="inc-meta")]
            if pub:
                conf_at = ts(pub["confirmed_at"])
                secs = int((conf_at - ts(pub["first_report_at"])).total_seconds()) if conf_at and pub["first_report_at"] else 0
                body.append(html.Div(f"Published as {pub['incident_id']}: confirmed {conf_at:%H:%M:%S}, {secs} s after the "
                                     f"first report; {plural(pub['messages'], 'warning')} sent so far (version "
                                     f"{int(num(pub['version']))})", className="inc-pub"))
            else:
                body.append(html.Div("Not published: the service uses the default rule (2 providers, or 1 and a road "
                                     "operator sensor). This is what the rule set on this page would add.",
                                     className="inc-nopub"))
            body.append(evidence_box(h, events_by_id))
            cards.append(html.Div(body, className="inc", style={"borderColor": color}))
        cleared = [p for p in d["published"] if p["status"] == "cleared"]
        if cleared:
            cards.append(html.Div("Cleared in the last 30 minutes", className="section-title", style={"marginTop": "10px"}))
            cards += [html.Div(f"{hms(p['cleared_at'])} {p['incident_id']}: {cat_label(p['srti_category'])} on "
                               f"{p['road'] or 'unnamed road'}", className="inc-meta") for p in cleared[:8]]
        if not confirmed:
            cards.insert(0, html.Div(f"No confirmed incidents. {plural(len(singles), 'single report')} on the map: "
                                     "one provider only, so not confirmed.", className="muted"))

        outbound = table(d["outbound"], [
            ("sim_at", "time", lambda v, r: hms(v)),
            ("status", "status", lambda v, r: badge(v, STATUS_COLOR.get(v, MUTED))),
            ("incident_id", "incident", lambda v, r: f"{v} v{int(num(r['version']))}"),
            ("srti_category", "category", lambda v, r: cat_label(v) + (f" ({r['cause']})" if r.get("cause") else "")),
            ("road", "road", None),
            ("kafka_offset", "topic@offset", lambda v, r: f"{r['topic']}@{int(num(v))}" if r.get("topic") else "not sent"),
        ]) if d["outbound"] else html.Div("Nothing published yet in this run.", className="muted")
        offers = table(d["offers"], [
            ("provider_id", "from", lambda v, r: PROVIDER_LABEL.get(v, v)),
            ("topic", "topic", None),
            ("usage_policy", "usage policy", None),
            ("contract", "contract", lambda v, r: v or ("we offer it" if r["provider_id"] == "SERVICE" else "-")),
        ])
        default = rule == DEFAULT_RULE
        note = ("The service publishes with this rule." if default else
                "What-if: the map and the list show what this rule would confirm. The service keeps publishing with "
                "the default rule (2 providers, a sensor may replace one).")

        traces = map_traces(d, layers, members)
        full = ctx.triggered_id != "tick" or (n or 0) % FULL_EVERY == 0
        map_out = map_figure(traces) if full else map_patch(traces)
        return kpi_row, map_out, cards, outbound, offers, note, sql_foldout(log)

    @app.callback(Output("stream-table", "data"), Output("stream-status", "children"), Output("stream-sql", "children"),
                  Input("tick", "n_intervals"), Input("tabs", "value"), Input("stream-window", "value"),
                  Input("min-providers", "value"), Input("sensor-replaces", "value"))
    def refresh_stream(_n, tab, window, min_providers, sensor_replaces):
        if tab != "stream":
            raise PreventUpdate
        log = []
        try:
            d, _ = fetch(server, metadata, rule_of(min_providers, sensor_replaces), set())
            rows = run_sql(server, metadata, "event_stream", {"minutes": int(window or 5), "max_rows": 3000}, log)
        except RuntimeError as e:
            return [], html.Div(str(e), className="err"), sql_foldout(log)
        members = set()
        for h in d["live"]:
            if yes(h["confirmed"]):
                members.update(str(h["members"] or "").split(","))
        valid = {e["event_id"] for e in d["events"]}
        out = []
        for r in rows:
            cls = ("withdrawn" if r["status"] == "cancel" else "part of an incident" if r["event_id"] in members
                   else "single report" if r["event_id"] in valid else "expired")
            out.append({"time": hms(r["detected_at"]), "class": cls,
                        "provider": PROVIDER_LABEL.get(r["provider"], r["provider"]), "status": r["status"],
                        "version": int(num(r["version"])), "event": r["event_id"],
                        "category": cat_label(r["srti_category"]) + (f" ({r['cause']})" if r.get("cause") else ""),
                        "road": r["road"] or "", "confidence": f"{num(r['confidence']):.2f}",
                        "seen_by": int(num(r["observations"])), "valid_until": hms(r["valid_until"]),
                        "position": f"{num(r['lat']):.5f}, {num(r['lon']):.5f}",
                        "kafka": f"{r['topic']}@{int(num(r['kafka_offset']))}"})
        counts = {k: sum(1 for o in out if o["class"] == k) for k in ("part of an incident", "single report", "withdrawn", "expired")}
        status = (f"{len(out):,} messages: " + ", ".join(f"{v:,} {k}" for k, v in counts.items())
                  + f"; refreshed every {REFRESH_MS // 1000} s")
        return out, status, sql_foldout(log)

    return app


STREAM_COLS = [("time", "time"), ("class", "class"), ("provider", "provider"), ("status", "status"),
               ("version", "version"), ("category", "SRTI category"), ("road", "road"), ("confidence", "confidence"),
               ("seen_by", "seen by vehicles"), ("valid_until", "valid until"), ("position", "position"),
               ("event", "event id"), ("kafka", "Kafka topic@offset")]
