-- Who received the published warnings (topic srti.receipts, written by the consumers): per incident and
-- consumer the latest receipt, what they did (vehicles warned, the message sign switched), and how long it
-- took from publishing the first version to the first receipt.
WITH n AS (SELECT "run_id" FROM ROAD_SAFETY.SIM_STATE
           ORDER BY CASE WHEN "status" = 'running' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1),
r AS (
    SELECT w.*, ROW_NUMBER() OVER (PARTITION BY w."incident_id", w."consumer"
                                   ORDER BY w."version" DESC, w."received_at" DESC) AS rn
    FROM ROAD_SAFETY.WARNING_RECEIPTS w JOIN n ON w."sim_run_id" = n."run_id"
),
f AS (
    SELECT w."incident_id", MIN(SECONDS_BETWEEN(w."received_at", w."warning_sent_at")) AS first_secs
    FROM ROAD_SAFETY.WARNING_RECEIPTS w JOIN n ON w."sim_run_id" = n."run_id"
    WHERE w."version" = 1
    GROUP BY w."incident_id"
)
SELECT r."incident_id", r."consumer", r."version", r."status", r."vehicles", r."sign_lat", r."sign_lon",
       r."sign_road", r."sign_text", r."action", r."received_at", f.first_secs AS "first_secs"
FROM r LEFT JOIN f ON f."incident_id" = r."incident_id"
WHERE r.rn = 1
