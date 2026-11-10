"""Suggested diversions around confirmed incidents that block a road, on the OSM road network.

For an incident with a position and a direction of travel: find the road segment it blocks, go back along the
same road to a point where traffic can still turn off, and forward to a point where it can join again, then find
the fastest route between the two that avoids the blocked segment. Speeds are the posted limits (or a default per
road class), with a penalty for small roads so that the diversion keeps to main roads where it can. It doesn't know
about traffic or turn restrictions: it is a suggestion for the map, not navigation.
"""
import heapq
import math
import re

KX, KY = 74320.0, 110540.0          # metres per degree of longitude / latitude in Munich
DEFAULT_KMH = {"motorway": 120, "motorway_link": 60, "trunk": 80, "trunk_link": 50, "primary": 50, "primary_link": 40,
               "secondary": 50, "secondary_link": 40, "tertiary": 40, "tertiary_link": 30, "unclassified": 30,
               "residential": 30, "living_street": 7}
PENALTY = {"tertiary": 1.2, "tertiary_link": 1.2, "unclassified": 1.6, "residential": 2.5, "living_street": 8.0}
HEADING_MAX = 60
SEARCH_STEPS_M = (400, 1000, 2500, 6000)   # how far back and ahead to look for a place to turn off and rejoin
_NUM = r"-?\d+(?:\.\d+)?"


def _coords(wkt):
    return [(float(x), float(y)) for x, y in re.findall(rf"({_NUM})\s+({_NUM})", wkt or "")]


def _angle(a, b):
    return abs((a - b + 540) % 360 - 180)


