-- Geometry of the main roads drawn with their traffic volume (cached by the page: it never changes).
SELECT s."segment_id", s."geometry_wkt"
FROM ROAD_DATA.ROAD_SEGMENTS s JOIN ROAD_DATA.SEGMENT_TRAFFIC t ON t."segment_id" = s."segment_id"
WHERE t."road_class" IN ('motorway', 'trunk', 'primary', 'secondary', 'motorway_link', 'trunk_link')
