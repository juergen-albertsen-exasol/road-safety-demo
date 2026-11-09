-- Real roadworks on the main road network today (City of Munich, Autobahn GmbH), as
-- matched to the road segments by prepare_road_data.py.
SELECT t."segment_id", t."roadworks", t."roadworks_source", t."capacity_factor", s."mid_lat", s."mid_lon"
FROM ROAD_DATA.SEGMENT_TRAFFIC t JOIN ROAD_DATA.ROAD_SEGMENTS s ON s."segment_id" = t."segment_id"
WHERE t."capacity_factor" < 1
