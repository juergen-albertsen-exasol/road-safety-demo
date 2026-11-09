-- The clock of the data: "now".
SELECT "run_id", "status", "sim_now", "sim_start", "speed", "weather_day", "vehicles", "updated_wall"
FROM ROAD_SAFETY.SIM_STATE
ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC
LIMIT 1
