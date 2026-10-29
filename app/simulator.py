"""
Fleet simulator: moves trucks along Dubai's truck corridors and writes to Atlas.

It knows nothing about the UI. The UI only listens to MongoDB change streams,
so you can run this as a separate process to make that point on stage:

    python -m app.simulator
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from pymongo import UpdateOne

from . import config
from .vessel_sim import VesselEngine
from .geo_data import BAN_HOURS, HUBS, TYPE_HUBS, RoadGraph, cumulative, haversine, interpolate

log = logging.getLogger("fleet.sim")
DUBAI_TZ = timezone(timedelta(hours=4))


def in_ban_hours(now_utc=None):
    if config.BAN_ALWAYS_ON:
        return True
    t = (now_utc or datetime.now(timezone.utc)).astimezone(DUBAI_TZ)
    h = t.hour + t.minute / 60
    return any(a <= h < b for a, b in BAN_HOURS)


class Truck:
    __slots__ = ("id", "plate", "type", "status", "pos", "heading", "speed", "target", "fuel", "path", "cum",
                 "s", "dest", "origin", "compliant", "wait_until", "to_depot", "low_fuel_flag", "reefer_temp",
                 "setpoint", "excursion_until", "dirty", "trip_changed")

    def __init__(self, d):
        self.id = d["_id"]
        self.plate = d.get("plate")
        self.type = d["type"]
        self.status = d["status"]
        lng, lat = d["location"]["coordinates"]
        self.pos = (lat, lng)
        self.heading = d.get("heading", 0)
        self.speed = d.get("speed_kmh", 0)
        self.target = random.uniform(62, 92)
        self.fuel = d.get("fuel_pct", 60)
        self.dest = d.get("trip", {}).get("dest")
        self.origin = d.get("trip", {}).get("origin")
        self.compliant = d.get("driver", {}).get("compliant", True)
        self.path, self.cum, self.s = None, None, 0.0
        self.wait_until = time.monotonic() + random.uniform(5, 90)
        self.to_depot = None
        self.low_fuel_flag = self.fuel < 12
        self.reefer_temp = d.get("reefer_temp_c")
        self.setpoint = d.get("reefer_setpoint_c")
        self.excursion_until = 0
        self.dirty = False
        self.trip_changed = False


class Simulator:
    def __init__(self, db):
        self.db = db
        self.graph = RoadGraph()
        self.trucks = {}
        self.vessels = VesselEngine(db)
        self.depots = []
        self.zones = []
        self.membership = {}
        self.zone_counts = {}
        self.tick_no = 0
        self.running = False

    # ------------------------------------------------------------ setup
    async def load(self):
        # same collection, two shapes: pick each kind by its discriminator
        docs = await self.db.vehicles.find({"kind": {"$ne": "vessel"}}).to_list(None)
        self.trucks = {d["_id"]: Truck(d) for d in docs if "plate" in d}
        await self.vessels.load()
        self.depots = await self.db.depots.find({}).to_list(None)
        for t in self.trucks.values():
            if t.status == "moving":
                self.plan(t, t.dest or self.pick_hub(t))
        await self.refresh_zones()
        log.info("simulator loaded %d trucks, %d vessels, %d zones",
                 len(self.trucks), len(self.vessels.vessels), len(self.zones))

    def lookup(self, vid):
        return self.trucks.get(vid) or self.vessels.vessels.get(vid)

    async def refresh_zones(self):
        self.zones = await self.db.zones.find({}, {"name": 1, "kind": 1, "geometry": 1, "rules": 1}).to_list(None)
        ids = {z["_id"] for z in self.zones}
        for zid in list(self.membership):
            if zid not in ids:
                self.membership.pop(zid, None)

    def pick_hub(self, t, exclude=None):
        hubs = [(h, w) for h, w in TYPE_HUBS[t.type].items() if h != exclude]
        r = random.uniform(0, sum(w for _, w in hubs))
        for h, w in hubs:
            r -= w
            if r <= 0:
                return h
        return hubs[-1][0]

    def plan(self, t, dest_hub=None, dest_pt=None):
        goal = dest_pt or HUBS[dest_hub]["pt"]
        nodes = self.graph.route_from(t.pos, goal, avoid_restricted=t.compliant)
        path = [t.pos] + nodes if haversine(t.pos, nodes[0]) > 30 else nodes
        if len(path) < 2:
            path = [t.pos, goal]
        t.path, t.cum, t.s = path, cumulative(path), 0.0
        if dest_hub:
            t.origin, t.dest = t.dest if t.dest != dest_hub else t.origin, dest_hub
        t.trip_changed = True

    # ------------------------------------------------------------ main loop
    async def run(self):
        self.running = True
        await self.load()
        while self.running:
            started = time.monotonic()
            try:
                await self.tick()
            except Exception:  # keep the demo alive through transient network blips
                log.exception("tick failed")
                await asyncio.sleep(2)
            await asyncio.sleep(max(0.05, config.TICK_SECONDS - (time.monotonic() - started)))

    def stop(self):
        self.running = False

    async def tick(self):
        self.tick_no += 1
        now_m = time.monotonic()
        now = datetime.now(timezone.utc)
        dt = config.TICK_SECONDS * config.TIME_SCALE
        events, reroute = [], []
        ops = []

        for t in self.trucks.values():
            if t.status in ("offline",):
                continue
            if t.status == "maintenance":
                if random.random() < 0.0008:  # released from the workshop
                    t.status, t.wait_until, t.dirty = "idle", now_m + 10, True
                else:
                    continue

            if t.status in ("loading", "idle"):
                if now_m >= t.wait_until:
                    t.status = "moving"
                    t.speed = 15.0
                    if not (t.path and t.s < t.cum[-1]):  # resume an unfinished trip, else a new job
                        self.plan(t, self.pick_hub(t, exclude=t.dest))
                    t.dirty = True
            elif t.status == "moving":
                self.step(t, dt, now_m, reroute)
                t.dirty = True

            if t.type == "reefer" and t.reefer_temp is not None:
                self.reefer(t, now_m, events, now)

            if t.dirty:
                s = {
                    "status": t.status,
                    "location": {"type": "Point", "coordinates": [round(t.pos[1], 6), round(t.pos[0], 6)]},
                    "heading": round(t.heading, 1),
                    "speed_kmh": round(t.speed, 1),
                    "fuel_pct": round(t.fuel, 1),
                    "last_update": now,
                }
                if t.reefer_temp is not None:
                    s["reefer_temp_c"] = round(t.reefer_temp, 1)
                if t.trip_changed:
                    if t.to_depot:
                        s["trip"] = {"origin": t.origin, "origin_name": HUBS.get(t.origin, {}).get("name"),
                                     "dest": "DEPOT", "dest_name": t.to_depot["name"]}
                    else:
                        s["trip"] = {"origin": t.origin, "origin_name": HUBS.get(t.origin, {}).get("name"),
                                     "dest": t.dest, "dest_name": HUBS[t.dest]["name"]}
                    t.trip_changed = False
                ops.append(UpdateOne({"_id": t.id}, {"$set": s}))
                t.dirty = False

        ops += self.vessels.tick_ops(now, now_m, events)
        if ops:  # trucks and vessels in ONE bulk write to ONE collection
            await self.db.vehicles.bulk_write(ops, ordered=False)

        for t in reroute:
            await self.send_to_depot(t, events, now)

        if self.tick_no % config.TELEMETRY_EVERY == 0:
            await self.write_telemetry(now)

        if self.tick_no % 15 == 0:
            await self.refresh_zones()
        if self.tick_no % config.GEOFENCE_EVERY == 0:
            await self.geofence(events, now)

        if self.vessels.vessels:
            if self.tick_no % 3 == 0:
                await self.vessels.ais_watchdog(now, events)
            if self.tick_no % 5 == 2:
                await self.vessels.close_quarters(now, events)

        if events:
            await self.db.events.insert_many(events, ordered=False)

    def step(self, t, dt, now_m, reroute):
        # speed wanders around the driver's target; slower when heavily loaded near junctions
        t.speed += (t.target - t.speed) * 0.25 + random.gauss(0, 4)
        t.speed = max(8.0, min(110.0, t.speed))
        if random.random() < 0.02:
            t.target = random.uniform(55, 95)
        dist = t.speed / 3.6 * dt
        t.s += dist
        t.fuel = max(0.0, t.fuel - dist / 1000 * 0.11)
        t.pos, t.heading = interpolate(t.path, t.cum, t.s)

        if t.s >= t.cum[-1]:
            t.speed = 0.0
            if t.to_depot:
                t.fuel, t.to_depot, t.low_fuel_flag = 100.0, None, False
                t.status, t.wait_until = "idle", now_m + random.uniform(15, 40)
            else:
                t.status, t.wait_until = "loading", now_m + random.uniform(40, 160)
                j = HUBS[t.dest]["pt"]
                t.pos = (j[0] + random.gauss(0, 0.0022), j[1] + random.gauss(0, 0.0022))
            return

        if t.fuel < 12 and not t.low_fuel_flag:
            t.low_fuel_flag = True
            reroute.append(t)
        elif random.random() < 0.0012:  # driver break / traffic stop
            t.status, t.speed, t.wait_until = "idle", 0.0, now_m + random.uniform(20, 70)

    def reefer(self, t, now_m, events, now):
        if t.excursion_until and now_m < t.excursion_until:
            t.reefer_temp += random.uniform(0.2, 0.8)
        elif t.excursion_until:
            t.excursion_until = 0
        else:
            t.reefer_temp += (t.setpoint - t.reefer_temp) * 0.3 + random.gauss(0, 0.15)
            if t.status == "moving" and random.random() < 0.0004:
                t.excursion_until = now_m + random.uniform(20, 45)
                events.append(self.event(t, now, "cold_chain", "warning",
                                         f"{t.id} reefer unit fault - temperature rising above {t.setpoint}°C setpoint"))
        if t.status != "moving" and self.tick_no % 5 == 0:
            t.dirty = True

    async def send_to_depot(self, t, events, now):
        """Low fuel -> ask MongoDB for the nearest fuel/service depot with $geoNear, then reroute."""
        pipeline = [
            {"$geoNear": {"near": {"type": "Point", "coordinates": [t.pos[1], t.pos[0]]},
                          "distanceField": "d", "spherical": True, "key": "location"}},
            {"$limit": 1},
        ]
        cur = await self.db.depots.aggregate(pipeline)
        best = await cur.to_list(1)
        if not best:
            return
        dep = best[0]
        lng, lat = dep["location"]["coordinates"]
        t.to_depot = {"name": dep["name"]}
        self.plan(t, dest_pt=(lat, lng))
        events.append(self.event(t, now, "low_fuel", "warning",
                                 f"{t.id} fuel at {t.fuel:.0f}% - rerouted to {dep['name']} ({dep['d']/1000:.1f} km, via $geoNear)"))

    async def write_telemetry(self, now):
        docs = [{
            "ts": now,
            "meta": {"vid": t.id, "type": t.type, "kind": "truck"},
            "location": {"type": "Point", "coordinates": [round(t.pos[1], 6), round(t.pos[0], 6)]},
            "speed": round(t.speed, 1),
            "fuel": round(t.fuel, 1),
            "status": t.status,
        } for t in self.trucks.values() if t.status == "moving"]
        docs += self.vessels.telemetry(now)
        if docs:
            await self.db.telemetry.insert_many(docs, ordered=False)

    async def geofence(self, events, now):
        """
        One indexed $geoWithin per zone; diff with the previous tick to find enter/exit.
        Zone rules are data too: applies_to (truck/vessel), ban_hours, exempt_types, max_speed_kn.
        """
        def base(z):
            q = {"location": {"$geoWithin": {"$geometry": z["geometry"]}}, "status": {"$nin": ["offline", "dark"]}}
            kinds = (z.get("rules") or {}).get("applies_to")
            if kinds:
                q["kind"] = {"$in": kinds}
            return q

        async def members(z):
            cur = self.db.vehicles.find(base(z), {"_id": 1})
            return {d["_id"] for d in await cur.to_list(None)}

        async def speeders(z):
            q = base(z)
            q["speed_kn"] = {"$gt": z["rules"]["max_speed_kn"]}
            cur = self.db.vehicles.find(q, {"speed_kn": 1})
            return {d["_id"]: d["speed_kn"] for d in await cur.to_list(None)}

        speed_zones = [z for z in self.zones if (z.get("rules") or {}).get("max_speed_kn")]
        results, speed_results = await asyncio.gather(
            asyncio.gather(*[members(z) for z in self.zones], return_exceptions=True),
            asyncio.gather(*[speeders(z) for z in speed_zones], return_exceptions=True))
        count_ops = []
        ban = in_ban_hours(now)
        for z, ids in zip(self.zones, results):
            if isinstance(ids, Exception):
                continue
            zid, rules = z["_id"], z.get("rules") or {}
            prev = self.membership.get(zid)
            self.membership[zid] = ids
            if self.zone_counts.get(zid) != len(ids):
                self.zone_counts[zid] = len(ids)
                count_ops.append(UpdateOne({"_id": zid}, {"$set": {"inside_count": len(ids)}}))
            if prev is None:
                continue
            alert_on = rules.get("alert_on", ["enter", "exit"])
            for vid in ids - prev:
                t = self.lookup(vid)
                if not t:
                    continue
                label = self.label(t)
                if z["kind"] in ("restricted", "protected"):
                    if "ban_hours" in rules and not ban:
                        continue
                    if t.type in rules.get("exempt_types", []):
                        continue
                    why = "during truck-ban hours" if "ban_hours" in rules else "- protected area violation"
                    events.append(self.event(t, now, "violation", "critical", f"{label} entered {z['name']} {why}", z))
                elif "enter" in alert_on:
                    events.append(self.event(t, now, "enter", "info", f"{label} entered {z['name']}", z))
            if "exit" in alert_on and z["kind"] not in ("restricted", "protected"):
                for vid in prev - ids:
                    t = self.lookup(vid)
                    if t:
                        events.append(self.event(t, now, "exit", "info", f"{self.label(t)} left {z['name']}", z))

        for z, fast in zip(speed_zones, speed_results):
            if isinstance(fast, Exception):
                continue
            key = ("speed", z["_id"])
            prev = self.membership.get(key)
            self.membership[key] = set(fast)
            if prev is None:
                continue
            for vid in set(fast) - prev:
                t = self.lookup(vid)
                if t:
                    events.append(self.event(
                        t, now, "speeding", "warning",
                        f"{self.label(t)} doing {fast[vid]:.1f} kn in {z['name'].split(' (')[0]} "
                        f"(limit {z['rules']['max_speed_kn']} kn)", z))
        if count_ops:
            await self.db.zones.bulk_write(count_ops, ordered=False)

    @staticmethod
    def label(t):
        if hasattr(t, "plate"):
            return f"{t.id} ({t.type})"
        return f"{t.name} ({t.type.replace('_', ' ')})"

    def event(self, t, now, kind, severity, message, zone=None):
        is_truck = hasattr(t, "plate")
        e = {
            "ts": now, "kind": kind, "severity": severity, "message": message,
            "vehicle_kind": "truck" if is_truck else "vessel",
            "vehicle_id": t.id, "vtype": t.type,
            "location": {"type": "Point", "coordinates": [round(t.pos[1], 6), round(t.pos[0], 6)]},
        }
        if is_truck:
            e["plate"] = t.plate
        else:
            e["name"] = t.name
        if zone:
            e.update(zone_id=zone["_id"], zone_name=zone["name"], zone_kind=zone["kind"])
        return e


async def _main():
    from .db import get_db
    sim = Simulator(get_db())
    await sim.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_main())
