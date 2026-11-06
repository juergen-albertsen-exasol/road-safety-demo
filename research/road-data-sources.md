# Open data for the road safety demo

Research for step 1 of the [road safety demo plan](../docs/road-safety-demo-plan.md): which real open data the demo uses, where it comes from, its license, how current it is and what it can't tell us. Checked on 2026-11-06. "Verified" means the endpoint was called or the file was downloaded and read on that day.

The vehicle and sensor stream is synthetic (schema `ROAD_SAFETY`). Everything below is real.

## Summary

| Layer | Source | License | How current | Verified |
|---|---|---|---|---|
| Road network, forests and parks | OpenStreetMap, Geofabrik extract Oberbayern | ODbL 1.0 | Daily; checked file contains OSM data up to 2026-11-04 | Download page and state file |
| City boundary, districts | `DISTRICTS` from the Munich dataset, copied into `ROAD_DATA` | dl-de/by-2.0 | Current boundaries | Loaded |
| City roadworks | `ROADWORKS` from the Munich dataset (Munich open data, Baustellen), copied into `ROAD_DATA` | dl-de/by-2.0 | Snapshot of the dataset build, 5,702 rows | Loaded |
| Motorway roadworks, closures, warnings | Autobahn GmbH, offer "Die Autobahn App" on Mobilithek; JSON endpoint `verkehr.autobahn.de` | GeoNutzV (per the Mobilithek offer) | Live | Endpoint called for the A9 |
| Weather | DWD Climate Data Center, station 3379 München-Stadt (and 1262 München-Flughafen) | CC BY 4.0 | 10-minute values, historical up to 2025-12-31, "recent" up to yesterday | Files downloaded and read |
| Accident history | Unfallatlas of the Statistical Offices of the Federation and the Länder | dl-de/by-2.0 | Yearly, 2016–2025 for Bavaria | 2025 file downloaded and read |

## Road network: OpenStreetMap (Geofabrik)

- **Source:** https://download.geofabrik.de/europe/germany/bayern/oberbayern.html, file `oberbayern-latest.osm.pbf`, 247 MB.
- **License:** Open Database License 1.0. Attribution "© OpenStreetMap contributors". Derived databases that are shared must stay under ODbL.
- **How current:** updated daily. The checked file contains OSM data up to 2026-11-04 21:21 UTC.
- **What we take:** all `highway=*` ways for cars (motorway to residential, plus link roads) with `oneway`, `maxspeed`, `bridge`, `tunnel`, `name`, `ref`, split into segments. Clipped to the city boundary (union of `DISTRICTS`) **plus the full motorway ring** A8, A9, A92, A94, A95, A96, A99 with a buffer of about 2 km. Also `landuse=forest`, `natural=wood` and `leisure=park` edges (Forstenrieder Park, Perlacher Forst, Aubinger Lohe …) as the wildlife risk layer, and the Isar bridges (`bridge=yes` crossing `waterway=river` Isar) as the area for the slippery road scenario.
- **Tools:** neither `osmium` nor GDAL is installed. `prepare_road_data.py` uses `pyosmium` and `shapely` from the project `.venv`.
- **What it can't tell us:** traffic volumes, the condition of the road surface, or where sensors really are. The sensor positions in the demo are virtual, placed at real OSM features.

## City roadworks: `ROADWORKS` from the Munich dataset

- Munich open data portal, Baustellen, dl-de/by-2.0. 5,702 current and planned roadworks with dates, impact and centroid. Details are in [data/README.md](../data/README.md).
- **Limit:** a snapshot from when the dataset was built, not a live feed. It shows today's roadworks, not those of the simulated weather day (2025-01-10). The dashboard should say so.

## Motorways: Autobahn GmbH