class RoadGraph:
    def __init__(self, rows):
        self.seg = {}
        self.out = {}        # node -> [(segment, forward, next node)]
        self.into = {}       # node -> [(segment, forward, previous node)]
        self.grid = {}       # (cell x, cell y) -> segments, cells of about 250 m
        for r in rows:
            sid = int(r["segment_id"])
            a, b, ow = int(r["from_node"]), int(r["to_node"]), int(r["oneway"] or 0)
            rc = r["road_class"] or "residential"
            kmh = float(r["maxspeed_kmh"]) if r["maxspeed_kmh"] else DEFAULT_KMH.get(rc, 40)
            length = float(r["length_m"] or 1.0)
            pts = _coords(r["geometry_wkt"])
            if len(pts) < 2:
                continue
            s = {"id": sid, "a": a, "b": b, "oneway": ow, "class": rc, "name": r["name"] or "", "ref": r["ref"] or "",
                 "len": length, "hdg": float(r["heading_deg"] or 0.0), "pts": pts,
                 "cost": length / (kmh / 3.6) * PENALTY.get(rc, 1.0), "secs": length / (kmh / 3.6)}
            self.seg[sid] = s
            if ow in (0, 1):
                self.out.setdefault(a, []).append((sid, True, b))
                self.into.setdefault(b, []).append((sid, True, a))
            if ow in (0, -1):
                self.out.setdefault(b, []).append((sid, False, a))
                self.into.setdefault(a, []).append((sid, False, b))
            for x, y in pts[:: max(1, len(pts) // 4)] + [pts[-1]]:
                self.grid.setdefault((int(x * KX / 250), int(y * KY / 250)), set()).add(sid)

    # ---- the blocked segment
    def blocked(self, lat, lon, hdg):
        """(segment, forward) the incident blocks: the nearest segment within 150 m whose travel direction matches."""
        cx, cy = int(lon * KX / 250), int(lat * KY / 250)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for sid in self.grid.get((cx + dx, cy + dy), ()):
                    s = self.seg[sid]
                    d = min(math.hypot((x - lon) * KX, (y - lat) * KY) for x, y in s["pts"])
                    if d > 150:
                        continue
                    for fwd in (True, False):
                        if (fwd and s["oneway"] == -1) or (not fwd and s["oneway"] == 1):
                            continue
                        travel = s["hdg"] if fwd else (s["hdg"] + 180) % 360
                        if hdg is not None and _angle(travel, hdg) > HEADING_MAX:
                            continue
                        if best is None or d < best[0]:
                            best = (d, sid, fwd)
        return (best[1], best[2]) if best else None

    def _same_road(self, s, t):
        return (s["ref"] and s["ref"] == t["ref"]) or (s["name"] and s["name"] == t["name"])

    def _travel(self, sid, fwd):
        s = self.seg[sid]
        return s["hdg"] if fwd else (s["hdg"] + 180) % 360

    def _walk(self, sid, fwd, metres, back):
        """Follow the road back (or ahead) from the blocked segment for about `metres`: the same road by name or
        number, else the straightest continuation (up to 50 degrees). Returns (end node, metres, segments, nodes);
        nodes are [(node, metres so far, segments so far)] along the way."""
        s = self.seg[sid]
        node = (s["a"] if fwd else s["b"]) if back else (s["b"] if fwd else s["a"])
        done, path, prev, prev_fwd, nodes, seen = 0.0, [], s, fwd, [(node, 0.0, [])], {sid}
        while done < metres:
            here = self._travel(prev["id"], prev_fwd)
            cands = [(t, f, n) for t, f, n in (self.into if back else self.out).get(node, ()) if t not in seen]
            same = [c for c in cands if self._same_road(self.seg[c[0]], prev)]
            pick = same or [c for c in cands if _angle(self._travel(c[0], c[1]), here) <= 50]
            if not pick:
                break
            t, f, n = min(pick, key=lambda c: _angle(self._travel(c[0], c[1]), here))
            done += self.seg[t]["len"]
            path.append((t, f))
            seen.add(t)
            prev, prev_fwd, node = self.seg[t], f, n
            nodes.append((node, done, list(path)))
        return node, done, path, nodes

    def upstream(self, lat, lon, hdg, metres=2000):
        """The stretch of the same road before the incident, against the traffic: where drivers get warned."""
        hit = self.blocked(lat, lon, hdg)
        if hit is None:
            return []
        sid, fwd = hit
        _, _, path, _ = self._walk(sid, fwd, metres, back=True)
        lines = []
        for s, f in [(sid, fwd)] + path:
            pts = self.seg[s]["pts"]
            lines.append(pts if f else pts[::-1])
        return lines

    # ---- routing
    def route(self, start, goals, banned, max_cost=3600.0):
        """The cheapest path from start to the first of `goals` it reaches: (goal, [(segment, forward)]) or None."""
        goals = set(goals) - {start}
        dist, prev, heap, hit = {start: 0.0}, {}, [(0.0, start)], None
        while heap:
            c, n = heapq.heappop(heap)
            if n in goals:
                hit = n
                break
            if c > dist.get(n, 1e18) or c > max_cost:
                continue
            for sid, fwd, m in self.out.get(n, ()):
                if sid in banned:
                    continue
                nc = c + self.seg[sid]["cost"]
                if nc < dist.get(m, 1e18):
                    dist[m], prev[m] = nc, (n, sid, fwd)
                    heapq.heappush(heap, (nc, m))
        if hit is None:
            return None
        path, n = [], hit
        while n != start:
            p, sid, fwd = prev[n]
            path.append((sid, fwd))
            n = p
        return hit, path[::-1]

    def diversion(self, lat, lon, hdg):
        """A suggested diversion, or None: {"path", "km", "extra_min", "via", "lines"}."""
        hit = self.blocked(lat, lon, hdg)
        if hit is None:
            return None
        sid, fwd = hit
        for step in SEARCH_STEPS_M:
            u, up_m, up_path, _ = self._walk(sid, fwd, step, back=True)
            _, _, _, ahead = self._walk(sid, fwd, step, back=False)
            found = self.route(u, [n for n, _, _ in ahead], banned={sid})
            if found:
                goal, path = found
                down_path = next(p for n, _, p in ahead if n == goal)
                break
        else:
            return None
        normal = up_path[::-1] + [(sid, fwd)] + down_path
        secs = sum(self.seg[s]["secs"] for s, _ in path)
        normal_secs = sum(self.seg[s]["secs"] for s, _ in normal)
        length = sum(self.seg[s]["len"] for s, _ in path)
        road = self.seg[sid]
        by_name = {}
        for s, _ in path:
            t = self.seg[s]
            label = t["name"] or t["ref"]
            if label and not self._same_road(t, road):
                by_name[label] = by_name.get(label, 0.0) + t["len"]
        via = [n for n, m in sorted(by_name.items(), key=lambda x: -x[1]) if m >= 80][:3]
        # Draw only the part off the blocked road: from where it turns off to where it rejoins (the stretch before
        # the turn-off is the warned stretch, drawn on its own). The length and time above cover the whole route.
        off = [not self._same_road(self.seg[s], road) for s, _ in path]
        first = next((i for i, o in enumerate(off) if o), 0)
        last = len(off) - next((i for i, o in enumerate(reversed(off)) if o), 0)
        lines = []
        for s, f in path[first:last]:
            pts = self.seg[s]["pts"]
            lines.append(pts if f else pts[::-1])
        return {"path": path, "km": length / 1000, "extra_min": max(0.0, (secs - normal_secs) / 60),
                "via": via, "lines": lines, "blocked": road["name"] or road["ref"]}
