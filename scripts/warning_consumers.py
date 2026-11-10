"""The consumers of the confirmed warnings: who receives what Exasol publishes, and what they do with it.

Simulated, like the other partners. Each consumer is a Kafka consumer group of its own on `srti.confirmed`
(visible in Kafbat UI under Consumers):

- the carmakers' backends (OEM A-F) pass a warning on to their own vehicles that will pass the spot: their
  share of the incident's reach (the fleet shares of ROAD_SAFETY.DATA_PROVIDERS);
- the navigation service passes it on to its app users, the same way;
- the road operator switches on a variable message sign on major roads, about 1.5 km before the incident, and
  off again when it clears.

Every consumer confirms each warning it received on the topic `srti.receipts` (schema `srti-receipt/1`).
Exasol loads those like every other topic (ROAD_SAFETY.WARNING_RECEIPTS), so it knows not only what it passed
on, but who received it and when: the audit trail a usage policy may ask for.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone

IN_TOPIC = "srti.confirmed"
RECEIPT_TOPIC = "srti.receipts"
RECEIPT_SCHEMA = "srti-receipt/1"
SIGN_BEFORE_M = 1500
SIGN_ROADS = {"motorway", "trunk", "primary", "secondary"}       # where the road operator has message signs
SIGN_TEXT = {"unprotected_accident_area": "ACCIDENT AHEAD", "unmanaged_blockage": "ROAD BLOCKED AHEAD",
             "temporary_slippery_road": "SLIPPERY ROAD", "obstacle_on_road": "OBSTACLE ON ROAD",
             "wrong_way_driver": "WRONG-WAY DRIVER - KEEP RIGHT", "reduced_visibility": "FOG - SLOW DOWN",
             "exceptional_weather": "STORM - SLOW DOWN", "short_term_road_works": "ROAD WORKS AHEAD"}
LABEL = {"OEM_A": "OEM A", "OEM_B": "OEM B", "OEM_C": "OEM C", "OEM_D": "OEM D", "OEM_E": "OEM E", "OEM_F": "OEM F",
         "NAV": "Navigation", "ROAD_OP": "Road operator"}
KX, KY = 74320.0, 110540.0


def group_id(consumer: str) -> str:
    return "warning-consumer." + consumer.lower().replace("_", "-")


def iso_wall() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _angle(a: float, b: float) -> float:
    return abs((a - b + 540) % 360 - 180)


class Consumers(threading.Thread):
    """All consumers in one thread: poll each group in turn, act, send receipts, print one line per warning."""

    def __init__(self, bootstrap: str, run_id: int, graph, fleet: dict[str, float], log=None):
        super().__init__(daemon=True)
        self.bootstrap, self.run_id, self.graph, self.fleet, self.log = bootstrap, run_id, graph, fleet, log
        self.consumers = list(fleet) + ["ROAD_OP"]
        self.signs: dict[str, dict] = {}                 # incident id -> the sign the road operator switched on
        self.ready = threading.Event()
        self.arriving: dict[int, list] = {}              # node -> [(segment, travelled from -> to)] that end there
        for seg in graph.values():
            if seg.oneway in (0, 1):
                self.arriving.setdefault(seg.to_node, []).append((seg, True))
            if seg.oneway in (0, -1):
                self.arriving.setdefault(seg.from_node, []).append((seg, False))

    # ---- the road operator's sign
    def sign_for(self, lat: float, lon: float, hdg: float | None) -> dict | None:
        """A message sign about SIGN_BEFORE_M before the incident, on the same major road, against the traffic."""
        best = None
        for s in self.graph.values():
            if s.road_class not in SIGN_ROADS:
                continue
            if abs(s.mid[1] - lat) * KY > 400 or abs(s.mid[0] - lon) * KX > 400:
                continue
            d = min(math.hypot((x - lon) * KX, (y - lat) * KY) for x, y in s.coords)
            for fwd in (True, False):
                if (fwd and s.oneway == -1) or (not fwd and s.oneway == 1):
                    continue
                travel = s.heading if fwd else (s.heading + 180) % 360
                if hdg is not None and _angle(travel, hdg) > 60:
                    continue
                if d <= 150 and (best is None or d < best[0]):
                    best = (d, s, fwd)
        if best is None:
            return None
        _, seg, fwd = best
        node, done, prev, prev_fwd = (seg.from_node if fwd else seg.to_node), 0.0, seg, fwd
        point = seg.coords[0] if fwd else seg.coords[-1]
        while done < SIGN_BEFORE_M:
            here = prev.heading if prev_fwd else (prev.heading + 180) % 360
            cands = [(t, f) for t, f in self.arriving.get(node, ())
                     if t.sid != prev.sid and ((t.ref and t.ref == prev.ref) or (t.name and t.name == prev.name))]
            if not cands:
                break
            t, f = min(cands, key=lambda c: _angle(c[0].heading if c[1] else (c[0].heading + 180) % 360, here))
            done += t.length
            node = t.from_node if f else t.to_node
            point = t.coords[0] if f else t.coords[-1]
            prev, prev_fwd = t, f
        road = seg.ref or seg.name
        return {"lat": round(point[1], 6), "lon": round(point[0], 6), "road": road, "before_m": round(done)}

    # ---- acting on a warning
    def act(self, consumer: str, w: dict, offset: int) -> dict:
        status, iid = w["status"], w["incident_id"]
        reach = (w.get("reach") or {}).get("vehicles") or 0
        r = {"schema": RECEIPT_SCHEMA, "message_type": "warning_receipt", "consumer": consumer,
             "consumer_group": group_id(consumer), "incident_id": iid, "version": w["version"], "status": status,
             "srti_category": w["srti_category"], "warning_topic": IN_TOPIC, "warning_offset": offset,
             "received_at": iso_wall(), "warning_sent_at": w.get("sent_at"), "sim_run_id": self.run_id,
             "vehicles": None, "sign_lat": None, "sign_lon": None, "sign_road": None, "sign_text": None, "action": ""}
        if consumer == "ROAD_OP":
            if status == "cleared":
                sign = self.signs.pop(iid, None)
                r["action"] = f"sign on {sign['road']} switched off" if sign else "no sign to switch off"
                if sign:
                    r.update(sign_lat=sign["lat"], sign_lon=sign["lon"], sign_road=sign["road"], sign_text="")
            else:
                sign = self.signs.get(iid)
                if sign is None or (w.get("lat") and math.hypot((w["lon"] - sign["inc_lon"]) * KX,
                                                                (w["lat"] - sign["inc_lat"]) * KY) > 1000):
                    found = self.sign_for(w["lat"], w["lon"], w.get("heading_deg")) if w.get("lat") else None
                    sign = {**found, "inc_lat": w["lat"], "inc_lon": w["lon"],
                            "text": SIGN_TEXT.get(w["srti_category"], "DANGER AHEAD")} if found else None
                    if sign:
                        self.signs[iid] = sign
                if sign:
                    r.update(sign_lat=sign["lat"], sign_lon=sign["lon"], sign_road=sign["road"], sign_text=sign["text"])
                    r["action"] = f'sign on {sign["road"]}, {sign["before_m"] / 1000:.1f} km before: "{sign["text"]}"'
                else:
                    r["action"] = "no message sign on this road"
        else:
            n = round(reach * self.fleet.get(consumer, 0) / 100)
            r["vehicles"] = n
            r["action"] = (f"all-clear to {n} vehicles" if status == "cleared" else
                           f"warned {n} {'app users' if consumer == 'NAV' else 'vehicles'}")
        return r

    def line(self, w: dict, receipts: list[dict]) -> str:
        """One log line per warning: who received it and what they did."""
        cars = [r for r in receipts if r["vehicles"] is not None]
        sign = next((r for r in receipts if r["consumer"] == "ROAD_OP"), None)
        n = sum(r["vehicles"] or 0 for r in cars)
        verb = "all-clear" if w["status"] == "cleared" else "warning"
        parts = [f"{LABEL[r['consumer']]} {r['vehicles']}" for r in cars]
        out = (f"{datetime.now():%H:%M:%S} RECEIVED {w['incident_id']} v{w['version']} {w['status']}: {verb} to {n} "
               f"vehicles ({', '.join(parts)})")
        if sign:
            out += f"; Road operator: {sign['action']}"
        return out + f"  -> receipts on {RECEIPT_TOPIC}"

    # ---- the loop
    def run(self) -> None:
        from kafka import KafkaConsumer, KafkaProducer
        try:
            subs = {c: KafkaConsumer(IN_TOPIC, bootstrap_servers=self.bootstrap, group_id=group_id(c),
                                     auto_offset_reset="latest", enable_auto_commit=True, client_id=group_id(c))
                    for c in self.consumers}
            producer = KafkaProducer(bootstrap_servers=self.bootstrap, linger_ms=10, acks=1)
        except Exception as e:  # noqa: BLE001 - the simulation keeps running without consumers
            print(f"(warning consumers: Kafka not reachable: {type(e).__name__})", flush=True)
            return
        self.ready.set()
        pending: dict[tuple, tuple[dict, list, float]] = {}    # (incident, version) -> warning, receipts, first seen
        while True:
            for c, sub in subs.items():
                try:
                    batch = sub.poll(timeout_ms=100)
                except Exception as e:  # noqa: BLE001
                    print(f"(warning consumer {c}: {type(e).__name__})", flush=True)
                    time.sleep(2)
                    continue
                for records in batch.values():
                    for rec in records:
                        try:
                            w = json.loads(rec.value)
                        except ValueError:
                            continue
                        if w.get("sim_run_id") != self.run_id:     # left from an earlier run
                            continue
                        receipt = self.act(c, w, rec.offset)
                        producer.send(RECEIPT_TOPIC, key=f"{w['incident_id']}:{c}".encode(),
                                      value=json.dumps(receipt, ensure_ascii=False).encode())
                        key = (w["incident_id"], w["version"])
                        pending.setdefault(key, (w, [], time.time()))[1].append(receipt)
            if pending:
                producer.flush(timeout=10)
            for key in [k for k, (_, rs, t0) in pending.items() if len(rs) >= len(subs) or time.time() - t0 > 3]:
                w, receipts, _ = pending.pop(key)              # one line, once everyone has it (or after 3 s)
                text = self.line(w, receipts)
                print(self.log.c(self.log.CYAN, text) if self.log else text, flush=True)
