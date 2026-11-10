#!/usr/bin/env python3
"""The road safety service: the reference implementation a DFRS partner runs on its own setup.

Every few seconds:
1. load: pull new messages from the Kafka topics into ROAD_SAFETY.INBOUND_MESSAGES
   (scripts/kafka_loader.py; skipped with --transport direct, where the simulator writes there itself);
2. parse: turn them into SRTI_EVENTS and SENSOR_READINGS (roadsafety/parse.sql);
3. combine: confirm incidents from several providers and the road operator's sensors
   (roadsafety/combine.sql, one SQL statement with parameters);
4. keep INCIDENTS with a stable id and lifecycle (new, update, cleared), with INCIDENT_EVIDENCE;
5. publish every change as a confirmed warning to the topic srti.confirmed, logged in OUTBOUND_MESSAGES.

Exasol is the system of record: after a restart (Kafka keeps nothing) the service picks up where it
was, and publishes the incidents that are still active again, so subscribers see the current state.

    .venv/bin/python scripts/road_safety_service.py                       # Kafka (default)
    .venv/bin/python scripts/road_safety_service.py --transport direct    # no Kafka: combine and log only
    .venv/bin/python scripts/road_safety_service.py --min-providers 3     # stricter rule

Connects as `sys` to the Exasol Personal deployment; the password is read from the deployment's
secrets.json and never printed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pyexasol  # noqa: E402

import srti_events as ev  # noqa: E402
from simulate_road_safety import connect, insert, wall_now  # noqa: E402

OUT_TOPIC = "srti.confirmed"
INCIDENT_SCHEMA = "srti-incident/1"
COMBINE_DEFAULTS = {"min_providers": 2, "sensor_replaces": 1, "window_min": 30, "heading_max": 60,
                    "friction_max": 0.4, "visibility_max": 150, "blockage_after_s": 600, "reach_min": 10}
RULE_TEXT = {"providers": "{p} providers", "providers_and_sensor": "{p} provider and a road operator sensor",
             "road_operator": "the road operator's sensor"}
LABEL = {"temporary_slippery_road": "temporary slippery road", "obstacle_on_road": "obstacle on the road",
         "unprotected_accident_area": "unprotected accident area", "short_term_road_works": "short-term road works",
         "reduced_visibility": "reduced visibility", "wrong_way_driver": "wrong-way driver",
         "unmanaged_blockage": "unmanaged blockage", "exceptional_weather": "exceptional weather"}

OFFERS = [  # offer id, provider, title, topic, content, usage policy
    *[(f"OFFER-{p}", p, f"SRTI events of {name}", ev.TOPICS[p],
       "Safety-related traffic events (SRTI categories) detected in the provider's own backend",
       "Road safety only (EU 886/2013). Credit the source. No resale, no tracking of vehicles.")
      for p, name in (("OEM_A", "OEM A"), ("OEM_B", "OEM B"), ("OEM_C", "OEM C"), ("OEM_D", "OEM D"),
                      ("OEM_E", "OEM E"), ("OEM_F", "OEM F"), ("NAV", "the navigation service"))],
    ("OFFER-ROAD_OP", "ROAD_OP", "Road sensors and alerts of the road operator", ev.TOPICS["ROAD_OP"],
     "Readings of road weather stations, cameras, loop detectors and warning trailers",
     "Road safety and traffic management only. Credit the source."),
    ("OFFER-SERVICE", "SERVICE", "Confirmed SRTI warnings, Munich", OUT_TOPIC,
     "Incidents confirmed from several providers and road sensors, with reach",
     "Road safety only. Pass on the sources' attribution."),
]


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def statements(name: str) -> list[str]:
    text = "\n".join(l for l in (ROOT / "roadsafety" / f"{name}.sql").read_text().splitlines()
                     if not l.strip().startswith("--"))
    return [s.strip() for s in text.split(";") if s.strip()]


def dist_m(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lon2 - lon1) * 74320, (lat2 - lat1) * 110540)


def ts(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("T", " ")[:19])


def iso(v) -> str | None:
    return ev.iso_sim(ts(v)) if v is not None else None


def num(v):
    return float(v) if v is not None else None


class Service:
    def __init__(self, args):
        self.args = args
        self.c = connect(args.deployment)
        for s in statements("service_schema"):
            self.c.execute(s)
        self.seed_offers()
        self.params = {**COMBINE_DEFAULTS, "min_providers": args.min_providers}
        self.combine_sql = self.c.format.format((ROOT / "roadsafety" / "combine.sql").read_text(), **self.params)
        self.parse_sql = statements("parse")
        self.loader = self.producer = None
        if args.transport == "kafka":
            from kafka import KafkaProducer
            from kafka_loader import Loader, write_hosts_file
            write_hosts_file(args.deployment)
            self.loader = Loader(SimpleNamespace(deployment=args.deployment, parallel=4, prefix=["srti.", "datex."],
                                                 exclude={OUT_TOPIC}, interval=args.interval, once=False,
                                                 verbose=args.verbose))
            self.producer = KafkaProducer(bootstrap_servers="127.0.0.1:19092", acks=1)
        self.run_id = None
        self.evidence: dict[str, set] = {}

    def seed_offers(self) -> None:
        if not self.c.execute("SELECT COUNT(*) FROM ROAD_SAFETY.DATA_OFFERS").fetchval():
            insert(self.c, "DATA_OFFERS", OFFERS)
            insert(self.c, "CONTRACTS", [
                (f"CONTRACT-{o[1]}", o[0], o[1], "Road safety service (this partner)", "agreed (simulated)",
                 "2026-11-01", o[5]) for o in OFFERS if o[1] != "SERVICE"])

    # ------------------------------------------------------------------ state
    def now(self) -> dict | None:
        return self.c.execute('SELECT "run_id", "sim_now", "status" FROM ROAD_SAFETY.SIM_STATE '
                              'ORDER BY CASE WHEN "status" = \'running\' THEN 0 ELSE 1 END, "run_id" DESC LIMIT 1').fetchone()

    def open_incidents(self) -> dict[str, dict]:
        return {r["incident_id"]: r for r in self.c.execute(
            'SELECT * FROM ROAD_SAFETY.INCIDENTS WHERE "status" = \'active\' AND "run_id" = {r!d}', {"r": self.run_id})}

    def load_evidence(self, ids) -> None:
        for i in ids:
            if i not in self.evidence:
                self.evidence[i] = {r["report_id"] for r in self.c.execute(
                    'SELECT "report_id" FROM ROAD_SAFETY.INCIDENT_EVIDENCE WHERE "incident_id" = {i}', {"i": i})}

    def next_id(self) -> str:
        n = int(self.c.execute('SELECT COUNT(*) FROM ROAD_SAFETY.INCIDENTS WHERE "run_id" = {r!d}',
                               {"r": self.run_id}).fetchval()) + 1
        return f"INC-{self.run_id}-{n:04d}"

    # ------------------------------------------------------------------ publish
    def message(self, inc: dict, status: str) -> dict:
        record_type, subtype = ev.CATEGORIES[inc["srti_category"]]
        conf = num(inc["confidence"]) or 0.0
        providers = [p for p in str(inc["provider_list"] or "").split(",") if p]
        sensors = [s for s in str(inc["sensor_list"] or "").split(",") if s]
        return {
            "schema": INCIDENT_SCHEMA, "message_type": "srti_incident",
            "incident_id": inc["incident_id"], "version": int(inc["version"]), "status": status,
            "srti_category": inc["srti_category"], "datex_record_type": record_type, "datex_type": subtype,
            "cause": inc["cause"], "safety_related_message": True,
            "probability_of_occurrence": "certain" if conf >= 0.85 else "probable" if conf >= 0.6 else "riskOf",
            "confidence": round(conf, 2),
            "lat": num(inc["lat"]), "lon": num(inc["lon"]), "heading_deg": num(inc["heading_deg"]), "road": inc["road"],
            "first_report_at": iso(inc["first_report_at"]), "confirmed_at": iso(inc["confirmed_at"]),
            "valid_until": iso(inc["valid_until"]), "cleared_at": iso(inc["cleared_at"]),
            "confirmed_by": inc["rule"], "providers": providers, "road_operator_sensors": sensors,
            "reach": {"vehicles": int(inc["reach_vehicles"]) if inc["reach_vehicles"] is not None else None,
                      "kind": inc["reach_kind"], "minutes": self.params["reach_min"]},
            "attribution": "Data for Road Safety. Sources: " + ", ".join(providers + (["road operator"] if sensors else [])),
            "sent_at": ev.iso_wall(), "sim_run_id": int(inc["run_id"]),
        }

    def publish(self, inc: dict, status: str, sim_at, note: str = "") -> None:
        msg = self.message(inc, status)
        topic = part = off = None
        if self.producer is not None:
            try:
                md = self.producer.send(OUT_TOPIC, key=inc["incident_id"].encode(),
                                        value=json.dumps(msg, ensure_ascii=False).encode()).get(timeout=10)
                topic, part, off = OUT_TOPIC, md.partition, md.offset
            except Exception as e:  # noqa: BLE001 - logged in OUTBOUND_MESSAGES without an offset
                print(f"  !! could not publish {inc['incident_id']}: {type(e).__name__}: {e}", flush=True)
        insert(self.c, "OUTBOUND_MESSAGES", [(inc["incident_id"], int(inc["version"]), status, topic, part, off,
                                              ts(sim_at), wall_now(), json.dumps(msg, ensure_ascii=False))])
        self.log(inc, status, note, f"{topic}@{off}" if topic else "logged (no Kafka)")

    def log(self, inc: dict, status: str, note: str, where: str) -> None:
        what = LABEL.get(inc["srti_category"], inc["srti_category"]) + (f" ({inc['cause']})" if inc["cause"] else "")
        line = f"{str(ts(inc['updated_at']))[11:19]} {status.upper():<7} {inc['incident_id']} {what} on {inc['road'] or 'unnamed road'}"
        if status == "new":
            first, conf_at = ts(inc["first_report_at"]), ts(inc["confirmed_at"])
            secs = int((conf_at - first).total_seconds()) if first and conf_at else 0
            reach = f"about {int(inc['reach_vehicles']):,} vehicles " + ("in the area" if inc["reach_kind"] == "in_area"
                                                                       else f"in the next {self.params['reach_min']} min") \
                if inc["reach_vehicles"] is not None else "reach unknown"
            line += (f": confirmed by {inc['rule']} ({inc['provider_list'] or '-'}"
                     f"{'; sensors ' + inc['sensor_list'] if inc['sensor_list'] else ''}), {secs} s after the first report;"
                     f" confidence {num(inc['confidence']):.2f}; {reach}")
        if note:
            line += f" ({note})"
        print(f"{line} -> {where}", flush=True)

    # ------------------------------------------------------------------ the cycle
    def start_run(self, run_id: int) -> None:
        """A (new) simulator run: close what is left of other runs, publish what is active in this one."""
        self.run_id = run_id
        for r in self.c.execute('SELECT * FROM ROAD_SAFETY.INCIDENTS WHERE "status" = \'active\' AND "run_id" <> {r!d}',
                                {"r": run_id}).fetchall():
            r = dict(r)
            r.update(status="cleared", version=int(r["version"]) + 1, cleared_at=r["updated_at"])
            self.save(r)
            self.publish(r, "cleared", r["updated_at"], "its run has ended")
        for r in self.open_incidents().values():
            self.publish(dict(r), "update", r["updated_at"], "republished after a restart")

    def save(self, inc: dict) -> None:
        self.c.execute('DELETE FROM ROAD_SAFETY.INCIDENTS WHERE "incident_id" = {i}', {"i": inc["incident_id"]})
        cols = ["incident_id", "run_id", "status", "version", "srti_category", "base_category", "cause", "lat", "lon",
                "heading_deg", "road", "first_report_at", "confirmed_at", "updated_at", "cleared_at", "valid_until",
                "confidence", "providers", "provider_list", "sensors", "sensor_list", "reports", "rule",
                "reach_vehicles", "reach_kind", "updated_wall"]
        inc["updated_wall"] = wall_now()
        row = []
        for k in cols:
            v = inc.get(k)
            if k in ("first_report_at", "confirmed_at", "updated_at", "cleared_at", "valid_until", "updated_wall"):
                v = ts(v)
            elif isinstance(v, (int, float)) or v is None or isinstance(v, str):
                pass
            else:
                v = float(v)
            row.append(v)
        insert(self.c, "INCIDENTS", [tuple(row)])

    def cycle(self) -> None:
        if self.loader is not None:
            self.loader.cycle()
        for s in self.parse_sql:
            self.c.execute(s)
        st = self.now()
        if not st:
            return
        if int(st["run_id"]) != self.run_id:
            self.start_run(int(st["run_id"]))
        sim_now = ts(st["sim_now"])
        cands = [dict(r) for r in self.c.execute(self.combine_sql)]
        opened = self.open_incidents()
        self.load_evidence(opened)
        matched: dict[str, dict] = {}
        supported: set[str] = set()
        for cand in sorted(cands, key=lambda c: (not c["confirmed"], -int(c["reports"]))):
            members = set(str(cand["members"]).split(","))
            best, best_key = None, None
            for iid, inc in opened.items():
                if inc["base_category"] != cand["base_category"]:
                    continue
                overlap = len(members & self.evidence.get(iid, set()))
                d = dist_m(num(inc["lat"]), num(inc["lon"]), num(cand["lat"]), num(cand["lon"]))
                if overlap or d <= float(cand["radius_m"]):
                    key = (overlap, -d)
                    if best_key is None or key > best_key:
                        best, best_key = iid, key
            if best is not None:
                supported.add(best)
                if cand["confirmed"] and best not in matched:
                    matched[best] = cand
            elif cand["confirmed"]:
                self.confirm(cand, sim_now)
        for iid, cand in matched.items():
            self.update(opened[iid], cand, sim_now)
        for iid, inc in opened.items():
            if iid not in supported:                      # no valid report left behind it
                inc = dict(inc)
                inc.update(status="cleared", version=int(inc["version"]) + 1, cleared_at=sim_now, updated_at=sim_now)
                self.save(inc)
                self.publish(inc, "cleared", sim_now, "no valid report left")

    def fields(self, cand: dict) -> dict:
        rule = RULE_TEXT.get(cand["rule"] or "", "-").format(p=int(cand["providers"]))
        return {"srti_category": cand["srti_category"], "base_category": cand["base_category"], "cause": cand["cause"],
                "lat": num(cand["lat"]), "lon": num(cand["lon"]), "heading_deg": num(cand["heading_deg"]),
                "road": cand["road"], "valid_until": cand["valid_until"], "confidence": num(cand["confidence"]),
                "providers": int(cand["providers"]), "provider_list": cand["provider_list"],
                "sensors": int(cand["sensors"]), "sensor_list": cand["sensor_list"], "reports": int(cand["reports"]),
                "rule": rule, "reach_vehicles": num(cand["reach_vehicles"]), "reach_kind": cand["reach_kind"]}

    def add_evidence(self, iid: str, cand: dict, sim_now) -> None:
        have = self.evidence.setdefault(iid, set())
        new = [m for m in str(cand["members"]).split(",") if m and m not in have]
        if new:
            insert(self.c, "INCIDENT_EVIDENCE", [
                (iid, m, "sensor" if m.startswith("sensor:") else "event",
                 "ROAD_OP" if m.startswith("sensor:") else m.rsplit("-", 1)[0],      # event ids are <provider>-<hex>
                 sim_now) for m in new])
            have.update(new)

    def confirm(self, cand: dict, sim_now) -> None:
        inc = {"incident_id": self.next_id(), "run_id": self.run_id, "status": "active", "version": 1,
               "first_report_at": cand["first_report_at"], "confirmed_at": sim_now, "updated_at": sim_now,
               "cleared_at": None, **self.fields(cand)}
        self.save(inc)
        self.add_evidence(inc["incident_id"], cand, sim_now)
        self.publish(inc, "new", sim_now)

    def update(self, inc: dict, cand: dict, sim_now) -> None:
        new = self.fields(cand)
        self.add_evidence(inc["incident_id"], cand, sim_now)
        changes = []
        if new["srti_category"] != inc["srti_category"]:
            changes.append(f"now {LABEL[new['srti_category']]}")
        if new["cause"] != inc["cause"] and new["cause"]:
            changes.append(f"cause {new['cause']}")
        if new["road"] is None:                  # a report without map matching: keep the road known so far
            new["road"] = inc["road"]
        if new["providers"] != int(inc["providers"]) or new["sensors"] != int(inc["sensors"]):
            changes.append(f"{plural(new['providers'], 'provider')}, {plural(new['sensors'], 'sensor')}")
        if dist_m(num(inc["lat"]), num(inc["lon"]), new["lat"], new["lon"]) > 150:
            changes.append("moved")
        if ts(new["valid_until"]) and ts(inc["valid_until"]) and \
                (ts(new["valid_until"]) - ts(inc["valid_until"])).total_seconds() >= 300:
            changes.append("valid longer")
        if abs((new["confidence"] or 0) - (num(inc["confidence"]) or 0)) >= 0.1:
            changes.append(f"confidence {new['confidence']:.2f}")
        inc = dict(inc)
        if not changes:                       # keep counts and position current, without a new version
            inc.update({k: v for k, v in new.items() if k not in ("valid_until",)}, updated_at=sim_now)
            self.save(inc)
            return
        inc.update(new, version=int(inc["version"]) + 1, updated_at=sim_now)
        self.save(inc)
        self.publish(inc, "update", sim_now, ", ".join(changes))

    def run(self) -> None:
        where = "Kafka topics srti.* and datex.*" if self.loader else "INBOUND_MESSAGES (direct)"
        print(f"{datetime.now():%H:%M:%S} road safety service: reading {where}, publishing to "
              f"{OUT_TOPIC if self.producer else 'OUTBOUND_MESSAGES only'}, every {self.args.interval:g} s; "
              f"rule: {self.params['min_providers']} providers, or one fewer plus a road operator sensor", flush=True)
        while True:
            t0 = time.time()
            self.cycle()
            if self.args.once:
                return
            time.sleep(max(0.2, self.args.interval - (time.time() - t0)))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--deployment", default="road-safety-demo")
    p.add_argument("--transport", choices=["kafka", "direct"], default="kafka")
    p.add_argument("--interval", type=float, default=1.5, help="seconds between rounds (default 1.5)")
    p.add_argument("--min-providers", type=int, default=2, help="different providers needed (default 2)")
    p.add_argument("--once", action="store_true", help="one round, then exit")
    p.add_argument("--verbose", action="store_true", help="also print every Kafka import")
    args = p.parse_args()
    while True:   # survive `exasol stop`/`start`: wait and reconnect
        try:
            Service(args).run()
            return
        except KeyboardInterrupt:
            return
        except (SystemExit, Exception) as e:  # noqa: BLE001
            if args.once:
                raise
            print(f"{datetime.now():%H:%M:%S} waiting for the database and Kafka: {type(e).__name__}: "
                  f"{' '.join(str(e).split())[:300]}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
