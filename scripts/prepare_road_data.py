#!/usr/bin/env python3
"""Download the open data for the road safety demo and turn it into flat CSVs for schema ROAD_DATA.

Sources, licenses and limits: research/road-data-sources.md. The plan: docs/road-safety-demo-plan.md.

    python3 -m venv .venv && .venv/bin/pip install osmium shapely pyexasol
    .venv/bin/python scripts/prepare_road_data.py --out ~/road-data
    scripts/load_road_data.sh road-safety-demo ~/road-data

The database is an Exasol Personal deployment (`exasol install local`), loaded with exapump;
`road-safety-demo` is the exapump profile for it. No starter kit needed.

The study area is the city of Munich (union of the 25 districts from MUNICH_DATASET's
districts.csv) plus the full motorway ring: the convex hull of the city and the A99,
buffered by 2 km. Inside the city every road for cars is kept; outside it only motorways.

Output files (-> table names after load, schema ROAD_DATA):
  road_segments.csv        ROAD_SEGMENTS        OSM roads split at junctions and every ~300 m: class, one-way,
                                                speed limit, bridge/tunnel, Isar crossing, district, geometry (WKT)
  road_areas.csv           ROAD_AREAS           city boundary, study area, forests and parks (wildlife risk),
                                                the Isar (WKT)
  accidents.csv            ACCIDENTS            Unfallatlas, accidents with personal injury, 2016-2025,
                                                with the nearest road segment
  weather_stations.csv     WEATHER_STATIONS     DWD stations 3379 Muenchen-Stadt and 1262 Muenchen-Flughafen
  weather_10min.csv        WEATHER_10MIN        DWD 10-minute values: temperature, 5 cm temperature, humidity,
                                                dew point, precipitation, wind, gusts (UTC and local time)
  weather_visibility.csv   WEATHER_VISIBILITY   DWD hourly visibility
  motorway_events.csv      MOTORWAY_EVENTS      Autobahn GmbH roadworks, closures and warnings: a snapshot
                                                taken when the script runs
  traffic_profile.csv      TRAFFIC_PROFILE      share of the daily traffic per hour, inbound/outbound/ring (BASt,
                                                Tue-Thu working days)
  bast_stations.csv        BAST_STATIONS        the BASt count stations used, with weekday traffic and peak load
  traffic_hotspots.csv     TRAFFIC_HOTSPOTS     known congestion hotspots (data/source/traffic_hotspots.csv)
  segment_traffic.csv      SEGMENT_TRAFFIC      per main road segment: lanes, capacity, free speed, peak load,
                                                direction, hotspot, real roadworks on the simulated day
  road_data_sources.csv    ROAD_DATA_SOURCES    source, license, URL and retrieval time per table
  districts.csv            DISTRICTS            copied from the Munich dataset (data/munich-dataset), so the
  roadworks.csv            ROADWORKS            schema is complete on its own

Run with --only to rebuild single parts, e.g. `--only autobahn` right before a talk.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
import shutil
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import numpy as np
    import osmium
    import shapely
    from shapely import wkt as shp_wkt
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree
except ImportError as e:  # pragma: no cover
    sys.exit(f"error: {e}. Install with: .venv/bin/pip install osmium shapely")

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "munich-mobility-agents/1.0 (road safety demo data preparation)"
BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

GEOFABRIK_URL = "https://download.geofabrik.de/europe/germany/bayern/oberbayern-latest.osm.pbf"
DWD_BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"
UNFALL_BASE = "https://www.opengeodata.nrw.de/produkte/transport_verkehr/unfallatlas"
AUTOBAHN_BASE = "https://verkehr.autobahn.de/o/autobahn"

MOTORWAYS = ["A8", "A9", "A92", "A94", "A95", "A96", "A99", "A995"]
CITY_CLASSES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street",
}
MOTORWAY_CLASSES = {"motorway", "motorway_link"}
DEFAULT_SPEED = {"motorway": None, "motorway_link": 80, "trunk": 100, "trunk_link": 60,
                 "living_street": 7}
SEGMENT_MAX_M = 300.0
BBOX = (11.2, 47.95, 12.0, 48.40)  # lon_min, lat_min, lon_max, lat_max: coarse prefilter

STATIONS = [  # DWD station list, zehn_min_rr_Beschreibung_Stationen.txt
    {"station_id": 3379, "station_name": "München-Stadt", "lat": 48.1632, "lon": 11.5429, "elevation_m": 515},
    {"station_id": 1262, "station_name": "München-Flughafen", "lat": 48.3477, "lon": 11.8134, "elevation_m": 446},
]

# --------------------------------------------------------------------------- helpers
LAT0, LON0 = 48.137, 11.575
KX = 111320.0 * math.cos(math.radians(LAT0))
KY = 110540.0


def to_m(geom):
    """WGS84 lon/lat -> local metres around Munich (good to well under 1 % in the study area)."""
    return shapely.transform(geom, lambda a: np.column_stack(((a[:, 0] - LON0) * KX, (a[:, 1] - LAT0) * KY)))


def to_ll(geom):
    return shapely.transform(geom, lambda a: np.column_stack((a[:, 0] / KX + LON0, a[:, 1] / KY + LAT0)))


def dist_m(lon1, lat1, lon2, lat2):
    return math.hypot((lon2 - lon1) * KX, (lat2 - lat1) * KY)


def heading(lon1, lat1, lon2, lat2):
    return round((math.degrees(math.atan2((lon2 - lon1) * KX, (lat2 - lat1) * KY)) + 360) % 360, 1)


def fetch(url: str, dest: Path | None = None, retries: int = 3) -> bytes | Path:
    """GET a URL. With dest: cache the file there and return the path."""
    if dest is not None and dest.exists() and dest.stat().st_size > 0:
        return dest
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r:
                if dest is None:
                    return r.read()
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                tmp.rename(dest)
                return dest
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise RuntimeError(f"download failed: {url}: {e}") from e
            time.sleep(2 * (attempt + 1))
    raise AssertionError


def list_dir(url: str, pattern: str) -> list[str]:
    html = fetch(url).decode("utf-8", "replace")
    # DWD serves an HTML index (href="..."), the NRW portal a JSON listing ("name":"..."): match any quoted name
    return sorted(set(re.findall(rf'"({pattern})"', html)))


def write_csv(path: Path, header: list[str], rows) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})
            n += 1
    print(f"  {path.name:26s} {n:>9,} rows")
    return n


def wkt(geom, precision=6) -> str:
    return shapely.to_wkt(geom, rounding_precision=precision, trim=True)


# --------------------------------------------------------------------------- study area
def load_districts(path: Path):
    districts = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            districts.append((int(r["district_no"]), shp_wkt.loads(r["polygon_wkt"]).buffer(0)))
    city = shapely.union_all([g for _, g in districts]).buffer(0)
    return districts, city


def district_of(districts, lons, lats):
    out = np.zeros(len(lons), dtype=int)
    for no, poly in districts:
        shapely.prepare(poly)
        hit = shapely.contains_xy(poly, lons, lats)
        out[(out == 0) & hit] = no
    return out


# --------------------------------------------------------------------------- OSM
def parse_speed(tag: str | None, road_class: str):
    if not tag:
        return DEFAULT_SPEED.get(road_class, 50 if road_class in CITY_CLASSES else None), "default"
    t = tag.strip().lower()
    if t in ("none", "signals", "variable"):
        return None, t
    if t in ("de:urban", "de:zone30", "de:zone:30"):
        return (30 if "30" in t else 50), "zone"
    if t == "de:rural":
        return 100, "zone"
    if t in ("walk", "de:living_street"):
        return 7, "zone"
    m = re.match(r"(\d+)\s*(mph)?", t)
    if m:
        v = int(m.group(1))
        return (round(v * 1.609) if m.group(2) else v), "tagged"
    return None, "unknown"


def parse_oneway(tags, road_class: str) -> int:
    v = (tags.get("oneway") or "").lower()
    if v in ("yes", "true", "1"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if v in ("no", "false", "0"):
        return 0
    if road_class in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


def read_osm(pbf: Path):
    """One pass over the extract: car roads, the Isar, forests and parks."""
    ways, isar, areas = [], [], []
    lon_min, lat_min, lon_max, lat_max = BBOX
    wkt_factory = osmium.geom.WKTFactory()
    fp = (osmium.FileProcessor(str(pbf))
          .with_locations()
          .with_areas(osmium.filter.KeyFilter("landuse", "natural", "leisure"))
          .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY | osmium.osm.AREA))
          .with_filter(osmium.filter.KeyFilter("highway", "waterway", "landuse", "natural", "leisure")))
    for o in fp:
        tags = o.tags
        if o.is_way():
            hw = tags.get("highway")
            if hw in CITY_CLASSES:
                nodes = [(n.ref, n.lon, n.lat) for n in o.nodes if n.location.valid()]
                if len(nodes) < 2 or not any(lon_min <= x <= lon_max and lat_min <= y <= lat_max for _, x, y in nodes):
                    continue
                ways.append((o.id, hw, dict(tags), nodes))
            elif tags.get("waterway") == "river" and tags.get("name") == "Isar":
                pts = [(n.lon, n.lat) for n in o.nodes if n.location.valid()]
                if len(pts) >= 2:
                    isar.append(LineString(pts))
        elif o.is_area():
            kind = ("forest" if tags.get("landuse") == "forest" or tags.get("natural") == "wood"
                    else "park" if tags.get("leisure") == "park" else None)
            if not kind:
                continue
            try:
                geom = shp_wkt.loads(wkt_factory.create_multipolygon(o))
            except Exception:  # noqa: BLE001 - broken multipolygons are skipped
                continue
            minx, miny, maxx, maxy = geom.bounds
            if maxx < lon_min or minx > lon_max or maxy < lat_min or miny > lat_max:
                continue
            areas.append((o.orig_id(), kind, tags.get("name", ""), geom))
    return ways, isar, areas


def build_study_area(city, ways):
    a99 = [LineString([(x, y) for _, x, y in nodes]) for _, hw, tags, nodes in ways
           if hw == "motorway" and "A 99" in (tags.get("ref") or "").replace("A99", "A 99")]
    if not a99:
        raise RuntimeError("A99 not found in the OSM extract")
    hull = shapely.union_all([city, *a99]).convex_hull
    return to_ll(to_m(hull).buffer(2000)), shapely.union_all(a99)


def split_ways(ways, city, study):
    """Split ways at shared nodes and every ~300 m; keep what belongs to the study area."""
    usage: dict[int, int] = {}
    for _, _, _, nodes in ways:
        for i, (ref, _, _) in enumerate(nodes):
            usage[ref] = usage.get(ref, 0) + (2 if i in (0, len(nodes) - 1) else 1)
    raw = []
    for way_id, hw, tags, nodes in ways:
        start, acc = 0, 0.0
        for i in range(1, len(nodes)):
            acc += dist_m(nodes[i - 1][1], nodes[i - 1][2], nodes[i][1], nodes[i][2])
            if i == len(nodes) - 1 or usage.get(nodes[i][0], 0) >= 2 or acc >= SEGMENT_MAX_M:
                raw.append((way_id, hw, tags, nodes[start:i + 1], acc))
                start, acc = i, 0.0
    mids = np.array([shapely.line_interpolate_point(LineString([(x, y) for _, x, y in seg]), 0.5, normalized=True).coords[0]
                     for _, _, _, seg, _ in raw])
    shapely.prepare(city)
    shapely.prepare(study)
    in_city = shapely.contains_xy(city, mids[:, 0], mids[:, 1])
    in_study = shapely.contains_xy(study, mids[:, 0], mids[:, 1])
    keep = [i for i, (_, hw, _, _, _) in enumerate(raw) if in_city[i] or (hw in MOTORWAY_CLASSES and in_study[i])]
    return [raw[i] for i in keep], mids[keep], in_city[keep]


def osm_part(args, out: Path, cache: Path, districts, city, sources):
    pbf = fetch(GEOFABRIK_URL, cache / "oberbayern-latest.osm.pbf")
    print(f"Reading {pbf.name} (a few minutes) ...")
    ways, isar_lines, areas = read_osm(pbf)
    study, a99 = build_study_area(city, ways)
    segs, mids, in_city = split_ways(ways, city, study)
    isar = shapely.union_all(isar_lines) if isar_lines else None
    if isar is not None:
        shapely.prepare(isar)
    dist_no = district_of(districts, mids[:, 0], mids[:, 1])

    rows, lines = [], []
    for k, (way_id, hw, tags, nodes, length) in enumerate(segs):
        line = LineString([(x, y) for _, x, y in nodes])
        lines.append(line)
        is_bridge = int(tags.get("bridge", "no") not in ("no", ""))
        speed, speed_src = parse_speed(tags.get("maxspeed"), hw)
        (f_ref, f_lon, f_lat), (t_ref, t_lon, t_lat) = nodes[0], nodes[-1]
        rows.append({
            "segment_id": k + 1, "osm_way_id": way_id, "from_node": f_ref, "to_node": t_ref,
            "road_class": hw, "name": tags.get("name", ""), "ref": tags.get("ref", ""),
            "oneway": parse_oneway(tags, hw), "maxspeed_kmh": speed, "maxspeed_source": speed_src,
            "lanes": tags.get("lanes", "") if (tags.get("lanes", "") or "").isdigit() else "",
            "is_bridge": is_bridge, "bridge_name": tags.get("bridge:name", ""),
            "is_tunnel": int(tags.get("tunnel", "no") not in ("no", "")),
            "crosses_isar": int(bool(is_bridge and isar is not None and shapely.intersects(isar, line))),
            "in_munich": int(in_city[k]), "district_no": int(dist_no[k]) or None,
            "length_m": round(length, 1), "heading_deg": heading(f_lon, f_lat, t_lon, t_lat),
            "from_lat": round(f_lat, 6), "from_lon": round(f_lon, 6), "to_lat": round(t_lat, 6), "to_lon": round(t_lon, 6),
            "mid_lat": round(mids[k][1], 6), "mid_lon": round(mids[k][0], 6), "geometry_wkt": wkt(line),
        })
    write_csv(out / "road_segments.csv", list(rows[0]), rows)

    area_rows = [
        {"area_id": 1, "kind": "city_boundary", "name": "München", "area_km2": round(to_m(city).area / 1e6, 2),
         "centroid_lat": round(city.centroid.y, 6), "centroid_lon": round(city.centroid.x, 6), "geometry_wkt": wkt(city.simplify(0.0001))},
        {"area_id": 2, "kind": "study_area", "name": "Munich and the motorway ring", "area_km2": round(to_m(study).area / 1e6, 2),
         "centroid_lat": round(study.centroid.y, 6), "centroid_lon": round(study.centroid.x, 6), "geometry_wkt": wkt(study.simplify(0.0005))},
    ]
    if isar is not None:
        isar_clip = isar.intersection(study)
        area_rows.append({"area_id": 3, "kind": "river", "name": "Isar", "area_km2": None,
                          "centroid_lat": round(isar_clip.centroid.y, 6), "centroid_lon": round(isar_clip.centroid.x, 6),
                          "geometry_wkt": wkt(isar_clip.simplify(0.0001))})
    shapely.prepare(study)
    next_id = 10
    for osm_id, kind, name, geom in areas:
        if not shapely.intersects(study, geom):
            continue
        km2 = to_m(geom).area / 1e6
        if (kind == "forest" and km2 < 0.5) or (kind == "park" and (km2 < 0.1 or not name)):
            continue
        g = geom.simplify(0.0001)
        area_rows.append({"area_id": next_id, "kind": kind, "name": name, "area_km2": round(km2, 2),
                          "centroid_lat": round(geom.centroid.y, 6), "centroid_lon": round(geom.centroid.x, 6),
                          "geometry_wkt": wkt(g)})
        next_id += 1
    write_csv(out / "road_areas.csv", list(area_rows[0]), area_rows)
    stamp = pbf.stat().st_mtime
    sources += [
        {"table_name": t, "source": "OpenStreetMap via Geofabrik (oberbayern-latest.osm.pbf)", "license": "ODbL 1.0",
         "attribution": "© OpenStreetMap contributors", "url": GEOFABRIK_URL,
         "retrieved_at": datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
        for t in ("ROAD_SEGMENTS", "ROAD_AREAS")]
    print(f"  {sum(r['crosses_isar'] for r in rows)} segments cross the Isar on a bridge, "
          f"{sum(1 for r in rows if r['road_class'] in MOTORWAY_CLASSES)} motorway segments")
    return lines


# --------------------------------------------------------------------------- accidents
ACC_ALIASES = {
    "accident_id": ["uidentstlae", "objectid", "id", "oid_", "fid"],
    "land": ["uland"], "regbez": ["uregbez"], "kreis": ["ukreis"],
    "year": ["ujahr"], "month": ["umonat"], "hour": ["ustunde"], "weekday": ["uwochentag"],
    "category": ["ukategorie"], "accident_kind": ["uart"], "accident_type": ["utyp1"],
    "light": ["ulichtverh", "licht"],
    "road_condition": ["iststrassenzustand", "ustrzustand", "strzustand", "iststrasse"],
    "involves_bicycle": ["istrad"], "involves_car": ["istpkw"], "involves_pedestrian": ["istfuss"],
    "involves_motorcycle": ["istkrad"], "involves_goods_vehicle": ["istgkfz"],
    "involves_other": ["istsonstige", "istsonstig"],
    "lon": ["xgcswgs84"], "lat": ["ygcswgs84"],
}
ACC_HEADER = ["accident_id", "year", "month", "hour", "weekday", "category", "accident_kind", "accident_type",
              "light", "road_condition", "involves_bicycle", "involves_car", "involves_pedestrian",
              "involves_motorcycle", "involves_goods_vehicle", "involves_other", "lat", "lon",
              "in_munich", "district_no", "segment_id", "segment_distance_m"]


def read_accident_zip(path: Path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        name = next((n for n in names if n.lower().endswith(".csv")), None) or next(n for n in names if n.lower().endswith(".txt"))
        raw = z.read(name)
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rd = csv.reader(io.StringIO(text), delimiter=";")
    header = [h.strip().lower() for h in next(rd)]
    col = {k: next((header.index(a) for a in al if a in header), None) for k, al in ACC_ALIASES.items()}
    for r in rd:
        yield {k: (r[i].strip().replace(",", ".") if i is not None and i < len(r) else "") for k, i in col.items()}


def accidents_part(args, out: Path, cache: Path, districts, city, study, seg_lines, sources):
    files = list_dir(UNFALL_BASE + "/", r"Unfallorte\d{4}_EPSG25832_CSV\.zip")
    years = [y for y in range(args.accident_from, 2100) if f"Unfallorte{y}_EPSG25832_CSV.zip" in files]
    print(f"Unfallatlas: years {years[0]}-{years[-1]}")
    rows = []
    shapely.prepare(study)
    for y in years:
        p = fetch(f"{UNFALL_BASE}/Unfallorte{y}_EPSG25832_CSV.zip", cache / "unfallatlas" / f"Unfallorte{y}_EPSG25832_CSV.zip")
        for r in read_accident_zip(p):
            if r["land"].lstrip("0") != "9":
                continue
            try:
                lon, lat = float(r["lon"]), float(r["lat"])
            except ValueError:
                continue
            if not shapely.contains_xy(study, lon, lat):
                continue
            r["lat"], r["lon"] = round(lat, 6), round(lon, 6)
            r["year"] = r["year"] or y
            r["accident_id"] = f"{y}-{r['accident_id']}"
            rows.append(r)
    lons = np.array([r["lon"] for r in rows])
    lats = np.array([r["lat"] for r in rows])
    shapely.prepare(city)
    in_city = shapely.contains_xy(city, lons, lats)
    dist_no = district_of(districts, lons, lats)
    if seg_lines:
        tree = STRtree([to_m(g) for g in seg_lines])
        pts = to_m(shapely.points(lons, lats))
        idx, dists = tree.query_nearest(pts, max_distance=50, return_distance=True, all_matches=False)
        nearest = {int(i): (int(j) + 1, round(float(d), 1)) for (i, j), d in zip(idx.T, dists)}
    else:
        nearest = {}
    for k, r in enumerate(rows):
        r["in_munich"] = int(in_city[k])
        r["district_no"] = int(dist_no[k]) or None
        r["segment_id"], r["segment_distance_m"] = nearest.get(k, (None, None))
    write_csv(out / "accidents.csv", ACC_HEADER, rows)
    sources.append({"table_name": "ACCIDENTS", "source": "Unfallatlas, Statistische Ämter des Bundes und der Länder",
                    "license": "dl-de/by-2-0", "attribution": "Statistische Ämter des Bundes und der Länder, Unfallatlas, "
                    "dl-de/by-2-0 (www.govdata.de/dl-de/by-2-0); clipped to the study area and matched to OSM segments",
                    "url": UNFALL_BASE + "/", "retrieved_at": NOW})


# --------------------------------------------------------------------------- weather
DWD_PRODUCTS = [  # (resolution, folder, file prefix, {DWD column: our column})
    ("10_minutes", "air_temperature", "10minutenwerte_TU",
     {"TT_10": "air_temp_c", "TM5_10": "temp_5cm_c", "RF_10": "rel_humidity_pct", "TD_10": "dew_point_c"}),
    ("10_minutes", "precipitation", "10minutenwerte_nieder",
     {"RWS_10": "precip_mm", "RWS_DAU_10": "precip_minutes"}),
    ("10_minutes", "wind", "10minutenwerte_wind", {"FF_10": "wind_ms", "DD_10": "wind_dir_deg"}),
    ("10_minutes", "extreme_wind", "10minutenwerte_extrema_wind", {"FX_10": "gust_ms"}),
    ("hourly", "visibility", "stundenwerte_VV", {"V_VV": "visibility_m"}),
]
W10_HEADER = ["station_id", "ts_utc", "ts_local", "air_temp_c", "temp_5cm_c", "rel_humidity_pct", "dew_point_c",
              "precip_mm", "precip_minutes", "wind_ms", "wind_dir_deg", "gust_ms"]
VIS_HEADER = ["station_id", "ts_utc", "ts_local", "visibility_m"]


def dwd_files(res, folder, prefix, station: int, since: str):
    sid = f"{station:05d}"
    out = []
    for name in list_dir(f"{DWD_BASE}/{res}/{folder}/historical/", rf"{prefix}_{sid}_\d{{8}}_\d{{8}}_hist\.zip"):
        end = re.search(r"_(\d{8})_hist", name).group(1)
        if end >= since:
            out.append(f"{DWD_BASE}/{res}/{folder}/historical/{name}")
    out.append(f"{DWD_BASE}/{res}/{folder}/recent/{prefix}_{sid}_akt.zip")
    return out


def read_dwd_zip(path: Path):
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.startswith("produkt"))
        text = z.read(name).decode("latin-1")
    rd = csv.reader(io.StringIO(text), delimiter=";")
    header = [h.strip() for h in next(rd)]
    for r in rd:
        yield dict(zip(header, (v.strip() for v in r)))


def ts_pair(stamp: str):
    fmt = "%Y%m%d%H%M" if len(stamp) == 12 else "%Y%m%d%H"
    t = datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S"), t.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M:%S")


def weather_part(args, out: Path, cache: Path, sources):
    since = args.weather_from.replace("-", "")
    ten: dict[tuple, dict] = {}
    vis: dict[tuple, dict] = {}
    for st in STATIONS:
        for res, folder, prefix, cols in DWD_PRODUCTS:
            target = vis if res == "hourly" else ten
            for url in dwd_files(res, folder, prefix, st["station_id"], since):
                p = fetch(url, cache / "dwd" / url.rsplit("/", 1)[1])
                for r in read_dwd_zip(p):
                    stamp = r.get("MESS_DATUM", "")
                    if stamp[:8] < since:
                        continue
                    key = (st["station_id"], stamp)
                    row = target.get(key)
                    if row is None:
                        utc, local = ts_pair(stamp)
                        row = target[key] = {"station_id": st["station_id"], "ts_utc": utc, "ts_local": local}
                    for src, dst in cols.items():
                        v = r.get(src, "")
                        row[dst] = None if v in ("", "-999") or v.startswith("-999") else v
    write_csv(out / "weather_stations.csv", list(STATIONS[0]), STATIONS)
    write_csv(out / "weather_10min.csv", W10_HEADER, (ten[k] for k in sorted(ten)))
    write_csv(out / "weather_visibility.csv", VIS_HEADER, (vis[k] for k in sorted(vis)))
    for t in ("WEATHER_STATIONS", "WEATHER_10MIN", "WEATHER_VISIBILITY"):
        sources.append({"table_name": t, "source": "Deutscher Wetterdienst, Climate Data Center", "license": "CC BY 4.0",
                        "attribution": "Source: Deutscher Wetterdienst", "url": DWD_BASE + "/", "retrieved_at": NOW})


# --------------------------------------------------------------------------- motorways
EVENT_HEADER = ["event_id", "road", "service", "display_type", "title", "subtitle", "is_blocked", "future",
                "start_ts", "abnormal_traffic_type", "delay_minutes", "lat", "lon", "extent_wkt", "description",
                "retrieved_at"]


def iso_utc(value: str | None) -> str | None:
    """ISO 8601 with time zone ('2026-10-17T00:00:00+02:00', '...Z') -> 'YYYY-MM-DD HH:MM:SS' in UTC."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def autobahn_part(args, out: Path, study, sources):
    rows, seen = [], set()
    shapely.prepare(study)
    for road in MOTORWAYS:
        for service in ("roadworks", "closure", "warning"):
            try:
                data = json.loads(fetch(f"{AUTOBAHN_BASE}/{road}/services/{service}"))
            except RuntimeError as e:
                print(f"  warning: {e}")
                continue
            for item in next(iter(data.values()), []):
                c = item.get("coordinate") or {}
                try:
                    lat, lon = float(c["lat"]), float(c["long"])
                except (KeyError, TypeError, ValueError):
                    continue
                if item["identifier"] in seen or not shapely.contains_xy(study, lon, lat):
                    continue
                seen.add(item["identifier"])
                ext = [float(v) for v in (item.get("extent") or "").split(",") if v.strip()]
                extent = (f"LINESTRING ({ext[1]:.6f} {ext[0]:.6f}, {ext[3]:.6f} {ext[2]:.6f})" if len(ext) == 4 else None)
                rows.append({
                    "event_id": item["identifier"], "road": road, "service": service,
                    "display_type": item.get("display_type"), "title": item.get("title"),
                    "subtitle": (item.get("subtitle") or "").strip(), "is_blocked": int(item.get("isBlocked") == "true"),
                    "future": int(bool(item.get("future"))), "start_ts": iso_utc(item.get("startTimestamp")),
                    "abnormal_traffic_type": item.get("abnormalTrafficType"), "delay_minutes": item.get("delayTimeValue"),
                    "lat": round(lat, 6), "lon": round(lon, 6), "extent_wkt": extent,
                    "description": " / ".join(x for x in item.get("description", []) if x)[:2000],
                    "retrieved_at": NOW,
                })
    write_csv(out / "motorway_events.csv", EVENT_HEADER, rows)
    sources.append({"table_name": "MOTORWAY_EVENTS", "source": "Die Autobahn GmbH des Bundes (Mobilithek offer "
                    "'Die Autobahn App'; endpoint verkehr.autobahn.de)", "license": "GeoNutzV",
                    "attribution": "© Die Autobahn GmbH des Bundes", "url": AUTOBAHN_BASE + "/", "retrieved_at": NOW})


