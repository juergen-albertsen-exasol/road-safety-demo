# Congestion hotspots in Munich, and open traffic data

Research for the traffic version of the road safety demo ([plan](../docs/road-safety-demo-plan.md)), 2026-11-06. **[V]** = checked on the source page, **[S]** = seen only in a search result. The INRIX corridor tables and some ADAC pages need JavaScript and could only be read in part.

## Hotspots

| # | Hotspot | Road | Direction / time | Reason | Evidence |
|---|---|---|---|---|---|
| 1 | Mittlerer Ring Süd, Stettnerstraße to Plinganserstraße (Brudermühl area) | B2R | northbound [S], evening, peak 17:00 [S] | Ring bottleneck with Isar crossing and Brudermühltunnel | Germany's no. 1 INRIX corridor 2021 and 2022, about 51 h/year [V] ([TRANSPORT, 10 Jan 2023](https://transport-online.de/news/inrix-trafic-scorecard-staus-der-stadt-sind-zeitfresser-vor-allem-muenchen-79746.html)) |
| 2 | Mittlerer Ring Süd, Brudermühl to Wolfratshauser Straße (toward Candidplatz) | B2R | - | Southern ring, many junctions | INRIX 2024: no. 2 in Germany, 42 h/year [V] ([Schwäbische/dpa, 7 Jan 2025](https://www.schwaebische.de/regional/bayern/muenchen-staut-sich-zur-arbeit-3219248)) |
| 3 | Heckenstallerstraße westbound | B2R | all day | Roadworks | INRIX: Germany's most congested segment, 97 h/week, year unknown [S] |
| 4 | Mittlerer Ring Nord, Lerchenauer Straße to Schwabinger Bach (Georg-Brauchle-Ring, Petuelring, Isarring) | B2R | evening [S] | Tunnels, crossing radial roads | INRIX 2024: 32 h/year [V] (same article) |
| 5 | Landshuter Allee | B2R west | rush hours | Very high volume, many ramps; bridge over Dachauer Straße with one lane until end of 2026, queues back up the ring | [web.de, 12 Jul 2024](https://web.de/magazine/auto/tempo-30-stau-muenchens-staureichster-strasse-39856756) [V]; [ADAC Südbayern, roadworks 2026](https://www.adac.de/der-adac/regionalclubs/suedbayern/news/muenchens-verkehrsnetz-am-limit-baustellen-2026/) [V] |
| 6 | Donnersbergerbrücke | B2R | - | About 150,000 vehicles/day, renovation closures | [S] |
| 7 | A99 ring: Kreuz München-Nord, Aschheim/Ismaning, Kirchheim to Hohenbrunn, Kreuz München-West | A99 | southbound Nord to Aschheim [S] | Interchanges; short incidents spread around the ring | 16,170 km of jams in 2025 [V] ([ADAC Südbayern Staubilanz 2025, 5 Feb 2026](https://presse.adac.de/regionalclubs/suedbayern/staubilanz2025.html)); most jams per 100 km in Germany [S] |
| 8 | A8 Ost, Salzburg to München | A8 | both | Volume; roadworks Perlach/Neubiberg and Hofolding 2026 [S] | 20,652 km in 2025, Bavaria no. 3 [V] (ADAC 2025) |
| 9 | A9 toward München, approach to Kreuz Neufahrn | A9 | southbound, into the city | Volume | 22,722 km in 2025, Bavaria no. 2 [V] (ADAC 2025) |
| 10 | Fürstenrieder Straße / A96 Laim | city road, A96 entrance | both, rush hours | Tram-Westtangente works cut capacity by about 70 % until about 2028 | [V] ADAC 2026 |
| 11 | A95 Kreuzhof and A995 Giesing to Kreuz München-Süd | A95, A995 | both, rush hours | Bridge works until Q2 2027, A995 pavement 2026 | [V] ADAC 2026 |

City-wide: TomTom (2024) city-centre congestion 55 % in the morning, 62 % in the evening, worst hour Thursday 17-18 h at 70 % [V, Schwäbische]. INRIX 2024: the last kilometres into the centre average 21 km/h [V]. INRIX 2025: 57 h lost per driver, Munich 5th in Germany [V] ([INRIX, 2 Dec 2025](https://inrix.com/press-releases/2025-global-traffic-scorecard-de/)).

No source found for: Leuchtenbergring, Luise-Kiesselbach-Platz, Effnerplatz, Tegernseer Landstraße, Leopoldstraße, Altstadtring, Rosenheimer Straße, Ungererstraße, A94, A96 entrance, A9 Fröttmaning. No source splits the morning and evening by direction; "inbound in the morning, outbound in the evening" is an assumption.

## Open traffic data

| Source | What | License | Notes |
|---|---|---|---|
| City of Munich, [Verkehrsmengenkarte 2022](https://stadt.muenchen.de/infos/verkehrsdaten.html) | Weekday traffic per main street up to the A99 | not stated | PDF map only, from about 500 counts (06-10 and 15-19 h, Tue-Thu) |
| opendata.muenchen.de | - | - | No vehicle counts or detector data found |
| BayernInfo / Mobilithek, [motorway sensors](https://mobilithek.info/offers/748580849261105152) | Speeds, volumes, weather, 1 minute, motorways in Bavaria | free, no license named | Live only |
| BayernInfo / Mobilithek, traffic reports and roadworks | DATEX II, all road classes in Bavaria | free | Live |
| [BASt automatic count stations](https://www.bast.de/DE/Themen/Digitales/HF_1/Massnahmen/verkehrszaehlung/Stundenwerte.html) | Hourly volumes 2003-2024, motorways and federal roads | no formal license; credit BASt | Best open source for a real rush-hour curve |
| BAYSIS road traffic census SVZ 2021 (WFS `BAYSIS_Verkehrsmengen`) | Average daily traffic per section of federal, state and district roads | CC BY 4.0, credit "BAYSIS" [S] | To be checked |
| INRIX, TomTom | Corridor rankings, city figures | proprietary | Evidence for where congestion forms, not data to load |

None of these gives open, ready-to-load hourly data for Munich city streets. Recommendation: the rush-hour curve from BASt hourly values (stations on the A99, A9, A8, A96 near Munich, Tue-Thu outside holidays), relative volume per road from BAYSIS SVZ 2021 where it covers the road and from the road class elsewhere, and the hotspots above as the places where capacity runs out.
