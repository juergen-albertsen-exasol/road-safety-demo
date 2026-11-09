-- What arrived in the last 15 minutes, per provider: events (distinct event ids), messages, sensor readings.
WITH n AS (SELECT "run_id", "sim_now" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1)
SELECT e."provider", COUNT(DISTINCT e."event_id") AS "events", COUNT(*) AS "messages"
FROM ROAD_SAFETY.SRTI_EVENTS e JOIN n ON e."sim_run_id" = n."run_id"
WHERE e."detected_at" > ADD_MINUTES(n."sim_now", -15) AND e."detected_at" <= n."sim_now"
GROUP BY e."provider"
UNION ALL
SELECT 'SENSORS', 0, COUNT(*)
FROM ROAD_SAFETY.SENSOR_READINGS r JOIN n ON r."run_id" = n."run_id"
WHERE r."ts" > ADD_MINUTES(n."sim_now", -15) AND r."ts" <= n."sim_now"
