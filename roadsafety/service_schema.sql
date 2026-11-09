-- The road safety service's own tables: what a partner running the reference implementation keeps.
-- Created by scripts/road_safety_service.py at start (CREATE ... IF NOT EXISTS: kept across restarts).
-- Exasol is the system of record: Kafka keeps nothing across a restart, these tables keep everything.
-- Column names are lower case and double quoted. Times named *_at are simulation time, local
-- (Europe/Berlin), like SIM_STATE. *_wall is the wall clock.

CREATE SCHEMA IF NOT EXISTS ROAD_SAFETY;

-- Every SRTI event as received, parsed from INBOUND_MESSAGES (one row per message: every version).
CREATE TABLE IF NOT EXISTS ROAD_SAFETY.SRTI_EVENTS (
    "topic"            VARCHAR(200) NOT NULL,
    "topic_id"         VARCHAR(36) NOT NULL,
    "kafka_partition"  DECIMAL(18,0) NOT NULL,
    "kafka_offset"     DECIMAL(36,0) NOT NULL,
    "event_id"         VARCHAR(100) NOT NULL,
    "version"          DECIMAL(9,0),
    "status"           VARCHAR(10),               -- new, update, cancel
    "provider"         VARCHAR(20),
    "srti_category"    VARCHAR(40),
    "datex_record_type" VARCHAR(60),
    "datex_type"       VARCHAR(60),
    "cause"            VARCHAR(20),
    "confidence"       DOUBLE,
    "probability"      VARCHAR(20),
    "observations"     DECIMAL(9,0),
    "lat"              DOUBLE,
    "lon"              DOUBLE,
    "heading_deg"      DOUBLE,
    "road"             VARCHAR(200),
    "detected_at"      TIMESTAMP,
    "valid_until"      TIMESTAMP,
    "sensor_id"        VARCHAR(20),
    "sim_run_id"       DECIMAL(9,0),
    "parsed_wall"      TIMESTAMP
);

-- How far INBOUND_MESSAGES has been parsed, per topic id and partition.
CREATE TABLE IF NOT EXISTS ROAD_SAFETY.PARSE_STATE (
    "topic_id"         VARCHAR(36) NOT NULL,
    "kafka_partition"  DECIMAL(18,0) NOT NULL,
    "last_offset"      DECIMAL(36,0) NOT NULL,    -- parsed up to here
    "next_offset"      DECIMAL(36,0) NOT NULL     -- being parsed up to here
);

-- Confirmed incidents with a stable id and a lifecycle. One row per incident, updated in place.
CREATE TABLE IF NOT EXISTS ROAD_SAFETY.INCIDENTS (
    "incident_id"      VARCHAR(40) NOT NULL,
    "run_id"           DECIMAL(9,0) NOT NULL,
    "status"           VARCHAR(10) NOT NULL,      -- active, cleared
    "version"          DECIMAL(9,0) NOT NULL,     -- incremented with every published change
    "srti_category"    VARCHAR(40) NOT NULL,
    "base_category"    VARCHAR(40) NOT NULL,      -- unprotected_accident_area for an unmanaged blockage
    "cause"            VARCHAR(20),
    "lat"              DOUBLE,
    "lon"              DOUBLE,
    "heading_deg"      DOUBLE,
    "road"             VARCHAR(200),
    "first_report_at"  TIMESTAMP,                 -- the earliest report behind it
    "confirmed_at"     TIMESTAMP,                 -- when the rule was first met
    "updated_at"       TIMESTAMP,
    "cleared_at"       TIMESTAMP,
    "valid_until"      TIMESTAMP,
    "confidence"       DOUBLE,
    "providers"        DECIMAL(4,0),
    "provider_list"    VARCHAR(200),
    "sensors"          DECIMAL(4,0),
    "sensor_list"      VARCHAR(400),
    "reports"          DECIMAL(6,0),
    "rule"             VARCHAR(200),              -- which rule confirmed it
    "reach_vehicles"   DECIMAL(9,0),
    "reach_kind"       VARCHAR(20),               -- pass_next_min (a spot) or in_area (fog, storm)
    "updated_wall"     TIMESTAMP
);

-- Which reports (SRTI events, sensor alerts) support which incident.
CREATE TABLE IF NOT EXISTS ROAD_SAFETY.INCIDENT_EVIDENCE (
    "incident_id"      VARCHAR(40) NOT NULL,
    "report_id"        VARCHAR(120) NOT NULL,     -- event_id, or sensor:<sensor id>:<category>
    "source"           VARCHAR(10) NOT NULL,      -- event, sensor
    "provider"         VARCHAR(20),
    "added_at"         TIMESTAMP
);

-- Every warning published to srti.confirmed: what was passed on, to whom, when.
CREATE TABLE IF NOT EXISTS ROAD_SAFETY.OUTBOUND_MESSAGES (
    "incident_id"      VARCHAR(40) NOT NULL,
    "version"          DECIMAL(9,0) NOT NULL,
    "status"           VARCHAR(10) NOT NULL,      -- new, update, cleared
    "topic"            VARCHAR(200),              -- NULL with --transport direct: logged, not sent
    "kafka_partition"  DECIMAL(18,0),
    "kafka_offset"     DECIMAL(36,0),
    "sim_at"           TIMESTAMP,
    "sent_wall"        TIMESTAMP,
    "payload"          VARCHAR(20000) UTF8
);

-- Data space metadata: the offers this partner uses and offers, and the contracts it "negotiated".
-- Shown on the dashboard, not enforced (no connector in iteration 1).
CREATE TABLE IF NOT EXISTS ROAD_SAFETY.DATA_OFFERS (
    "offer_id"         VARCHAR(40) NOT NULL,
    "provider_id"      VARCHAR(20) NOT NULL,      -- who offers it, SERVICE = this partner
    "title"            VARCHAR(200),
    "topic"            VARCHAR(100),
    "content"          VARCHAR(400),
    "usage_policy"     VARCHAR(400)
);

CREATE TABLE IF NOT EXISTS ROAD_SAFETY.CONTRACTS (
    "contract_id"      VARCHAR(40) NOT NULL,
    "offer_id"         VARCHAR(40) NOT NULL,
    "provider_id"      VARCHAR(20) NOT NULL,
    "consumer"         VARCHAR(100) NOT NULL,
    "status"           VARCHAR(20) NOT NULL,      -- agreed (simulated)
    "agreed_on"        DATE,
    "usage_policy"     VARCHAR(400)
)
