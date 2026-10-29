"""
Vessel simulation + maritime monitoring.

Vessels are updated in the SAME bulk_write as trucks (same collection, different fields).
Monitoring runs as MongoDB queries against that collection:
  * AIS watchdog      - partial index on ais.last_msg finds vessels that went silent ("dark")
  * Close quarters    - $geoNear between vessels under way
  * Harbour speeding  - $geoWithin + speed_kn filter (in Simulator.geofence, driven by zone rules)
  * Protected zone    - $geoWithin per zone (in Simulator.geofence)
"""
import asyncio
import logging
import random
import time
from datetime import timedelta

from pymongo import UpdateOne

from . import config
from . import maritime_data as M
from .geo_data import cumulative, haversine, interpolate

log = logging.getLogger("fleet.vessels")
KN = 0.514444  # m/s per knot


class Vessel:
    def __init__(self, d):
        self.id = d["_id"]
        self.name = d.get("name", self.id)
        self.type = d["type"]
        self.prof = M.VESSEL_TYPES[self.type]
        self.status = d["status"]
        lng, lat = d["location"]["coordinates"]
        self.pos = (lat, lng)
        self.heading = d.get("heading", 0.0)
        self.speed = d.get("speed_kn", 0.0)
        self.target = random.uniform(*self.prof["kn"])
        v = d.get("voyage", {})
        self.voyage = {k: v[k] for k in ("phase", "port", "berth", "last_port", "next_port", "exit", "anchored_since")
                       if k in v}
        self.phase = v.get("phase", "moored")
        self.compliant = d.get("compliant", True)
        self.reckless = self.type == "tug" and random.random() < 0.5 or random.random() < 0.06
        self.path, self.cum, self.s = None, None, 0.0
        self.wait_until = time.monotonic() + random.uniform(20, 500)
        self.dark_until = 0.0
        self.dark_from = None
        self.dark_started = 0.0
        self.dirty = False
        self.voyage_changed = False
        self.eta = None

    @property
    def remaining(self):
        return (self.cum[-1] - self.s) if self.path else 0.0