# --------------------------------------------------------------------------- traffic model inputs
BAST_HOURLY_URL = "https://www.bast.de/videos/{year}_A_S.zip"
BAST_STATIONS_URL = ("https://www.bast.de/DE/Themen/Digitales/HF_1/Massnahmen/verkehrszaehlung/Daten/{year}_1/"
                     "Jawe{year}.csv?view=renderTcDataExportCSV")
# BASt permanent count stations on the motorways around Munich (Jawe2024 numbers)
BAST_STATIONS = ("9115 9066 9242 9064 9063 9062 9215 9155 9775 9213 9244 9229 9207 9219 9220 9141 9140 9224 "
                 "9082 9083 9106 9222 9043 9221 9217 9987").split()
MAIN_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
                "secondary", "secondary_link", "tertiary"}
CAP_PER_LANE = {"motorway": 1900, "trunk": 1700, "primary": 1100, "secondary": 900, "tertiary": 700}
LINK_CAP = 1200
DEFAULT_LANES = {"motorway": 3, "trunk": 2, "primary": 2, "secondary": 1, "tertiary": 1}
# load at the peak hour (volume / capacity) for city roads without counts; motorways come from BASt
CITY_PEAK_VC = {"trunk": 0.80, "primary": 0.75, "secondary": 0.65, "tertiary": 0.50}
CENTRE = (11.575, 48.137)
TRAFFIC_HEADER = ["segment_id", "road_class", "road", "lanes_dir", "capacity_vph", "free_kmh", "peak_vc", "flow_dir",
                  "hotspot_id", "hotspot_factor", "roadworks", "roadworks_source", "capacity_factor"]


