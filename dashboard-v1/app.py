"""Munich Road Events (v1): the reports the partners send, live from Exasol. One page, three queries, refreshed every 2 s."""
import importlib.util
import re
import time
from pathlib import Path

import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html

_SPEC = importlib.util.spec_from_file_location("dash_server_generated_exasol_helper",
                                               Path(__file__).with_name("dash_server_exasol.py"))
_HELPER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPER)
load_rows, has_error = _HELPER.load_rows, _HELPER.has_error

APP_DIR = Path(__file__).resolve().parent
TITLE = "Munich Road Events"
VERSION_LINE = "v1: every report the carmakers, the navigation service and the road operator send, as Exasol receives it"
CAVEAT = ("The partners are simulated: they send SRTI events through Kafka, never raw vehicle data. "
          "Exasol loads and parses them; this page reads them with SQL every 2 seconds.")
REFRESH_MS = 2000
PROVIDER_COLOR = {"OEM_A": "#2a78d6", "OEM_B": "#eb6834", "OEM_C": "#1baf7a", "OEM_D": "#eda100",
                  "OEM_E": "#e87ba4", "OEM_F": "#008300", "NAV": "#6250d6", "ROAD_OP": "#334155"}
PROVIDER_LABEL = {"OEM_A": "OEM A", "OEM_B": "OEM B", "OEM_C": "OEM C", "OEM_D": "OEM D", "OEM_E": "OEM E",
                  "OEM_F": "OEM F", "NAV": "Navigation", "ROAD_OP": "Road operator"}
CATEGORY = {"unprotected_accident_area": "Accident", "unmanaged_blockage": "Road blocked",
            "temporary_slippery_road": "Slippery road", "obstacle_on_road": "Obstacle", "wrong_way_driver": "Wrong-way driver",
            "reduced_visibility": "Reduced visibility", "exceptional_weather": "Exceptional weather",
            "short_term_road_works": "Road works"}
STATUS_COLOR = {"new": "#dc2626", "update": "#2563eb", "cancel": "#6b7280"}

INDEX_STRING = """<!DOCTYPE html>
<html><head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}
<style>
body { margin: 0; background: #f4f5f7; color: #1f2937; font-family: Inter, -apple-system, "Segoe UI", Roboto, sans-serif; }
.page { max-width: 1400px; margin: 0 auto; padding: 18px 24px 40px; }
h1 { font-size: 26px; margin: 0; }
.sub { color: #6b7280; margin-top: 4px; font-size: 14px; }
.clock { color: #374151; font-size: 13px; margin-top: 6px; font-variant-numeric: tabular-nums; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 16px; }
.kpis { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.kpi-label { font-size: 12px; color: #6b7280; }
.kpi-value { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
.grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 14px; margin-top: 14px; }
@media (max-width: 1000px) { .grid, .kpis { grid-template-columns: 1fr; } .page { padding: 14px; } }
.section-title { font-weight: 650; font-size: 14px; margin: 2px 0 8px; }
.legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; color: #4b5563; margin-bottom: 4px; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 4px; }
table.tbl { border-collapse: collapse; width: 100%; font-size: 12px; font-variant-numeric: tabular-nums; }
table.tbl th { text-align: left; color: #6b7280; font-weight: 600; border-bottom: 1px solid #e5e7eb; padding: 4px 6px; }
table.tbl td { border-bottom: 1px solid #f3f4f6; padding: 4px 6px; }
.badge { font-size: 11px; font-weight: 600; color: #fff; border-radius: 10px; padding: 1px 7px; white-space: nowrap; }
.caveat { margin-top: 14px; font-size: 13px; color: #6b7280; border-left: 3px solid #d1d5db; padding-left: 10px; }
details.sql { margin-top: 14px; background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 10px 16px; }
details.sql summary { cursor: pointer; font-weight: 600; }
.sql-meta { font-size: 12px; color: #6b7280; margin: 12px 0 4px; }
.sql-text { margin: 0; white-space: pre-wrap; font-size: 12px; background: #f9fafb; border: 1px solid #e5e7eb; padding: 8px 10px; border-radius: 6px; }
.err { color: #991b1b; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 8px 12px; margin-top: 10px; font-size: 13px; }
</style></head>
<body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"""


def run_sql(server, metadata, name, params, log):
    rel = f"queries/{name}.sql"
    t0 = time.perf_counter()
    rows = load_rows(server, metadata, __file__, rel, params=params)
    error = rows[0]["_error"] if has_error(rows) else None
    text = re.sub(r"\{(\w+)!\w\}", lambda m: str(params.get(m.group(1), m.group(0))), (APP_DIR / rel).read_text())
    log.append({"name": rel, "rows": 0 if error else len(rows), "ms": round((time.perf_counter() - t0) * 1000),
                "sql": text, "error": error})
    if error:
        raise RuntimeError(f"{rel}: {error}")
    return [{k.lower(): v for k, v in r.items()} for r in rows]


