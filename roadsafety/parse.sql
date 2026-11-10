-- Parse new messages from INBOUND_MESSAGES (raw JSON, as received from Kafka) into SRTI_EVENTS,
-- SENSOR_READINGS and WARNING_RECEIPTS (the consumers' receipts for the published warnings). Run by scripts/road_safety_service.py every few seconds, statement by statement.
--
-- PARSE_STATE keeps, per topic id and partition, the offset parsed up to (last_offset). Each round
-- first fixes the upper end (next_offset = what is there now), parses everything in between, then
-- moves last_offset up. Messages arriving meanwhile wait for the next round: nothing is parsed twice
-- or skipped. The field names are those of scripts/srti_events.py.

MERGE INTO ROAD_SAFETY.PARSE_STATE p
USING (SELECT "topic_id", "kafka_partition", MAX("kafka_offset") AS mx
       FROM ROAD_SAFETY.INBOUND_MESSAGES GROUP BY "topic_id", "kafka_partition") m
ON p."topic_id" = m."topic_id" AND p."kafka_partition" = m."kafka_partition"
WHEN MATCHED THEN UPDATE SET p."next_offset" = m.mx
WHEN NOT MATCHED THEN INSERT VALUES (m."topic_id", m."kafka_partition", -1, m.mx);

INSERT INTO ROAD_SAFETY.SRTI_EVENTS
SELECT m."topic", m."topic_id", m."kafka_partition", m."kafka_offset",
       JSON_VALUE(m."payload", '$.event_id'),
       JSON_VALUE(m."payload", '$.version' RETURNING DECIMAL(9,0)),
       JSON_VALUE(m."payload", '$.status'),
       JSON_VALUE(m."payload", '$.provider'),
       JSON_VALUE(m."payload", '$.srti_category'),
       JSON_VALUE(m."payload", '$.datex_record_type'),
       JSON_VALUE(m."payload", '$.datex_type'),
       JSON_VALUE(m."payload", '$.cause'),
       JSON_VALUE(m."payload", '$.confidence' RETURNING DOUBLE),
       JSON_VALUE(m."payload", '$.probability_of_occurrence'),
       JSON_VALUE(m."payload", '$.observations' RETURNING DECIMAL(9,0)),
       JSON_VALUE(m."payload", '$.lat' RETURNING DOUBLE),
       JSON_VALUE(m."payload", '$.lon' RETURNING DOUBLE),
       JSON_VALUE(m."payload", '$.heading_deg' RETURNING DOUBLE),
       JSON_VALUE(m."payload", '$.road'),
       CAST(REPLACE(SUBSTR(JSON_VALUE(m."payload", '$.detected_at'), 1, 19), 'T', ' ') AS TIMESTAMP),
       CAST(REPLACE(SUBSTR(JSON_VALUE(m."payload", '$.valid_until'), 1, 19), 'T', ' ') AS TIMESTAMP),
       JSON_VALUE(m."payload", '$.sensor_id'),
       JSON_VALUE(m."payload", '$.sim_run_id' RETURNING DECIMAL(9,0)),
       CURRENT_TIMESTAMP
FROM ROAD_SAFETY.INBOUND_MESSAGES m
JOIN ROAD_SAFETY.PARSE_STATE p ON p."topic_id" = m."topic_id" AND p."kafka_partition" = m."kafka_partition"
WHERE m."kafka_offset" > p."last_offset" AND m."kafka_offset" <= p."next_offset"
  AND JSON_VALUE(m."payload", '$.message_type') = 'srti_event';

INSERT INTO ROAD_SAFETY.SENSOR_READINGS
SELECT JSON_VALUE(m."payload", '$.sim_run_id' RETURNING DECIMAL(9,0)),
       CAST(REPLACE(SUBSTR(JSON_VALUE(m."payload", '$.ts'), 1, 19), 'T', ' ') AS TIMESTAMP),
       JSON_VALUE(m."payload", '$.sensor_id'),
       JSON_VALUE(m."payload", '$.surface_temp_c' RETURNING DECIMAL(4,1)),
       JSON_VALUE(m."payload", '$.friction' RETURNING DECIMAL(3,2)),
       JSON_VALUE(m."payload", '$.visibility_m' RETURNING DECIMAL(6,0)),
       JSON_VALUE(m."payload", '$.wrong_way_detected' RETURNING BOOLEAN),
       JSON_VALUE(m."payload", '$.stationary_vehicle' RETURNING BOOLEAN),
       JSON_VALUE(m."payload", '$.vehicle_count' RETURNING DECIMAL(5,0)),
       JSON_VALUE(m."payload", '$.avg_speed_kmh' RETURNING DECIMAL(4,0)),
       CURRENT_TIMESTAMP,
       JSON_VALUE(m."payload", '$.works_active' RETURNING BOOLEAN)
FROM ROAD_SAFETY.INBOUND_MESSAGES m
JOIN ROAD_SAFETY.PARSE_STATE p ON p."topic_id" = m."topic_id" AND p."kafka_partition" = m."kafka_partition"
WHERE m."kafka_offset" > p."last_offset" AND m."kafka_offset" <= p."next_offset"
  AND JSON_VALUE(m."payload", '$.message_type') = 'sensor_reading';

INSERT INTO ROAD_SAFETY.WARNING_RECEIPTS
SELECT m."topic", m."topic_id", m."kafka_partition", m."kafka_offset",
       JSON_VALUE(m."payload", '$.consumer'),
       JSON_VALUE(m."payload", '$.consumer_group'),
       JSON_VALUE(m."payload", '$.incident_id'),
       JSON_VALUE(m."payload", '$.version' RETURNING DECIMAL(9,0)),
       JSON_VALUE(m."payload", '$.status'),
       JSON_VALUE(m."payload", '$.srti_category'),
       JSON_VALUE(m."payload", '$.warning_offset' RETURNING DECIMAL(36,0)),
       CAST(REPLACE(SUBSTR(JSON_VALUE(m."payload", '$.warning_sent_at'), 1, 23), 'T', ' ') AS TIMESTAMP),
       CAST(REPLACE(SUBSTR(JSON_VALUE(m."payload", '$.received_at'), 1, 23), 'T', ' ') AS TIMESTAMP),
       JSON_VALUE(m."payload", '$.vehicles' RETURNING DECIMAL(9,0)),
       JSON_VALUE(m."payload", '$.sign_lat' RETURNING DOUBLE),
       JSON_VALUE(m."payload", '$.sign_lon' RETURNING DOUBLE),
       JSON_VALUE(m."payload", '$.sign_road'),
       JSON_VALUE(m."payload", '$.sign_text'),
       JSON_VALUE(m."payload", '$.action'),
       JSON_VALUE(m."payload", '$.sim_run_id' RETURNING DECIMAL(9,0)),
       CURRENT_TIMESTAMP
FROM ROAD_SAFETY.INBOUND_MESSAGES m
JOIN ROAD_SAFETY.PARSE_STATE p ON p."topic_id" = m."topic_id" AND p."kafka_partition" = m."kafka_partition"
WHERE m."kafka_offset" > p."last_offset" AND m."kafka_offset" <= p."next_offset"
  AND JSON_VALUE(m."payload", '$.message_type') = 'warning_receipt';

UPDATE ROAD_SAFETY.PARSE_STATE SET "last_offset" = "next_offset" WHERE "last_offset" < "next_offset"
