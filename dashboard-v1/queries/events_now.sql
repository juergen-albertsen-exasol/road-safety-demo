-- The SRTI events that are valid now: the current version of every event the providers sent in the
-- last {window_min} minutes, not withdrawn (cancel) and not expired. One dot each on the map.
WITH n AS (SELECT "run_id", "sim_now" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1),
ev AS (
    SELECT e.*, ROW_NUMBER() OVER (PARTITION BY e."event_id" ORDER BY e."version" DESC, e."kafka_offset" DESC) AS rn,
           f."first_at"
    FROM ROAD_SAFETY.SRTI_EVENTS e JOIN n ON e."sim_run_id" = n."run_id"
    JOIN (SELECT x."event_id", MIN(x."detected_at") AS "first_at"   -- the first report, also when it is older
          FROM ROAD_SAFETY.SRTI_EVENTS x JOIN n m ON x."sim_run_id" = m."run_id"
          GROUP BY x."event_id") f ON f."event_id" = e."event_id"
    WHERE e."detected_at" > ADD_MINUTES(n."sim_now", -{window_min!d}) AND e."detected_at" <= n."sim_now"
)
SELECT ev."event_id", ev."version", ev."provider", ev."srti_category", ev."cause", ev."confidence", ev."observations",
       ev."lat", ev."lon", ev."heading_deg", ev."road", ev."first_at", ev."detected_at", ev."valid_until", ev."sensor_id",
       ev."topic"
FROM ev JOIN n ON ev."valid_until" >= n."sim_now"
WHERE ev.rn = 1 AND ev."status" <> 'cancel'
