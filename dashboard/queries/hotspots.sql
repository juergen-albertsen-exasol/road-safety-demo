-- Accident hotspots: road segments with the most injury accidents 2016-2025 (Unfallatlas).
SELECT s."segment_id", COALESCE(NULLIF(s."name", ''), s."ref") AS "road", s."mid_lat", s."mid_lon",
       COUNT(*) AS "accidents", SUM(CASE WHEN a."road_condition" = 2 THEN 1 ELSE 0 END) AS "icy"
FROM ROAD_DATA.ACCIDENTS a JOIN ROAD_DATA.ROAD_SEGMENTS s ON s."segment_id" = a."segment_id"
GROUP BY s."segment_id", s."name", s."ref", s."mid_lat", s."mid_lon"
HAVING COUNT(*) >= {min_accidents!d}
