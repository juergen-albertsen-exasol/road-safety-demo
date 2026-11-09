-- Every message the providers sent in the last {minutes} minutes, newest first (all versions: new, update,
-- cancel), as received through Kafka and parsed from INBOUND_MESSAGES.
WITH n AS (SELECT "run_id", "sim_now" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1)
SELECT e."detected_at", e."provider", e."status", e."version", e."event_id", e."srti_category", e."cause",
       e."confidence", e."observations", e."road", e."lat", e."lon", e."heading_deg", e."valid_until", e."sensor_id",
       e."topic", e."kafka_offset"
FROM ROAD_SAFETY.SRTI_EVENTS e JOIN n ON e."sim_run_id" = n."run_id"
WHERE e."detected_at" > ADD_MINUTES(n."sim_now", -{minutes!d}) AND e."detected_at" <= n."sim_now"
ORDER BY e."detected_at" DESC, e."kafka_offset" DESC
LIMIT {max_rows!d}
