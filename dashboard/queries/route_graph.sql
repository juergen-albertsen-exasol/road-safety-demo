-- The road network as a routing graph, for suggested diversions (loaded once per process: it never changes).
-- Every OSM road segment with its start and end node, one-way flag (1 = only from -> to, -1 = only to -> from),
-- class, name, speed limit, length and geometry.
SELECT s."segment_id", s."from_node", s."to_node", s."oneway", s."road_class", s."name", s."ref",
       s."maxspeed_kmh", s."length_m", s."heading_deg", s."geometry_wkt"
FROM ROAD_DATA.ROAD_SEGMENTS s
