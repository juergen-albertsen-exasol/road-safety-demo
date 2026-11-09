-- Infrastructure: the newest reading of every road sensor (placed at real places from OSM; warning trailers
-- of road works sites appear while the site is set up).
WITH n AS (SELECT "run_id", "sim_now" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1),
last_ AS (SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r."sensor_id" ORDER BY r."ts" DESC) AS rn
          FROM ROAD_SAFETY.SENSOR_READINGS r JOIN n ON r."run_id" = n."run_id"
          WHERE r."ts" > ADD_MINUTES(n."sim_now", -3) AND r."ts" <= n."sim_now")
SELECT i."sensor_id", i."sensor_type", i."location_kind", i."sensor_name", i."lat", i."lon", l."ts", l."surface_temp_c",
       l."friction", l."visibility_m", l."wrong_way_detected", l."stationary_vehicle", l."vehicle_count", l."avg_speed_kmh",
       l."works_active"
FROM ROAD_SAFETY.INFRA_SENSORS i LEFT JOIN last_ l ON l."sensor_id" = i."sensor_id" AND l.rn = 1
ORDER BY i."sensor_type", i."sensor_id"
