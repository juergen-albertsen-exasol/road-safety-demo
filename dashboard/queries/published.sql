-- The incidents the service confirmed and published (INCIDENTS), active ones and those cleared in the last
-- 30 minutes, with how many warnings went out for each.
WITH n AS (SELECT "run_id", "sim_now" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1)
SELECT i.*, (SELECT COUNT(*) FROM ROAD_SAFETY.OUTBOUND_MESSAGES o WHERE o."incident_id" = i."incident_id") AS "messages"
FROM ROAD_SAFETY.INCIDENTS i JOIN n ON i."run_id" = n."run_id"
WHERE i."status" = 'active' OR i."cleared_at" >= ADD_MINUTES(n."sim_now", -30)
ORDER BY i."confirmed_at" DESC
