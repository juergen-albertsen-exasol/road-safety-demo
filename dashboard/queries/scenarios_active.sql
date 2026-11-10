-- The events triggered on purpose that are running now (SCENARIO_LOG): drawn on the map as a dashed outline
-- marked SIMULATION, so the audience sees at once where something was started. Display only: the
-- combination (incidents_live.sql) never reads this table, it finds incidents only from what the partners send.
WITH n AS (SELECT "run_id" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1)
SELECT s."scenario_id", s."scenario_type", s."area_wkt", s."started_at"
FROM ROAD_SAFETY.SCENARIO_LOG s JOIN n ON s."run_id" = n."run_id"
WHERE s."ended_at" IS NULL
ORDER BY s."scenario_id"