class VesselEngine:
    def __init__(self, db):
        self.db = db
        self.graph = M.sea_graph()
        self.vessels = {}
        self.pair_cooldown = {}
        self.tick_no = 0

    async def load(self):
        docs = await self.db.vehicles.find({"kind": "vessel", "type": {"$in": list(M.VESSEL_TYPES)}}).to_list(None)
        self.vessels = {d["_id"]: Vessel(d) for d in docs}
        for v in self.vessels.values():
            self.resume_phase(v)
        log.info("vessel engine loaded %d vessels", len(self.vessels))

    # ------------------------------------------------------------ voyage planning
    def berth_node(self, v):
        return M.PORTS[v.prof["port"]][v.voyage.get("berth") or v.prof["berths"][0]]

    def plan(self, v, goal_pt, goal_node=None):
        goal_node = goal_node or self.graph.nearest_node(goal_pt)
        nodes = self.graph.route_from(v.pos, goal_node, avoid_restricted=v.compliant)
        path = [v.pos] + nodes
        if haversine(goal_pt, goal_node) > 20:
            path.append(goal_pt)
        if len(path) > 2 and haversine(path[0], path[1]) < 30:
            path.pop(1)
        if len(path) < 2:
            path = [v.pos, goal_pt]
        v.path, v.cum, v.s = path, cumulative(path), 0.0

    def resume_phase(self, v):
        p = v.phase
        if p == "inbound":
            goal = M.anchor_spot(random) if v.prof["anchor"] else M.berth_spot(random, self.berth_node(v))
            self.plan(v, goal)
        elif p == "approach":
            self.plan(v, M.berth_spot(random, self.berth_node(v)))
        elif p == "outbound":
            self.plan(v, M.N[v.voyage.get("exit") or "HORMUZ_EXIT"])
        elif p in ("assist", "return"):
            self.plan(v, M.N["JA_BASIN"] if p == "return" else random.choice([M.N["JA_OUTER"], M.N["JA_ANCH"]]))

    def start_next(self, v, now):
        """leave anchorage / berth"""
        v.status, v.speed = "underway", 3.0
        if v.type == "tug":
            v.phase = "assist"
            self.plan(v, random.choice([M.N["JA_OUTER"], M.N["JA_ANCH"], M.N["JA_CHAN"]]))
        elif v.phase == "anchored":
            v.phase = "approach"
            v.voyage.pop("anchored_since", None)
            self.plan(v, M.berth_spot(random, self.berth_node(v)))
        else:  # moored -> sail out
            exit_key = random.choice(v.prof["exits"])
            v.phase = "outbound"
            foreign = "Khasab" if v.type == "dhow" else random.choice(M.EXITS[exit_key])
            v.voyage.update(exit=exit_key, last_port=v.prof["port"], next_port=foreign)
            self.plan(v, M.N[exit_key])
        v.voyage["phase"] = v.phase
        v.voyage_changed = True

    def arrive(self, v, now, now_m):
        v.speed = 0.0
        if v.phase == "inbound" and v.prof["anchor"]:
            v.phase, v.status = "anchored", "anchored"
            v.voyage["anchored_since"] = now - timedelta(minutes=random.randint(15, 300))
            v.wait_until = now_m + random.uniform(300, 700)
        elif v.phase in ("inbound", "approach"):
            v.phase, v.status = "moored", "moored"
            v.wait_until = now_m + (random.uniform(300, 600) if v.type == "passenger" else random.uniform(150, 360))
        elif v.phase == "assist":
            v.phase, v.status = "return", "underway"
            self.plan(v, M.berth_spot(random, "JA_BASIN", 120), M.N["JA_BASIN"])
            v.speed = 6.0
        elif v.phase == "return":
            v.phase, v.status = "moored", "moored"
            v.wait_until = now_m + random.uniform(20, 90)
        elif v.phase == "outbound":
            # left the area: re-enter later as a new inbound voyage from one of the exits
            exit_key = random.choice(v.prof["exits"])
            v.pos = M.N[exit_key]
            foreign = "Khasab" if v.type == "dhow" else random.choice(M.EXITS[exit_key])
            v.voyage = {"phase": "inbound", "port": v.prof["port"], "berth": random.choice(v.prof["berths"]),
                        "last_port": foreign, "next_port": v.prof["port"], "exit": exit_key}
            v.phase, v.status, v.speed = "inbound", "underway", v.target
            goal = M.anchor_spot(random) if v.prof["anchor"] else M.berth_spot(random, self.berth_node(v))
            self.plan(v, goal)
        v.voyage["phase"] = v.phase
        v.voyage_changed = True

    # ------------------------------------------------------------ per tick
    def step(self, v, dt, now, now_m, events):
        if v.status in ("moored", "anchored"):
            if now_m >= v.wait_until:
                self.start_next(v, now)
                v.dirty = True
            elif self.tick_no % 15 == hash(v.id) % 15:  # AIS heartbeat while stationary, swinging at anchor
                v.heading = (v.heading + random.uniform(-8, 8)) % 360
                v.dirty = True
            return

        # under way
        in_harbour = M.in_harbour(v.pos)
        target = v.target
        if in_harbour and not v.reckless:
            target = random.uniform(3.2, 4.5)
        if v.remaining < 1500 and not v.reckless:
            target = min(target, 4.0)
        if v.type == "tug" and v.reckless:
            target = max(target, 10.5)
        v.speed += (target - v.speed) * 0.3 + random.gauss(0, 0.25)
        v.speed = max(1.5, min(v.prof["kn"][1] + 2, v.speed))
        v.s += v.speed * KN * dt
        v.pos, v.heading = interpolate(v.path, v.cum, v.s)

        if v.dark_until:
            if now_m >= v.dark_until:  # transponder back on
                gap = now_m - v.dark_started
                jump = haversine(v.dark_from, v.pos) / 1000
                v.dark_until = 0.0
                events.append(self.event(v, now, "ais_resumed", "info",
                                         f"{v.name} AIS back after {gap:.0f}s silence - reappeared {jump:.1f} km from last fix"))
                v.dirty = True
            if v.s >= v.cum[-1]:
                v.dark_until = 0.0
            else:
                return  # silent: nothing written

        if v.s >= v.cum[-1]:
            self.arrive(v, now, now_m)
            v.dirty = True
            return

        # occasionally a vessel switches its transponder off in open water
        if (v.phase in ("inbound", "outbound") and not in_harbour and v.remaining > 12000
                and v.type != "passenger" and random.random() < 0.0005):
            v.dark_until = now_m + random.uniform(35, 80)
            v.dark_from, v.dark_started = v.pos, now_m
            return
        v.dirty = True

    def op(self, v, now):
        s = {
            "status": v.status,
            "location": {"type": "Point", "coordinates": [round(v.pos[1], 6), round(v.pos[0], 6)]},
            "heading": round(v.heading, 1),
            "speed_kn": round(v.speed, 1),
            "ais.last_msg": now,                       # nested fields updated in place
            "ais.nav_status": M.nav_status(v.status),
            "last_update": now,
        }
        if v.phase in ("inbound", "approach") and v.path:
            v.eta = now + timedelta(seconds=v.remaining / (max(v.speed, 4) * KN * config.VESSEL_TIME_SCALE))
        if v.voyage_changed:
            voyage = dict(v.voyage)
            if v.phase in ("inbound", "approach"):
                voyage["eta"] = v.eta
            s["voyage"] = voyage
            v.voyage_changed = False
        elif v.phase in ("inbound", "approach") and self.tick_no % 5 == 0:
            s["voyage.eta"] = v.eta
        v.dirty = False
        return UpdateOne({"_id": v.id}, {"$set": s})

    def tick_ops(self, now, now_m, events):
        self.tick_no += 1
        dt = config.TICK_SECONDS * config.VESSEL_TIME_SCALE
        ops = []
        for v in self.vessels.values():
            self.step(v, dt, now, now_m, events)
            if v.dirty:
                ops.append(self.op(v, now))
        return ops

    def telemetry(self, now):
        return [{
            "ts": now,
            "meta": {"vid": v.id, "type": v.type, "kind": "vessel"},
            "location": {"type": "Point", "coordinates": [round(v.pos[1], 6), round(v.pos[0], 6)]},
            "speed_kn": round(v.speed, 1),       # time-series measurements can differ per kind too
            "status": v.status,
        } for v in self.vessels.values() if v.status == "underway" and not v.dark_until]

    # ------------------------------------------------------------ monitors (MongoDB queries)
    async def ais_watchdog(self, now, events):
        """Vessels 'under way' whose last AIS message is too old -> mark dark. Uses the partial index."""
        cutoff = now - timedelta(seconds=config.AIS_GAP_SECONDS)
        stale = await self.db.vehicles.find(
            {"kind": "vessel", "status": "underway", "ais.last_msg": {"$lt": cutoff}},
            {"name": 1, "type": 1, "ais.last_msg": 1, "location": 1}).to_list(None)
        if not stale:
            return
        await self.db.vehicles.update_many({"_id": {"$in": [d["_id"] for d in stale]}},
                                           {"$set": {"status": "dark", "ais.nav_status": "No signal"}})
        for d in stale:
            secs = (now - d["ais"]["last_msg"]).total_seconds()
            e = {"ts": now, "kind": "ais_gap", "severity": "warning", "vehicle_kind": "vessel",
                 "vehicle_id": d["_id"], "name": d.get("name"), "vtype": d.get("type"),
                 "location": d["location"],
                 "message": f"{d.get('name')} ({d.get('type', '').replace('_', ' ')}) went dark - no AIS for {secs:.0f}s"}
            events.append(e)

    async def close_quarters(self, now, events):
        cands = [v for v in self.vessels.values()
                 if v.status == "underway" and not v.dark_until and v.type != "tug" and not M.in_harbour(v.pos)]
        now_m = time.monotonic()

        async def nearest(v):
            pipeline = [
                {"$geoNear": {"near": {"type": "Point", "coordinates": [v.pos[1], v.pos[0]]},
                              "distanceField": "d", "spherical": True, "key": "location",
                              "maxDistance": config.CPA_METERS,
                              "query": {"kind": "vessel", "status": "underway", "_id": {"$ne": v.id},
                                        "type": {"$ne": "tug"}}}},
                {"$limit": 1}, {"$project": {"name": 1, "type": 1, "d": 1}},
            ]
            cur = await self.db.vehicles.aggregate(pipeline)
            return v, await cur.to_list(1)

        results = await asyncio.gather(*[nearest(v) for v in cands], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) or not r[1]:
                continue
            v, (o,) = r[0], r[1]
            if v.type == "dhow" and o.get("type") == "dhow":
                continue  # dhows sail in loose convoys on the same lane
            key = frozenset((v.id, o["_id"]))
            # one alert per pair, and at most one per vessel every 10 minutes
            if (now_m - self.pair_cooldown.get(key, -1e9) < 900 or
                    now_m - self.pair_cooldown.get(v.id, -1e9) < 600 or now_m - self.pair_cooldown.get(o["_id"], -1e9) < 600):
                continue
            self.pair_cooldown[key] = self.pair_cooldown[v.id] = self.pair_cooldown[o["_id"]] = now_m
            events.append(self.event(v, now, "close_quarters", "warning",
                                     f"Close quarters: {v.name} and {o.get('name')} {o['d']:.0f} m apart, both under way",
                                     other_id=o["_id"]))

    def event(self, v, now, kind, severity, message, **extra):
        return {"ts": now, "kind": kind, "severity": severity, "message": message, "vehicle_kind": "vessel",
                "vehicle_id": v.id, "name": v.name, "vtype": v.type,
                "location": {"type": "Point", "coordinates": [round(v.pos[1], 6), round(v.pos[0], 6)]}, **extra}
