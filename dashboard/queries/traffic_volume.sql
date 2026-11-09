-- Background of the map: how busy each main road is at this hour, modelled from open data: the peak-hour
-- load of the segment (BASt counts for motorways, road class for city roads, known hotspots) times the
-- BASt hourly profile for its direction, against its capacity (real roadworks lower it). Per segment the
-- busier direction. Not measured: no vehicle positions are shared.
WITH n AS (SELECT "sim_now" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1),
prof AS (
    SELECT p."inbound" / mx.i AS f_in, p."outbound" / mx.o AS f_out, p."ring" / mx.r AS f_ring
    FROM ROAD_DATA.TRAFFIC_PROFILE p JOIN n ON p."hour" = HOUR(n."sim_now")
    CROSS JOIN (SELECT MAX("inbound") AS i, MAX("outbound") AS o, MAX("ring") AS r FROM ROAD_DATA.TRAFFIC_PROFILE) mx
)
SELECT t."segment_id", t."road_class",
       ROUND(t."peak_vc" * t."hotspot_factor" / t."capacity_factor"
             * GREATEST(CASE t."flow_dir" WHEN 'inbound' THEN p.f_in WHEN 'outbound' THEN p.f_out ELSE p.f_ring END,
                        CASE t."flow_dir" WHEN 'inbound' THEN p.f_out WHEN 'outbound' THEN p.f_in ELSE p.f_ring END), 2) AS "load",
       ROUND(t."peak_vc" * t."hotspot_factor" * t."capacity_vph"
             * CASE t."flow_dir" WHEN 'inbound' THEN p.f_in WHEN 'outbound' THEN p.f_out ELSE p.f_ring END) AS "vph"
FROM ROAD_DATA.SEGMENT_TRAFFIC t CROSS JOIN prof p
WHERE t."road_class" IN ('motorway', 'trunk', 'primary', 'secondary', 'motorway_link', 'trunk_link')
