-- The stream of the last 60 s, per provider.
WITH n AS (SELECT "run_id", "sim_now" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1)
SELECT v."provider_id", COUNT(*) AS "signals", COUNT(DISTINCT v."vehicle_id") AS "vehicles"
FROM ROAD_SAFETY.VEHICLE_SIGNALS v JOIN n ON v."run_id" = n."run_id"
WHERE v."ts" > ADD_SECONDS(n."sim_now", -60) AND v."ts" <= n."sim_now"
GROUP BY v."provider_id"