def bast_part(args, out: Path, cache: Path, sources) -> dict:
    """Hourly profiles (Tue-Thu working days) and per-motorway load from BASt count stations."""
    year = args.bast_year
    zpath = fetch(BAST_HOURLY_URL.format(year=year), cache / "bast" / f"{year}_A_S.zip")
    jawe_text = fetch(BAST_STATIONS_URL.format(year=year)).decode("latin-1")
    jawe = {r["DZ_Nr"]: r for r in csv.DictReader(io.StringIO(jawe_text), delimiter=";")}
    kind = {}
    for z in BAST_STATIONS:
        r = jawe.get(z)
        if not r:
            continue
        if r["Str_Nr"].strip() == "99":
            kind[z] = ("ring", 0)
        elif "München" in r["Fernziel_Ri1"]:
            kind[z] = ("radial", 1)
        elif "München" in r["Fernziel_Ri2"]:
            kind[z] = ("radial", 2)
    sums: dict = {}
    print(f"BASt {year}: reading hourly values of {len(kind)} stations (a minute) ...")
    with zipfile.ZipFile(zpath) as zf, zf.open(zf.namelist()[0]) as fh:
        rd = csv.reader(io.TextIOWrapper(fh, encoding="latin-1"), delimiter=";")
        next(rd)
        for row in rd:
            z = row[1].strip()
            if z not in kind or row[6].strip() not in ("2", "3", "4") or row[7].strip() != "w":
                continue
            if row[10].strip() != "-" or row[12].strip() != "-":
                continue
            h, r1, r2 = int(row[8]) - 1, int(row[9]), int(row[11])
            if r1 < 0 or r2 < 0:
                continue
            k, inb = kind[z]
            if k == "ring":
                sums.setdefault((z, "ring"), [0.0] * 24)[h] += (r1 + r2) / 2
            else:
                sums.setdefault((z, "inbound"), [0.0] * 24)[h] += r1 if inb == 1 else r2
                sums.setdefault((z, "outbound"), [0.0] * 24)[h] += r2 if inb == 1 else r1
    prof = {d: [0.0] * 24 for d in ("inbound", "outbound", "ring")}
    n = {d: 0 for d in prof}
    for (z, d), hs in sums.items():
        tot = sum(hs)
        if tot > 0 and all(v > 0 for v in hs[5:22]):
            for h in range(24):
                prof[d][h] += hs[h] / tot
            n[d] += 1
    rows = [{"hour": h, **{d: round(prof[d][h] / max(n[d], 1), 5) for d in prof}} for h in range(24)]
    write_csv(out / "traffic_profile.csv", ["hour", "inbound", "outbound", "ring"], rows)
    # peak-hour load per motorway: DTV per direction x peak share / (lanes per direction x capacity)
    peak_share = max(r["inbound"] for r in rows)
    st_rows, by_ref = [], {}
    for z, (k, inb) in kind.items():
        r = jawe[z]
        try:
            dtv = float(r["DTV_Kfz_W_Q"].replace(".", "").replace(",", ".")) if r["DTV_Kfz_W_Q"].strip() else None
            lanes = int(r["Anz_Fs_Q"])
        except ValueError:
            dtv, lanes = None, 0
        vc = round(dtv / 2 * peak_share / (lanes / 2 * CAP_PER_LANE["motorway"]), 3) if dtv and lanes else None
        ref = "A " + r["Str_Nr"].strip()
        st_rows.append({"station": z, "road": ref, "name": r["DZ_Name"].strip(), "lanes": lanes,
                        "dtv_weekday": int(dtv) if dtv else None, "kind": k, "peak_vc": vc})
        if vc:
            by_ref.setdefault(ref, []).append(vc)
    write_csv(out / "bast_stations.csv", ["station", "road", "name", "lanes", "dtv_weekday", "kind", "peak_vc"], st_rows)
    ref_vc = {ref: round(statistics.median(v), 3) for ref, v in by_ref.items()}
    print(f"  peak-hour load per motorway (median of stations): {ref_vc}")
    for t in ("TRAFFIC_PROFILE", "BAST_STATIONS"):
        sources.append({"table_name": t, "source": f"BASt automatic count stations, hourly values {year} (Tue-Thu working days)",
                        "license": "CC BY 4.0", "attribution": "Bundesanstalt für Straßenwesen (BASt)",
                        "url": BAST_HOURLY_URL.format(year=year), "retrieved_at": NOW})
    return ref_vc


