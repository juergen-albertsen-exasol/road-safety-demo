-- The newest warnings published to srti.confirmed (OUTBOUND_MESSAGES): what was passed on, and when.
WITH n AS (SELECT "run_id" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1)
SELECT o."sim_at", o."status", o."incident_id", o."version", o."topic", o."kafka_offset",
       i."srti_category", i."road", i."cause"
FROM ROAD_SAFETY.OUTBOUND_MESSAGES o
JOIN ROAD_SAFETY.INCIDENTS i ON i."incident_id" = o."incident_id"
JOIN n ON i."run_id" = n."run_id"
ORDER BY o."sent_wall" DESC
LIMIT {max_rows!d}