def hms(v):
    return str(v)[11:19] if v else "-"


def badge(text, color):
    return html.Span(text, className="badge", style={"background": color})


def kpi(label, value):
    return html.Div([html.Div(label, className="kpi-label"), html.Div(value, className="kpi-value")], className="card")


def map_figure(events):
    fig = go.Figure()
    for p, color in PROVIDER_COLOR.items():
        mine = [e for e in events if e["provider"] == p]
        fig.add_trace(go.Scattermap(
            lat=[float(e["lat"]) for e in mine], lon=[float(e["lon"]) for e in mine], mode="markers",
            marker={"size": 13, "color": color}, name=PROVIDER_LABEL[p], showlegend=False,
            text=[f"{PROVIDER_LABEL[p]}: {CATEGORY.get(e['srti_category'], e['srti_category'])}"
                  f"{' (' + e['cause'] + ')' if e['cause'] else ''}<br>{e['road'] or 'unnamed road'}, "
                  f"detected {hms(e['detected_at'])}, confidence {float(e['confidence'] or 0):.2f}" for e in mine],
            hoverinfo="text"))
    fig.update_layout(map={"style": "carto-positron", "center": {"lat": 48.150, "lon": 11.590}, "zoom": 10.0},
                      margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=560, uirevision="keep")
    return fig


def stream_table(rows):
    head = html.Tr([html.Th(c) for c in ("Time", "Provider", "Message", "Event", "Road")])
    body = [html.Tr([html.Td(hms(r["detected_at"])),
                     html.Td(badge(PROVIDER_LABEL.get(r["provider"], r["provider"]), PROVIDER_COLOR.get(r["provider"], "#6b7280"))),
                     html.Td(html.Span(r["status"], style={"color": STATUS_COLOR.get(r["status"], "#374151"), "fontWeight": 600})),
                     html.Td(CATEGORY.get(r["srti_category"], r["srti_category"])),
                     html.Td(r["road"] or "-")]) for r in rows]
    return html.Table([html.Thead(head), html.Tbody(body)], className="tbl")


def create_dash_app(server, url_base_pathname, metadata):
    app = Dash(__name__, server=server, routes_pathname_prefix="/",
               requests_pathname_prefix=url_base_pathname.rstrip("/") + "/", title=TITLE)
    app.index_string = INDEX_STRING
    legend = html.Div([html.Span([html.I(style={"background": c}), PROVIDER_LABEL[p]]) for p, c in PROVIDER_COLOR.items()],
                      className="legend")
    app.layout = html.Div([
        html.H1(TITLE), html.Div(VERSION_LINE, className="sub"), html.Div(id="clock", className="clock"),
        html.Div(id="kpis", className="kpis"),
        html.Div([
            html.Div([html.Div("Reports valid now", className="section-title"), legend,
                      dcc.Graph(id="map", figure=map_figure([]), config={"displayModeBar": False})], className="card"),
            html.Div([html.Div("Newest messages (last 5 minutes)", className="section-title"),
                      html.Div(id="stream")], className="card"),
        ], className="grid"),
        html.Div(id="error"),
        html.Div(CAVEAT, className="caveat"),
        html.Div(id="sql"),
        dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),
    ], className="page")

    @app.callback(Output("clock", "children"), Output("kpis", "children"), Output("map", "figure"),
                  Output("stream", "children"), Output("sql", "children"), Output("error", "children"),
                  Input("tick", "n_intervals"))
    def refresh(_n):
        log = []
        try:
            sim = run_sql(server, metadata, "sim", {}, log)
            events = run_sql(server, metadata, "events_now", {"window_min": 30}, log)
            stream = run_sql(server, metadata, "event_stream", {"minutes": 5, "max_rows": 15}, log)
            error = None
        except RuntimeError as e:
            sim, events, stream, error = [], [], [], html.Div(str(e), className="err")
        s = sim[0] if sim else {}
        clock = (f"Simulation time {hms(s.get('sim_now'))} ({s.get('status', 'no run')}, run {s.get('run_id', '-')})"
                 if s else "No simulation run yet")
        kpis = [kpi("Reports valid now", f"{len(events):,}"),
                kpi("Providers reporting", f"{len({e['provider'] for e in events}):,}"),
                kpi("Last message", hms(stream[0]["detected_at"]) if stream else "-")]
        sql = html.Details([html.Summary(f"SQL executed ({len(log)} statements, every {REFRESH_MS // 1000} s)")] +
                           [html.Div([html.Div(f"{x['name']} - {x['rows']:,} rows - {x['ms']:,} ms", className="sql-meta"),
                                      html.Pre(x["sql"], className="sql-text")]) for x in log], className="sql")
        return clock, kpis, map_figure(events), stream_table(stream), sql, error

    return app
