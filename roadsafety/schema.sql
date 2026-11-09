-- Schema ROAD_SAFETY: the partners of the road safety demo and what they send.
-- Everything in this schema is simulated by scripts/simulate_road_safety.py. The real open
-- data it builds on is in ROAD_DATA (scripts/prepare_road_data.py). The messages themselves arrive
-- through Kafka in ROAD_SAFETY.INBOUND_MESSAGES (created by scripts/kafka_loader.py).
--
-- Created by `simulate_road_safety.py setup`. Column names are lower case and double quoted,
-- like ROAD_DATA. Timestamps are simulation time, local (Europe/Berlin), unless named *_wall.

CREATE SCHEMA IF NOT EXISTS ROAD_SAFETY;

-- Fictional participants with generic names: no real brands, so nobody assumes real data behind them.
CREATE OR REPLACE TABLE ROAD_SAFETY.DATA_PROVIDERS (
    "provider_id"      VARCHAR(20) NOT NULL,
    "provider_name"    VARCHAR(100) NOT NULL,
    "provider_kind"    VARCHAR(20) NOT NULL,      -- oem, navigation, road_operator
    "shares_vehicle_dynamics" BOOLEAN,            -- ESC/ABS, airbag, hard braking from the CAN bus
    "shares_environment"      BOOLEAN,            -- outside temperature, wipers, fog lights
    "fleet_share_pct"  DECIMAL(5,1),
    "topic"            VARCHAR(100),              -- the Kafka topic it publishes to
    PRIMARY KEY ("provider_id")
);

-- VEHICLES and VEHICLE_SIGNALS belong to the raw-signal version: the simulator no longer writes them
-- (since 2026-11-09). They stay until the combination SQL and the dashboard no longer read them.
CREATE OR REPLACE TABLE ROAD_SAFETY.VEHICLES (
    "vehicle_id"       DECIMAL(9,0) NOT NULL,
    "provider_id"      VARCHAR(20) NOT NULL,
    "vehicle_type"     VARCHAR(20) NOT NULL,      -- car, van, truck, bus
    "home_segment_id"  DECIMAL(9,0),              -- where the vehicle started
    PRIMARY KEY ("vehicle_id")
);

-- Virtual sensors at real places taken from OSM (ROAD_DATA.ROAD_SEGMENTS).
CREATE OR REPLACE TABLE ROAD_SAFETY.INFRA_SENSORS (
    "sensor_id"        VARCHAR(20) NOT NULL,
    "sensor_type"      VARCHAR(20) NOT NULL,      -- road_weather, camera, loop_detector, warning_trailer
    "location_kind"    VARCHAR(20) NOT NULL,      -- isar_bridge, bridge, motorway, tunnel, motorway_entry, mobile
    "sensor_name"      VARCHAR(200),
    "segment_id"       DECIMAL(9,0) NOT NULL,
    "provider_id"      VARCHAR(20) NOT NULL,
    "lat"              DOUBLE,
    "lon"              DOUBLE,
    PRIMARY KEY ("sensor_id")
);

-- Raw vehicle stream. Each vehicle reports every 10 s of simulation time, and at once on an event
-- (hard braking, ESC/ABS, airbag). NULL means the provider doesn't share that signal.
CREATE OR REPLACE TABLE ROAD_SAFETY.VEHICLE_SIGNALS (
    "run_id"           DECIMAL(9,0) NOT NULL,
    "ts"               TIMESTAMP NOT NULL,
    "vehicle_id"       DECIMAL(9,0) NOT NULL,
    "provider_id"      VARCHAR(20) NOT NULL,
    "lat"              DOUBLE NOT NULL,
    "lon"              DOUBLE NOT NULL,
    "segment_id"       DECIMAL(9,0) NOT NULL,
    "heading_deg"      DECIMAL(4,0),
    "speed_kmh"        DECIMAL(4,0),
    "esc_active"       BOOLEAN,
    "abs_active"       BOOLEAN,
    "hard_braking"     BOOLEAN,
    "hazard_lights"    BOOLEAN,
    "fog_lights"       BOOLEAN,
    "wiper_level"      DECIMAL(1,0),              -- 0 off, 1 interval, 2 normal, 3 fast
    "outside_temp_c"   DECIMAL(4,1),
    "airbag"           BOOLEAN,
    "ingested_wall"    TIMESTAMP NOT NULL          -- wall clock when the row was written
);

-- Readings of the virtual sensors, every 60 s of simulation time. Since 2026-11-09 they are sent to the
-- topic datex.road-operator; this table is filled from INBOUND_MESSAGES.
CREATE OR REPLACE TABLE ROAD_SAFETY.SENSOR_READINGS (
    "run_id"           DECIMAL(9,0) NOT NULL,
    "ts"               TIMESTAMP NOT NULL,
    "sensor_id"        VARCHAR(20) NOT NULL,
    "surface_temp_c"   DECIMAL(4,1),              -- road_weather
    "friction"         DECIMAL(3,2),              -- road_weather: 0 (ice) .. 1 (dry, grippy)
    "visibility_m"     DECIMAL(6,0),              -- road_weather, capped at 2,000 m
    "wrong_way_detected" BOOLEAN,                 -- camera
    "stationary_vehicle" BOOLEAN,                 -- camera
    "vehicle_count"    DECIMAL(5,0),              -- camera, loop_detector: vehicles on the segment
    "avg_speed_kmh"    DECIMAL(4,0),
    "ingested_wall"    TIMESTAMP NOT NULL,
    "works_active"     BOOLEAN                    -- warning_trailer: a road works site is set up here
);

-- Ground truth: every scenario injected on purpose. Written by `simulate_road_safety.py trigger`.
-- Detection must never read this table; the dashboard reads it only for the simulation band.
CREATE OR REPLACE TABLE ROAD_SAFETY.SCENARIO_LOG (
    "scenario_id"      DECIMAL(9,0) NOT NULL,
    "run_id"           DECIMAL(9,0),
    "scenario_type"    VARCHAR(40) NOT NULL,      -- slippery_road, accident, wrong_way_driver, fog, storm, animal, debris
    "area"             VARCHAR(100),
    "area_wkt"         VARCHAR(2000000),          -- the injected area, shown dashed on the map
    "started_at"       TIMESTAMP NOT NULL,        -- simulation time
    "ended_at"         TIMESTAMP,
    "started_wall"     TIMESTAMP NOT NULL,
    "triggered_by"     VARCHAR(100),
    "note"             VARCHAR(2000),
    "steering"         VARCHAR(2000),             -- JSON: who reports it, delays, confidence, sensor (trigger options)
    PRIMARY KEY ("scenario_id")
);

-- One row per simulator run; the row with status 'running' holds the current simulation time.
CREATE OR REPLACE TABLE ROAD_SAFETY.SIM_STATE (
    "run_id"           DECIMAL(9,0) NOT NULL,
    "status"           VARCHAR(20) NOT NULL,      -- running, stopped
    "weather_day"      DATE NOT NULL,
    "sim_start"        TIMESTAMP NOT NULL,
    "sim_now"          TIMESTAMP NOT NULL,
    "speed"            DOUBLE NOT NULL,           -- simulation seconds per wall second
    "seed"             DECIMAL(9,0),
    "vehicles"         DECIMAL(6,0),              -- raw-signal version only
    "started_wall"     TIMESTAMP NOT NULL,
    "updated_wall"     TIMESTAMP NOT NULL,
    "transport"        VARCHAR(10),               -- kafka or direct
    PRIMARY KEY ("run_id")
);
