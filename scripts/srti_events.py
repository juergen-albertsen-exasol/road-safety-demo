"""The messages the simulated partners send, and who can report what. All field names live here.

Until we have a real Data for Road Safety (DFRS) sample, the events follow the DATEX II SRTI profile:
the record types and values are the ones real SRTI feeds use (for example Finland's Digitraffic,
DATEX II 3.7: `GeneralObstruction` with `obstructionType = unprotectedAccidentArea`,
`PoorEnvironmentConditions`, `probabilityOfOccurrence`, a validity with start and end time). The
categories are the eight of the SRTI regulation (EU 886/2013, Art. 3). Carried as flat JSON, one
Kafka topic per provider, so the format is easy to change once the real one is known.

A carmaker detects hazards in its own backend and shares only the finished event: category, position,
direction, time, validity, confidence. Never raw signals or vehicle positions. An event has a stable
`event_id`; `update` messages replace the previous version, `cancel` withdraws it.

Messages never contain the simulator's scenario id: detection must find hazards from what the
partners send, not from the ground truth.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
EVENT_SCHEMA = "srti-event/1"
SENSOR_SCHEMA = "road-operator-sensor/1"

# The eight SRTI categories (EU 886/2013) -> DATEX II record type and the value of its type field
CATEGORIES = {
    "temporary_slippery_road": ("NonWeatherRelatedRoadConditions", "slipperyRoad"),
    "obstacle_on_road": ("GeneralObstruction", "objectOnTheRoad"),
    "unprotected_accident_area": ("GeneralObstruction", "unprotectedAccidentArea"),
    "short_term_road_works": ("MaintenanceWorks", "roadworks"),
    "reduced_visibility": ("PoorEnvironmentConditions", "fog"),
    "wrong_way_driver": ("VehicleObstruction", "vehicleOnWrongCarriageway"),
    "unmanaged_blockage": ("GeneralObstruction", "roadBlocked"),   # set by the combination, not by providers
    "exceptional_weather": ("PoorEnvironmentConditions", "strongWinds"),
}

# Topics: one per provider, like one data offer per partner in the data space
TOPICS = {
    "OEM_A": "srti.oem-a", "OEM_B": "srti.oem-b", "OEM_C": "srti.oem-c", "OEM_D": "srti.oem-d",
    "OEM_E": "srti.oem-e", "OEM_F": "srti.oem-f", "NAV": "srti.nav", "ROAD_OP": "datex.road-operator",
}

# What each provider can detect. Vehicle dynamics (ESC/ABS, airbag, hard braking) give slippery road,
# accidents, obstacles and wrong-way drivers (from the car's own map). Environment sensors (outside
# temperature, fog lights, wipers) add fog, storms and the cause of a slippery road. The navigation
# service sees speeds and user reports only: obstacles and road works, no accidents (no airbag data).
DYNAMICS = {"temporary_slippery_road", "unprotected_accident_area", "obstacle_on_road", "wrong_way_driver"}
ENVIRONMENT = {"reduced_visibility", "exceptional_weather"}
CAPABILITIES = {
    "OEM_A": DYNAMICS | ENVIRONMENT, "OEM_B": DYNAMICS | ENVIRONMENT, "OEM_C": DYNAMICS | ENVIRONMENT,
    "OEM_D": DYNAMICS | ENVIRONMENT, "OEM_E": DYNAMICS, "OEM_F": DYNAMICS,
    "NAV": {"obstacle_on_road", "short_term_road_works"},
    "ROAD_OP": set(CATEGORIES),       # through its sensors, cameras and warning trailers
}
KNOWS_CAUSE = {"OEM_A", "OEM_B", "OEM_C", "OEM_D"}   # outside temperature: ice or not

# Per scenario: SRTI category, cause, which providers report it by default (in this order) and, for the
# "realistic" delay profile, after how many simulation seconds; how long an event is valid, how often it
# is updated, and whether the road operator's sensor reports it too.
SCENARIO_EVENTS = {
    "slippery_road": {"category": "temporary_slippery_road", "cause": "ice",
                      "providers": {"OEM_A": 20, "OEM_C": 45, "OEM_B": 75}, "valid_min": 10, "update_s": 30},
    "oil_spill": {"category": "temporary_slippery_road", "cause": "fluid",
                  "providers": {"OEM_A": 20, "OEM_D": 50}, "valid_min": 15, "update_s": 30},
    "accident": {"category": "unprotected_accident_area", "cause": None,
                 "providers": {"OEM_B": 5, "OEM_E": 20}, "valid_min": 30, "update_s": 30},
    "wrong_way_driver": {"category": "wrong_way_driver", "cause": None,
                         "providers": {"OEM_C": 10, "OEM_A": 25}, "valid_min": 5, "update_s": 15},
    "fog": {"category": "reduced_visibility", "cause": None,
            "providers": {"OEM_A": 30, "OEM_D": 60, "OEM_B": 90}, "valid_min": 20, "update_s": 60},
    "storm": {"category": "exceptional_weather", "cause": None,
              "providers": {"OEM_A": 20, "OEM_B": 40, "OEM_C": 60}, "valid_min": 15, "update_s": 45},
    "animal": {"category": "obstacle_on_road", "cause": "animal",
               "providers": {"NAV": 15, "OEM_B": 40}, "valid_min": 15, "update_s": 30},
    "debris": {"category": "obstacle_on_road", "cause": "object",
               "providers": {"OEM_D": 15, "NAV": 35}, "valid_min": 15, "update_s": 30},
    "roadworks": {"category": "short_term_road_works", "cause": None, "sensor": True,
                  "providers": {}, "valid_min": 120, "update_s": 120},
}
DEFAULT_CONFIDENCE = (0.65, 0.9)

# Delay profiles. "stage" (the default): fast enough to narrate, slow enough to see one source before
# the second: the providers report after 5, 12, 20, 30 ... seconds in their order. "realistic": the
# per-scenario delays above, as a real backend's detection might take.
STAGE_DELAYS = (5, 12, 20, 30, 40, 50, 60)
SENSOR_DELAY_S = {"stage": 8, "realistic": 30}   # the road operator's sensor


def plan_delays(kind: str, steering: dict | None) -> tuple[dict, bool, float]:
    """Who reports a scenario after how many seconds: ({provider: delay}, sensor reports?, sensor delay).
    steering: the trigger options stored in SCENARIO_LOG (providers, delay, delay_mode, sensor)."""
    spec = SCENARIO_EVENTS[kind]
    st = steering or {}
    mode = st.get("delay_mode") or "stage"
    names = list(spec["providers"]) if st.get("providers") is None else list(st["providers"])
    if mode == "realistic":
        providers = {p: float(spec["providers"].get(p, 20 + 20 * i)) for i, p in enumerate(names)}
    else:
        providers = {p: float(STAGE_DELAYS[min(i, len(STAGE_DELAYS) - 1)]) for i, p in enumerate(names)}
    for p, d in (st.get("delay") or {}).items():          # explicit --delay wins
        if p == "*":
            providers = {q: float(d) for q in providers}
        elif p in providers:
            providers[p] = float(d)
    sensor = st.get("sensor") if st.get("sensor") is not None else spec.get("sensor", False)
    return providers, bool(sensor), float(SENSOR_DELAY_S[mode])


def iso_sim(t: datetime) -> str:
    """Simulation time (naive, local Munich time) as ISO 8601 with offset."""
    return t.replace(tzinfo=BERLIN).isoformat(timespec="seconds")


def iso_wall() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def srti_event(*, event_id: str, version: int, status: str, provider: str, category: str, cause: str | None,
               lat: float, lon: float, heading: float | None, road: str | None, detected: datetime,
               valid_until: datetime, confidence: float, observations: int, run_id: int,
               sensor_id: str | None = None) -> dict:
    record_type, subtype = CATEGORIES[category]
    return {
        "schema": EVENT_SCHEMA,
        "message_type": "srti_event",
        "event_id": event_id,
        "version": version,
        "status": status,                          # new, update, cancel
        "provider": provider,
        "srti_category": category,
        "datex_record_type": record_type,
        "datex_type": subtype,
        "cause": cause,
        "safety_related_message": True,
        "probability_of_occurrence": "certain" if confidence >= 0.85 else "probable" if confidence >= 0.6 else "riskOf",
        "confidence": round(confidence, 2),
        "observations": observations,              # how many of the provider's vehicles saw it so far
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "heading_deg": None if heading is None else round(heading) % 360,
        "road": road,                              # the provider's own map matching
        "detected_at": iso_sim(detected),
        "valid_from": iso_sim(detected),
        "valid_until": iso_sim(valid_until),
        "sensor_id": sensor_id,                    # road operator only: the sensor that saw it
        "sent_at": iso_wall(),
        "sim_run_id": run_id,
    }


def sensor_reading(row: dict, run_id: int) -> dict:
    """A road operator sensor reading (the columns of ROAD_SAFETY.SENSOR_READINGS)."""
    out = {"schema": SENSOR_SCHEMA, "message_type": "sensor_reading", "provider": "ROAD_OP"}
    out.update({k: v for k, v in row.items() if k != "ts"})
    out["ts"] = iso_sim(row["ts"])
    out["sent_at"] = iso_wall()
    out["sim_run_id"] = run_id
    return out