- **Endpoint:** `https://verkehr.autobahn.de/o/autobahn/{road}/services/{roadworks|warning|closure}`. JSON, no key needed. Checked on the A9: 63 roadworks, 14 closures, 1 warning. Each item has coordinates, extent, title, direction, description and a `display_type` (e.g. `SHORT_TERM_ROADWORKS`, `CLOSURE_ENTRY_EXIT`, `WARNING`) and, for warnings, `abnormalTrafficType` and delay.
- **Official offer:** Mobilithek offer "Die Autobahn App", publisher Die Autobahn GmbH des Bundes (https://mobilithek.info/offers/573189053963235328, listed on GovData). License: **GeoNutzV** (Verordnung zur Festlegung der Nutzungsbestimmungen für die Bereitstellung von Geodaten des Bundes, 19 March 2013): free use, with the source named, e.g. "© Die Autobahn GmbH des Bundes".
- **Caveat:** the JSON endpoint itself carries no terms. Its documentation (https://autobahn.api.bund.dev, https://github.com/bundesAPI/autobahn-api) is a community project, not Autobahn GmbH. We use it as the technical access to the data the Mobilithek offer describes and name the source on the page. Not verified: whether the Mobilithek offer also provides a DATEX II feed of the same data.
- `SHORT_TERM_ROADWORKS` is a native category here, the same as the SRTI category in the demo.
- **What it can't tell us:** history. The endpoint shows only the current state, so the layer shows today's motorway events, like the city roadworks. Polling it live during the demo is possible; caching a snapshot before the talk is the safer fallback.

## Weather: DWD Climate Data Center

- **Source:** https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/
- **License:** CC BY 4.0 (Terms_of_use.txt, status May 2024). Attribution "Source: Deutscher Wetterdienst".
- **Stations:** 3379 München-Stadt (48.1632 N, 11.5429 E, 515 m) for the city; 1262 München-Flughafen for the north (fog scenario). Both have:
  - 10-minute: air temperature, temperature 5 cm above the ground, humidity, dew point (`air_temperature`), precipitation (`precipitation`), wind (`wind`)
  - hourly: visibility (`visibility`), weather phenomena (`weather_phenomena`)
- **Time zone:** 10-minute values since 2000 are in **UTC**. CET is UTC+1.
- **The chosen day, 2025-01-10 (times in CET):**
  - 9 Jan: mild afternoon around 12 °C. A cold front passes at about 19:00: the air drops to 5 °C in 30 minutes and 4.7 mm of rain falls between 19:00 and 20:10.
  - Overnight the air stays just above 0 °C, but 5 cm above the ground it is below zero from about 00:30, coldest at 06:30–07:00 (-1.6 °C) and back above zero at about 09:00.
  - Visibility stays above 40 km all morning: no fog that day.
  - So: wet roads, then a surface frost exactly in the morning rush hour, with the air near 0 °C, when bridges freeze before the other roads. **The simulation clock starts at 2025-01-10 06:30 CET.**
- **Fog:** dense fog mornings are rare at the city station. Since 2022, only two mornings had at least 4 hours below 200 m between 05:00 and 10:00 CET: 2022-11-12 (130 m) and 2024-10-30 (120 m). The airport station 1262 has fog more often and is closer to the scenario area "north of the city". The fog scenario takes visibility from one of these days, not from the main weather day.
- **Weather warnings:** DWD publishes warnings as open data too (CAP), but only current ones, not for 2025-01-10. Not used for now.
- **What it can't tell us:** the condition of the road surface. The 5 cm temperature over grass at the station is the closest proxy; bridge decks and shaded streets are colder. The demo's road weather sensors are virtual.

## Accident history: Unfallatlas

- **Source:** https://www.opengeodata.nrw.de/produkte/transport_verkehr/unfallatlas/, one CSV per year (`Unfallorte{year}_EPSG25832_CSV.zip`), 2016–2025. Data description: `DSB_Unfallatlas.pdf` (status 10 June 2025).
- **License:** Datenlizenz Deutschland – Namensnennung – Version 2.0 ("Diese Daten stehen unter der Datenlizenz Deutschland Namensnennung 2.0", NRW download page). Attribution: "Statistische Ämter des Bundes und der Länder, Unfallatlas, dl-de/by-2-0 (www.govdata.de/dl-de/by-2-0)", with the dataset URL and a note that the data was changed (clipped and aggregated).
- **Coverage:** accidents with personal injury only, with the place snapped to the road. Bavaria from 2016. Munich is `ULAND = 09, UREGBEZ = 1, UKREIS = 62`.
- **Checked 2025:** 5,264 accidents in Munich. Road condition (`IstStrassenzustand`): 4,262 dry (0), 957 wet, damp or slippery (1), 45 icy or snowy (2, "winterglatt").
- **Useful fields:** year, month, hour, weekday, category (killed, seriously, slightly injured), type, light conditions, road condition, who was involved (bicycle, car, pedestrian, motorcycle, goods vehicle, other), WGS84 coordinates.
- **Use:** accident hotspots per segment, and as a real check for the slippery road story: where in Munich did "winterglatt" accidents happen in 2016–2025? If the Isar bridges show up there, that's a good line for the talk.
- **What it can't tell us:** accidents with property damage only, the exact day (only month, hour and weekday), and near misses.

## Not used

- **DFRS data itself** (Data for Road Safety, Mobility Data Space): not published under an open license. That's why the vehicle and sensor stream is synthetic.
- **Live traffic volumes:** the Bavarian counting stations (open.bydata, "Automatische Zählstellen") are yearly aggregates, not live, and not needed for the demo.

## Attribution line for the dashboard

"Road network © OpenStreetMap contributors (ODbL). Roadworks: City of Munich (dl-de/by-2.0) and © Die Autobahn GmbH des Bundes. Weather: Deutscher Wetterdienst (CC BY 4.0). Accidents: Statistische Ämter des Bundes und der Länder, Unfallatlas (dl-de/by-2-0). Vehicle and sensor data is synthetic and marked as simulation."
