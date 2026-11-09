-- Combination: SRTI events from several providers and the road operator's sensor alerts -> incidents.
-- A template with pyexasol placeholders. scripts/road_safety_service.py runs it with the defaults every
-- few seconds and keeps the confirmed rows as INCIDENTS; the dashboard runs it with its own sliders.
--
-- Placeholders (defaults in brackets):
--   {min_providers!d}     different providers (carmakers, navigation service) needed [2]
--   {sensor_replaces!d}   1: a road operator sensor counts as one of them [1]
--   {window_min!d}        reports considered, minutes back from the simulation's "now" [30]
--   {heading_max!d}       directional categories: headings may differ by at most this many degrees [60]
--   {friction_max!f}      a road weather station below this friction (at or below 0 C) alerts [0.4]
--   {visibility_max!d}    a road weather station below this visibility in metres alerts [150]
--   {blockage_after_s!d}  an accident area without a road operator report becomes an unmanaged blockage
--                         after this many seconds [600]
--   {reach_min!d}         reach of a spot: vehicles passing it in this many minutes [10]
--
-- Reads only what partners send (SRTI_EVENTS, SENSOR_READINGS), the sensor list, SIM_STATE for "now"
-- and the open data in ROAD_DATA. Never SCENARIO_LOG: combination doesn't know the ground truth.
--
-- How it works:
-- 1. pts: every report that is valid now: the current version of each SRTI event (not cancelled, not
--    expired), and every road operator sensor whose latest reading alerts (low friction on a frozen
--    surface, poor visibility, a wrong-way vehicle, a stationary vehicle, road works set up).
-- 2. rules: per SRTI category the radius in which reports are about the same thing, whether the
--    driving direction must match, and whether the road operator alone can confirm it (its warning
--    trailer for road works, its wrong-way cameras).
-- 3. Grouping: two reports belong together when they have the same category, are within the radius
--    and (for directional categories) head the same way. Every report joins the group of its
--    earliest neighbour.
-- 4. Confirmed: min_providers different providers, or one fewer plus a road operator sensor, or the
--    road operator alone where the rule allows it. Several reports of one provider count once.
-- 5. Cause (slippery road): from the providers (ice, fluid), else a road weather station (ice), else
--    the DWD air temperature (at or below 1 C: ice). Confidence: 1 - product of (1 - confidence) of
--    the reports. An accident area without a road operator report becomes an unmanaged blockage.
-- 6. Reach, from the traffic volume on the open data (ROAD_DATA.SEGMENT_TRAFFIC, BASt hourly
--    profile): for a spot, the vehicles that pass it in the next reach_min minutes on the nearest
--    road segment in its direction; for fog and storms, the vehicles in the area now.
WITH now_ AS (
    SELECT "run_id" AS run_id, "sim_now" AS sim_now
    FROM ROAD_SAFETY.SIM_STATE
    ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC
    LIMIT 1
),
rules AS (
    SELECT CAST('temporary_slippery_road' AS VARCHAR(40)) AS cat, 300 AS radius_m, 1 AS directional,
           0 AS sensor_alone, CAST('spot' AS VARCHAR(10)) AS reach_kind FROM DUAL
    UNION ALL SELECT 'obstacle_on_road', 150, 1, 0, 'spot' FROM DUAL
    UNION ALL SELECT 'unprotected_accident_area', 250, 1, 0, 'spot' FROM DUAL
    UNION ALL SELECT 'short_term_road_works', 400, 1, 1, 'spot' FROM DUAL
    UNION ALL SELECT 'wrong_way_driver', 2500, 1, 1, 'spot' FROM DUAL
    UNION ALL SELECT 'reduced_visibility', 5000, 0, 0, 'area' FROM DUAL
    UNION ALL SELECT 'exceptional_weather', 6000, 0, 0, 'area' FROM DUAL
),
ev AS (   -- every message of this run in the window; rn = 1 is the current version of an event
    SELECT e."event_id", e."version", e."status", e."provider", e."srti_category", e."cause", e."confidence",
           e."lat", e."lon", e."heading_deg", e."road", e."detected_at", e."valid_until", e."sensor_id",
           ROW_NUMBER() OVER (PARTITION BY e."event_id" ORDER BY e."version" DESC, e."kafka_offset" DESC) AS rn,
           MIN(e."detected_at") OVER (PARTITION BY e."event_id") AS first_at
    FROM ROAD_SAFETY.SRTI_EVENTS e
    JOIN now_ n ON e."sim_run_id" = n.run_id
    WHERE e."detected_at" > ADD_MINUTES(n.sim_now, -{window_min!d}) AND e."detected_at" <= n.sim_now
),
readings AS (   -- sensor readings in the window, with the alert each one raises
    SELECT r."sensor_id", r."ts",
           CASE WHEN r."friction" < {friction_max!f} AND r."surface_temp_c" <= 0 THEN 'temporary_slippery_road'
                WHEN r."visibility_m" < {visibility_max!d} THEN 'reduced_visibility'
                WHEN r."wrong_way_detected" THEN 'wrong_way_driver'
                WHEN r."stationary_vehicle" THEN 'unprotected_accident_area'
                WHEN r."works_active" THEN 'short_term_road_works' END AS cat,
           ROW_NUMBER() OVER (PARTITION BY r."sensor_id" ORDER BY r."ts" DESC) AS rn
    FROM ROAD_SAFETY.SENSOR_READINGS r
    JOIN now_ n ON r."run_id" = n.run_id
    WHERE r."ts" > ADD_MINUTES(n.sim_now, -{window_min!d}) AND r."ts" <= n.sim_now
),
alarms AS (   -- sensors whose latest reading (at most 3 minutes old) alerts, and since when
    SELECT cur."sensor_id", cur.cat, cur."ts" AS last_at,
           (SELECT MIN(x."ts") FROM readings x WHERE x."sensor_id" = cur."sensor_id" AND x.cat = cur.cat) AS first_at
    FROM readings cur
    JOIN now_ n ON cur."ts" >= ADD_SECONDS(n.sim_now, -180)
    WHERE cur.rn = 1 AND cur.cat IS NOT NULL
),
pts AS (
    SELECT 'e:' || ev."event_id" AS id, ev."event_id" AS report_id, CAST('event' AS VARCHAR(10)) AS src,
           ev."provider" AS provider, CASE WHEN ev."provider" = 'ROAD_OP' THEN 1 ELSE 0 END AS is_sensor,
           COALESCE(ev."sensor_id", CASE WHEN ev."provider" = 'ROAD_OP' THEN ev."event_id" END) AS sensor_key,
           ev."srti_category" AS cat, ev."cause" AS cause, ev."lat" AS lat, ev."lon" AS lon,
           CAST(ev."heading_deg" AS DECIMAL(6,1)) AS hdg, ev."road" AS road,
           LEAST(COALESCE(ev."confidence", 0.5), 0.99) AS conf, ev.first_at, ev."detected_at" AS last_at,
           ev."valid_until" AS valid_until
    FROM ev JOIN now_ n ON ev."valid_until" >= n.sim_now
    WHERE ev.rn = 1 AND ev."status" <> 'cancel'
    UNION ALL
    SELECT 's:' || a."sensor_id" || ':' || a.cat, 'sensor:' || a."sensor_id" || ':' || a.cat, 'sensor',
           'ROAD_OP', 1, a."sensor_id", a.cat,
           CASE WHEN a.cat = 'temporary_slippery_road' THEN 'ice' END,
           CAST(s."lat" AS DOUBLE), CAST(s."lon" AS DOUBLE), CAST(NULL AS DECIMAL(6,1)), CAST(NULL AS VARCHAR(200)),
           0.9, a.first_at, a.last_at, ADD_SECONDS(a.last_at, 180)
    FROM alarms a JOIN ROAD_SAFETY.INFRA_SENSORS s ON s."sensor_id" = a."sensor_id"
),
nb AS (   -- pairs of reports about the same thing
    SELECT a.id AS a_id, b.id AS b_id, b.first_at AS b_first
    FROM pts a
    JOIN pts b ON b.cat = a.cat
    JOIN rules r ON r.cat = a.cat
    WHERE SQRT(POWER((b.lon - a.lon) * 74320, 2) + POWER((b.lat - a.lat) * 110540, 2)) <= r.radius_m
      AND (r.directional = 0 OR a.hdg IS NULL OR b.hdg IS NULL
           OR ABS(MOD(a.hdg - b.hdg + 540, 360) - 180) <= {heading_max!d})
),
anchor AS (   -- every report joins the group of its earliest neighbour
    SELECT a_id, MIN(TO_CHAR(b_first, 'YYYY-MM-DD HH24:MI:SS') || '|' || b_id) AS k
    FROM nb GROUP BY a_id
),
grp AS (
    SELECT p.*, SUBSTR(x.k, INSTR(x.k, '|') + 1) AS anchor_id
    FROM pts p JOIN anchor x ON x.a_id = p.id
),
cl AS (
    SELECT g.anchor_id, g.cat,
           COUNT(DISTINCT CASE WHEN g.is_sensor = 0 THEN g.provider END) AS providers,
           COUNT(DISTINCT CASE WHEN g.is_sensor = 1 THEN g.sensor_key END) AS sensors,
           COUNT(*) AS reports,
           GROUP_CONCAT(DISTINCT CASE WHEN g.is_sensor = 0 THEN g.provider END ORDER BY 1 SEPARATOR ',') AS provider_list,
           GROUP_CONCAT(DISTINCT CASE WHEN g.is_sensor = 1 THEN g.sensor_key END ORDER BY 1 SEPARATOR ',') AS sensor_list,
           GROUP_CONCAT(g.report_id ORDER BY g.first_at SEPARATOR ',') AS members,
           AVG(g.lat) AS lat, AVG(g.lon) AS lon,
           MAX(CASE WHEN g.id = g.anchor_id THEN g.hdg END) AS hdg,
           MAX(g.road) AS road,
           MIN(g.first_at) AS first_at, MAX(g.last_at) AS last_at, MAX(g.valid_until) AS valid_until,
           1 - EXP(SUM(LN(1 - g.conf))) AS conf,
           MAX(CASE WHEN g.is_sensor = 0 THEN g.cause END) AS provider_cause,
           MAX(g.cause) AS any_cause
    FROM grp g
    GROUP BY g.anchor_id, g.cat
),
wx AS (   -- the DWD air temperature now
    SELECT w."air_temp_c" AS air_temp
    FROM ROAD_DATA.WEATHER_10MIN w JOIN now_ n ON w."ts_local" <= n.sim_now
    WHERE w."station_id" = 3379
    ORDER BY w."ts_local" DESC
    LIMIT 1
),
res AS (
    SELECT c.*, r.radius_m, r.reach_kind,
           CASE WHEN c.providers >= {min_providers!d} THEN 'providers'
                WHEN {sensor_replaces!d} = 1 AND c.sensors >= 1 AND c.providers >= {min_providers!d} - 1 THEN 'providers_and_sensor'
                WHEN r.sensor_alone = 1 AND c.sensors >= 1 THEN 'road_operator' END AS rule_used,
           CASE WHEN c.cat = 'unprotected_accident_area' AND c.sensors = 0
                     AND SECONDS_BETWEEN(n.sim_now, c.first_at) >= {blockage_after_s!d} THEN 'unmanaged_blockage'
                ELSE c.cat END AS srti_category,
           CASE WHEN c.cat = 'temporary_slippery_road'
                     THEN COALESCE(c.provider_cause, c.any_cause, CASE WHEN wx.air_temp <= 1 THEN 'ice' END)
                WHEN c.cat = 'obstacle_on_road' THEN c.provider_cause END AS cause_out
    FROM cl c
    JOIN rules r ON r.cat = c.cat
    CROSS JOIN now_ n
    LEFT JOIN wx ON 1 = 1
),
prof AS (   -- share of the peak-hour traffic at this hour, per direction of travel (BASt profile)
    SELECT p."inbound" / mx.i AS f_in, p."outbound" / mx.o AS f_out, p."ring" / mx.r AS f_ring
    FROM ROAD_DATA.TRAFFIC_PROFILE p
    JOIN now_ n ON p."hour" = HOUR(n.sim_now)
    CROSS JOIN (SELECT MAX("inbound") AS i, MAX("outbound") AS o, MAX("ring") AS r FROM ROAD_DATA.TRAFFIC_PROFILE) mx
),
seg AS (
    SELECT t."segment_id", CAST(s."mid_lat" AS DOUBLE) AS lat, CAST(s."mid_lon" AS DOUBLE) AS lon,
           CAST(s."heading_deg" AS DECIMAL(6,1)) AS hdg, s."oneway" AS oneway, s."length_m" AS len_m,
           GREATEST(t."free_kmh" * 0.8, 10) AS kmh, t."peak_vc" * t."hotspot_factor" * t."capacity_vph" AS peak_flow,
           t."flow_dir" AS dir
    FROM ROAD_DATA.SEGMENT_TRAFFIC t
    JOIN ROAD_DATA.ROAD_SEGMENTS s ON s."segment_id" = t."segment_id"
),
flows AS (   -- per incident and nearby segment: vehicles per hour with and against the segment's direction
    SELECT r.anchor_id, r.reach_kind, g.len_m, g.kmh,
           SQRT(POWER((g.lon - r.lon) * 74320, 2) + POWER((g.lat - r.lat) * 110540, 2)) AS dist_m,
           CASE WHEN r.hdg IS NULL THEN 1 WHEN ABS(MOD(g.hdg - r.hdg + 540, 360) - 180) <= 60 THEN 1 ELSE 0 END AS fwd_ok,
           CASE WHEN r.hdg IS NULL THEN 1 WHEN ABS(MOD(g.hdg + 180 - r.hdg + 540, 360) - 180) <= 60 THEN 1 ELSE 0 END AS bwd_ok,
           CASE WHEN g.oneway IN (0, 1) THEN g.peak_flow * CASE g.dir WHEN 'inbound' THEN p.f_in WHEN 'outbound' THEN p.f_out ELSE p.f_ring END
                ELSE 0 END AS fwd_vph,
           CASE WHEN g.oneway IN (0, -1) THEN g.peak_flow * CASE g.dir WHEN 'inbound' THEN p.f_out WHEN 'outbound' THEN p.f_in ELSE p.f_ring END
                ELSE 0 END AS bwd_vph
    FROM res r
    CROSS JOIN prof p
    JOIN seg g ON ABS(g.lat - r.lat) * 110540 <= r.radius_m + 200 AND ABS(g.lon - r.lon) * 74320 <= r.radius_m + 200
),
reach AS (
    SELECT anchor_id,
           CASE WHEN MAX(reach_kind) = 'area'
                THEN SUM(CASE WHEN dist_m <= 3000 THEN (fwd_vph + bwd_vph) / kmh * len_m / 1000 END)
                ELSE SUBSTR(MIN(CASE WHEN dist_m <= 150 AND fwd_ok * fwd_vph + bwd_ok * bwd_vph > 0
                                     THEN LPAD(TO_CHAR(ROUND(dist_m)), 6, '0') || '|'
                                          || TO_CHAR(ROUND(fwd_ok * fwd_vph + bwd_ok * bwd_vph))
                                END), 8) * {reach_min!d} / 60 END AS vehicles
    FROM flows
    GROUP BY anchor_id
)
SELECT r.anchor_id AS "incident_key", r.srti_category AS "srti_category", r.cat AS "base_category",
       r.cause_out AS "cause", r.rule_used IS NOT NULL AS "confirmed", r.rule_used AS "rule",
       ROUND(r.lat, 6) AS "lat", ROUND(r.lon, 6) AS "lon", r.hdg AS "heading_deg", r.road AS "road",
       r.first_at AS "first_report_at", r.last_at AS "last_report_at", r.valid_until AS "valid_until",
       r.providers AS "providers", r.provider_list AS "provider_list", r.sensors AS "sensors",
       r.sensor_list AS "sensor_list", r.reports AS "reports", r.members AS "members",
       ROUND(r.conf, 2) AS "confidence", ROUND(x.vehicles) AS "reach_vehicles",
       CASE WHEN r.reach_kind = 'area' THEN 'in_area' ELSE 'pass_next_min' END AS "reach_kind",
       r.radius_m AS "radius_m"
FROM res r
LEFT JOIN reach x ON x.anchor_id = r.anchor_id
ORDER BY r.rule_used IS NULL, r.first_at
