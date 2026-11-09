# data/source

Small inputs kept in git. Everything else is downloaded by `scripts/prepare_road_data.py` into `data/road-data/` (gitignored).

| File | Content | Source | License |
|---|---|---|---|
| `districts.csv` | The 25 city districts (Stadtbezirke) of Munich: number, name, residents (end of 2024), area, centroid and boundary polygon (WKT, WGS84). Their union is the city boundary. | City of Munich open data portal (opendata.muenchen.de): district boundaries, and residents from the *Indikatorenatlas* | Datenlizenz Deutschland – Namensnennung 2.0 (dl-de/by-2.0), "Landeshauptstadt München" |
| `roadworks.csv` | 5,702 current and planned city roadworks with street, impact, start and end date, centroid and district. Snapshot from 2026-10-21. | City of Munich open data portal, *Baustellen* | dl-de/by-2.0, "Landeshauptstadt München" |
| `traffic_hotspots.csv` | Known congestion hotspots in and around Munich, with the source of each entry | Researched for this demo, see [research/munich-congestion-hotspots.md](../../research/munich-congestion-hotspots.md) | Facts with sources |

`prepare_road_data.py` copies `districts.csv` and `roadworks.csv` unchanged into the output folder, so they load as `ROAD_DATA.DISTRICTS` and `ROAD_DATA.ROADWORKS`.

**Roadworks are a snapshot.** The simulation day is 2026-10-27 by default, close to the snapshot. For a talk on another day, replace `roadworks.csv` with a current export of the *Baustellen* dataset in the same columns (coordinates in WGS84, `district_no` by point-in-polygon against `districts.csv`).
