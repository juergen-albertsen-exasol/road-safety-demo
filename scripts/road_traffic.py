"""Traffic model for the road safety simulator: load, capacity, queues and speed per road segment.

Keeps the traffic on every main road segment and direction (the simulator has no individual vehicles;
the road operator's loop detectors and cameras read their counts and speeds from here):

- load = peak-hour load (ROAD_DATA.SEGMENT_TRAFFIC.peak_vc: BASt counts for motorways, road class for
  city roads) x hotspot factor x rush-hour curve (ROAD_DATA.TRAFFIC_PROFILE, BASt 2024, Tue-Thu) for the
  direction (into the city, out of it, around it), normalised to the curve's peak;
- capacity factor = real roadworks on the segment (ROAD_DATA.SEGMENT_TRAFFIC.capacity_factor) x
  scenario overrides (an accident closes lanes);
- load above capacity builds a queue: a share of the excess demand (QUEUE_GROWTH) joins it, the rest is
  assumed to divert or leave later; it discharges with the spare capacity and is capped (MAX_QUEUE_M);
  the queue covers the upstream segments of the same road, where traffic crawls;
- elsewhere the speed falls as the load approaches capacity.

"""
from __future__ import annotations

import math
from collections import defaultdict

JAM_M_PER_VEHICLE = 7.0          # metres of queue per vehicle and lane
QUEUE_KMH = 12.0                 # speed in a queue (stop and go)
BOTTLENECK_KMH = 22.0            # speed through the bottleneck itself
CITY_SIGNAL_FACTOR = 0.8         # traffic lights: city roads run below the limit even when free
INVERT = {"inbound": "outbound", "outbound": "inbound", "ring": "ring"}
QUEUE_GROWTH = 0.35              # share of the excess demand that joins the queue (the rest diverts or leaves later)
MAX_QUEUE_M = {"motorway": 6000, "other": 2500}


class TrafficModel:
    def __init__(self, c, graph):
        self.graph = graph
        prof = {int(r["hour"]): r for r in c.execute("SELECT * FROM ROAD_DATA.TRAFFIC_PROFILE")}
        self.profile = {d: [float(prof[h][d]) for h in range(24)] for d in ("inbound", "outbound", "ring")}
        self.peak = {d: max(v) for d, v in self.profile.items()}
        self.seg = {}
        for r in c.execute("SELECT * FROM ROAD_DATA.SEGMENT_TRAFFIC"):
            sid = int(r["segment_id"])
            if sid not in graph:
                continue
            self.seg[sid] = {
                "lanes": max(1, int(r["lanes_dir"])), "cap": float(r["capacity_vph"]),
                "free": float(r["free_kmh"]) * (1.0 if r["road_class"].startswith("motorway") else CITY_SIGNAL_FACTOR),
                "load": float(r["peak_vc"]) * float(r["hotspot_factor"]), "flow": r["flow_dir"],
                "capf": float(r["capacity_factor"]), "road": r["road"] or "",
            }
        # directions a segment can be driven in: (segment id, forward?)
        self.keys = []
        for sid in self.seg:
            s = graph[sid]
            if s.oneway in (0, 1):
                self.keys.append((sid, True))
            if s.oneway in (0, -1):
                self.keys.append((sid, False))
        # node -> main-road directions that end at it (for walking a queue upstream)
        self.enters = defaultdict(list)
        for sid, fwd in self.keys:
            s = graph[sid]
            self.enters[s.to_node if fwd else s.from_node].append((sid, fwd))
        self.queue = defaultdict(float)          # (sid, fwd) -> vehicles waiting at this bottleneck
        self.speed: dict = {}                    # (sid, fwd) -> km/h
        self.queued: dict = {}                   # (sid, fwd) -> bottleneck key whose queue covers it

    def factor(self, flow: str, hour: float) -> float:
        """Share of the curve's peak at this time (hourly values placed at the half hour, interpolated)."""
        p = self.profile[flow]
        x = hour - 0.5
        h0 = math.floor(x) % 24
        t = x - math.floor(x)
        return (p[h0] * (1 - t) + p[(h0 + 1) % 24] * t) / self.peak[flow]

    def update(self, when, dt_s: float, cap_override: dict) -> None:
        hour = when.hour + when.minute / 60 + when.second / 3600
        fac = {d: self.factor(d, hour) for d in self.profile}
        speed = {}
        for key in self.keys:
            sid, fwd = key
            s = self.seg[sid]
            flow = s["flow"] if fwd else INVERT[s["flow"]]
            load = s["load"] * fac[flow]
            capf = s["capf"] * cap_override.get(sid, 1.0)
            r = load / capf
            q = self.queue[key]
            if r > 1:
                qmax = MAX_QUEUE_M["motorway" if s["free"] >= 90 else "other"] * s["lanes"] / JAM_M_PER_VEHICLE
                q = min(qmax, q + QUEUE_GROWTH * (r - 1) * s["cap"] * dt_s / 3600)
            elif q > 0:
                q = max(0.0, q - (1 - r) * s["cap"] * dt_s / 3600)
            self.queue[key] = q
            v = s["free"] * max(0.45, 1 - 0.5 * min(r, 1.0) ** 6)
            if q > 3:
                v = min(v, BOTTLENECK_KMH)
            speed[key] = v
        # queues spill back upstream along the same road
        queued = {}
        for key, q in self.queue.items():
            if q <= 3:
                continue
            sid, fwd = key
            length = q * JAM_M_PER_VEHICLE / self.seg[sid]["lanes"]
            road = self.seg[sid]["road"]
            cur, covered, seen = key, 0.0, {key}
            while covered < length:
                s = self.graph[cur[0]]
                node = s.from_node if cur[1] else s.to_node
                preds = [k for k in self.enters.get(node, ()) if k[0] != cur[0] and k not in seen]
                if not preds:
                    break
                same = [k for k in preds if self.seg[k[0]]["road"] == road]
                nxt = max(same or preds, key=lambda k: self.seg[k[0]]["cap"])
                seen.add(nxt)
                speed[nxt] = min(speed[nxt], QUEUE_KMH)
                queued[nxt] = key
                covered += self.graph[nxt[0]].length
                cur = nxt
        self.speed = speed
        self.queued = queued

    def vehicles_on(self, sid: int, when) -> tuple[int, int] | None:
        """Vehicles on a main road segment (both directions) and their average speed, for the road
        operator's loop detectors and cameras: density = flow / speed, flow = load x capacity."""
        s = self.seg.get(sid)
        if s is None:
            return None
        hour = when.hour + when.minute / 60 + when.second / 3600
        count, speeds = 0.0, []
        for fwd in (True, False):
            v = self.speed.get((sid, fwd))
            if v is None:
                continue
            flow = s["load"] * self.factor(s["flow"] if fwd else INVERT[s["flow"]], hour) * s["cap"]
            count += flow / max(v, 5.0) * self.graph[sid].length / 1000 + self.queue.get((sid, fwd), 0.0)
            speeds.append(v)
        return (round(count), round(sum(speeds) / len(speeds))) if speeds else None

    def kmh(self, sid: int, fwd: bool):
        """Current speed on a main road segment, or None for minor roads."""
        return self.speed.get((sid, fwd))

    def summary(self) -> str:
        jams = [(k, q) for k, q in self.queue.items() if q > 3]
        km = sum(q * JAM_M_PER_VEHICLE / self.seg[k[0]]["lanes"] for k, q in jams) / 1000
        return f"{len(jams)} queues, {km:.1f} km"