def traffic_part(args, out: Path, sources, ref_vc: dict) -> None:
    """Capacity, peak load, direction, hotspot and roadworks per main road segment."""
    day = args.traffic_day
    hot = list(csv.DictReader(open(ROOT / "data" / "source" / "traffic_hotspots.csv", encoding="utf-8")))
    write_csv(out / "traffic_hotspots.csv", list(hot[0]), hot)
    segs = [r for r in csv.DictReader(open(out / "road_segments.csv", encoding="utf-8")) if r["road_class"] in MAIN_CLASSES]
    lines = [to_m(shp_wkt.loads(r["geometry_wkt"])) for r in segs]
    tree = STRtree(lines)
    # real roadworks that narrow or close the carriageway, valid on the simulated day
    works = {}
    lane_rx = re.compile(r"Fahrspur|Fahrstreifen|Einengung|eingeengt|halbseitig|Vollsperr|Straßensperr|"
                         r"Fahrbahneinschränkung|Fahrbahn gesperrt|Fahrbahnsperr", re.I)
    for r in csv.DictReader(open(out / "roadworks.csv", encoding="utf-8")):
        text = f"{r['impact']} {r['kind']}"
        if not (r["start_date"] <= day <= r["end_date"]) or not lane_rx.search(text) or not r.get("shape_wkt"):
            continue
        site = to_m(shp_wkt.loads(r["shape_wkt"]))
        if site.area > 200_000:                         # district-wide areas are no lane closure
            continue
        closed = re.search(r"Vollsperr|Straßensperrung(?!.*halbseitig)", text) is not None
        zone = site.buffer(10)
        for i in tree.query(zone, predicate="intersects"):
            if lines[int(i)].intersection(zone).length < 30:  # a side street that only touches the site
                continue
            f = 0.7 if closed else 0.85
            label = f"City roadworks {r['street']}: {r['impact'] or r['kind']}".strip()
            if works.get(int(i), (1.0,))[0] > f:
                works[int(i)] = (f, label[:200], "City of Munich")
    for r in csv.DictReader(open(out / "motorway_events.csv", encoding="utf-8")):
        if r["service"] != "roadworks" or r["future"] == "1" or not r["extent_wkt"]:
            continue
        geom = to_m(shp_wkt.loads(r["extent_wkt"]))
        for i in tree.query(geom.buffer(25), predicate="intersects"):
            s = segs[int(i)]
            if s["road_class"] != "motorway" or abs(((float(s["heading_deg"]) - _bearing(geom)) + 180) % 360 - 180) > 60:
                continue
            if works.get(int(i), (1.0,))[0] > 0.75:
                works[int(i)] = (0.75, f"Motorway roadworks {r['title']}"[:200], "Autobahn GmbH")
    rows = []
    cx, cy = CENTRE
    for i, r in enumerate(segs):
        rc = r["road_class"]
        base = rc.replace("_link", "")
        lon, lat = float(r["mid_lon"]), float(r["mid_lat"])
        oneway = r["oneway"] in ("1", "-1")
        try:
            lanes = int(r["lanes"])
            lanes = lanes if oneway else max(1, lanes // 2)
        except ValueError:
            lanes = 1 if rc.endswith("_link") else DEFAULT_LANES[base]
        cap = lanes * (LINK_CAP if rc.endswith("_link") else CAP_PER_LANE[base])
        free = float(r["maxspeed_kmh"]) if r["maxspeed_kmh"] else {"motorway": 120, "trunk": 70, "primary": 50,
                                                                   "secondary": 50, "tertiary": 50}[base]
        # direction relative to the centre: toward it (inbound), away (outbound), around it (ring)
        to_c = math.degrees(math.atan2((cx - lon) * 74300, (cy - lat) * 110540)) % 360
        off = abs((float(r["heading_deg"]) - to_c + 180) % 360 - 180)
        dist = math.hypot((cx - lon) * 74300, (cy - lat) * 110540)
        flow = "ring" if (60 <= off <= 120 or dist < 1500) else ("inbound" if off < 60 else "outbound")
        refs = [x.strip() for x in r["ref"].split(";") if x.strip()]
        if base == "motorway":
            vc = max([ref_vc.get(x, 0.0) for x in refs] + [0.0]) or 0.75
        else:
            vc = CITY_PEAK_VC[base]
        if rc.endswith("_link"):
            vc *= 0.8
        hid, hfac = "", 1.0
        for h in hot:
            roads = [x.strip() for x in h["roads"].split(";")]
            if not (set(refs) & set(roads) or r["name"] in roads):
                continue
            if math.hypot((float(h["lon"]) - lon) * 74300, (float(h["lat"]) - lat) * 110540) > float(h["radius_m"]):
                continue
            if h["direction"] == "inbound" and flow != "inbound":
                continue
            if float(h["demand_factor"]) > hfac:
                hid, hfac = h["hotspot_id"], float(h["demand_factor"])
        wf, wtext, wsrc = works.get(i, (1.0, "", ""))
        rows.append({"segment_id": r["segment_id"], "road_class": rc, "road": r["name"] or r["ref"], "lanes_dir": lanes,
                     "capacity_vph": cap, "free_kmh": free, "peak_vc": round(vc, 3), "flow_dir": flow,
                     "hotspot_id": hid, "hotspot_factor": hfac, "roadworks": wtext, "roadworks_source": wsrc,
                     "capacity_factor": wf})
    write_csv(out / "segment_traffic.csv", TRAFFIC_HEADER, rows)
    print(f"  {sum(1 for r in rows if r['hotspot_id'])} segments at hotspots, "
          f"{sum(1 for r in rows if r['roadworks'])} with roadworks on {day}")
    sources.append({"table_name": "TRAFFIC_HOTSPOTS", "source": "Research from INRIX, TomTom, ADAC Südbayern and press "
                    "(research/munich-congestion-hotspots.md)", "license": "facts with sources", "attribution": "see the source column",
                    "url": "research/munich-congestion-hotspots.md", "retrieved_at": NOW})
    sources.append({"table_name": "SEGMENT_TRAFFIC", "source": "Derived: OSM lanes and speed limits, BASt loads, hotspots, "
                    f"City of Munich and Autobahn GmbH roadworks valid on {day}", "license": "ODbL (derived from OSM)",
                    "attribution": "© OpenStreetMap contributors; BASt; City of Munich; Die Autobahn GmbH des Bundes",
                    "url": "scripts/prepare_road_data.py", "retrieved_at": NOW})


def _bearing(line) -> float:
    (x1, y1), (x2, y2) = line.coords[0], line.coords[-1]
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(Path.home() / "road-data"), help="flat output folder")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "munich-road-data"), help="download cache")
    ap.add_argument("--districts", default=str(Path(__file__).resolve().parent.parent / "data" / "munich-dataset" / "districts.csv"),
                    help="districts.csv of MUNICH_DATASET (for the city boundary)")
    ap.add_argument("--only", nargs="+", choices=["osm", "accidents", "weather", "autobahn", "traffic"],
                    help="build only these parts (accidents needs osm in the same run for the segment match)")
    ap.add_argument("--weather-from", default="2022-01-01", help="first day of weather values (default 2022-01-01)")
    ap.add_argument("--accident-from", type=int, default=2016, help="first accident year (default 2016)")
    ap.add_argument("--bast-year", type=int, default=2024, help="year of the BASt hourly counts (default 2024)")
    ap.add_argument("--traffic-day", default="2026-10-27", help="simulated day: roadworks valid on it (default 2026-10-27)")
    args = ap.parse_args()
    parts = set(args.only or ["osm", "accidents", "weather", "autobahn", "traffic"])

    out, cache = Path(args.out).expanduser(), Path(args.cache).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    districts, city = load_districts(Path(args.districts).expanduser())
    print(f"Writing to {out} (cache: {cache})")

    sources_path = out / "road_data_sources.csv"
    sources = []
    if sources_path.exists():  # keep the entries of parts not rebuilt in this run
        with open(sources_path, encoding="utf-8") as f:
            sources = list(csv.DictReader(f))

    rebuilt = {"osm": ("ROAD_SEGMENTS", "ROAD_AREAS"), "accidents": ("ACCIDENTS",),
               "weather": ("WEATHER_STATIONS", "WEATHER_10MIN", "WEATHER_VISIBILITY"), "autobahn": ("MOTORWAY_EVENTS",),
               "traffic": ("TRAFFIC_PROFILE", "BAST_STATIONS", "TRAFFIC_HOTSPOTS", "SEGMENT_TRAFFIC")}
    drop = {t for p in parts for t in rebuilt[p]}
    sources = [s for s in sources if s["table_name"] not in drop]

    munich = Path(args.districts).expanduser().parent
    for name, table in (("districts.csv", "DISTRICTS"), ("roadworks.csv", "ROADWORKS")):
        shutil.copyfile(munich / name, out / name)
        print(f"  {name:26s} copied from {munich}")
        sources = [s for s in sources if s["table_name"] != table] + [{
            "table_name": table, "source": "City of Munich open data portal (via the Munich dataset)",
            "license": "dl-de/by-2-0", "attribution": "Landeshauptstadt München, dl-de/by-2-0",
            "url": "https://opendata.muenchen.de", "retrieved_at": NOW}]

    seg_lines, study = [], None
    if "osm" in parts:
        seg_lines = osm_part(args, out, cache, districts, city, sources)
    if parts & {"accidents", "autobahn"}:
        areas = out / "road_areas.csv"
        if not areas.exists():
            sys.exit("error: road_areas.csv is missing; run with the osm part first")
        with open(areas, encoding="utf-8") as f:
            study = next(shp_wkt.loads(r["geometry_wkt"]) for r in csv.DictReader(f) if r["kind"] == "study_area")
    if "accidents" in parts:
        accidents_part(args, out, cache, districts, city, study, seg_lines, sources)
    if "weather" in parts:
        weather_part(args, out, cache, sources)
    if "autobahn" in parts:
        autobahn_part(args, out, study, sources)
    if "traffic" in parts:
        traffic_part(args, out, sources, bast_part(args, out, cache, sources))

    write_csv(sources_path, ["table_name", "source", "license", "attribution", "url", "retrieved_at"],
              sorted(sources, key=lambda s: s["table_name"]))
    print(f"Done. Load with:  scripts/load_road_data.sh road-safety-demo {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
