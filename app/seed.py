"""
Seed Atlas with a synthetic Dubai trucking fleet.

    python -m app.seed            # seed only if empty
    python -m app.seed --reset    # drop and reseed
"""
import argparse
import logging
import random
from datetime import datetime, timezone

from . import config
from . import maritime_data as maritime
from .db import ensure_schema, sync_db
from .geo_data import (DEPOTS, HUBS, TYPE_HUBS, RoadGraph, cumulative, depot_documents,
                       interpolate, route_documents, zone_documents)

log = logging.getLogger("fleet.seed")

TYPES = {  # type: (share, capacity range t, makes, cargo options [(label, hazmat)])
    "container": (0.30, (26, 32), ["Volvo FH 460", "Mercedes Actros 1845", "MAN TGX 18.480", "Scania R450"],
                  [("40ft container", False), ("20ft container x2", False), ("Empty container return", False)]),
    "box":       (0.20, (6, 12), ["Isuzu FVR", "Hino 500", "Mitsubishi Fuso Fighter", "Isuzu NPR"],
                  [("E-commerce parcels", False), ("FMCG pallets", False), ("Electronics", False), ("Furniture", False)]),
    "reefer":    (0.16, (10, 22), ["Volvo FH 420 + Thermo King", "Mercedes Actros + Carrier", "Hino 700 Reefer"],
                  [("Frozen food", False), ("Fresh produce", False), ("Pharmaceuticals", False), ("Dairy", False)]),
    "flatbed":   (0.14, (20, 30), ["Mercedes Actros 3344", "MAN TGS 33.400", "Volvo FMX"],
                  [("Steel coils", False), ("Rebar", False), ("Precast concrete", False), ("Machinery", False)]),
    "tanker":    (0.12, (20, 36), ["Scania G410 Tanker", "Mercedes Actros Tanker", "Volvo FM Tanker"],
                  [("Diesel", True), ("Jet A-1", True), ("Industrial chemicals", True), ("Potable water", False)]),
    "tipper":    (0.08, (20, 30), ["Mercedes Arocs 4142", "Volvo FMX 460", "Sinotruk Howo"],
                  [("Sand", False), ("Aggregate", False), ("Excavated soil", False)]),
}

COMPANIES = ["Gulf Horizon Logistics", "Creek Line Haulage", "Falcon Cargo", "Oasis Cold Chain",
             "Dune Freight", "Palm Route Transport", "Desert Star Movers", "Seabridge Haulers"]
FIRST = ["Ahmed", "Mohammed", "Omar", "Yousef", "Khalid", "Rashid", "Imran", "Faisal", "Sanjay", "Rajesh",
         "Tariq", "Hassan", "Bilal", "Arjun", "Joseph", "Anil", "Samir", "Nabil", "Kareem", "Vikram",
         "Hamza", "Zubair", "Suresh", "Waleed", "Ali", "Mustafa", "Ravi", "Adnan", "Ibrahim", "Deepak"]
LAST = ["Al Mansoori", "Khan", "Hussain", "Nair", "Rahman", "Pillai", "Al Suwaidi", "Siddiqui", "Menon",
        "Qureshi", "Al Hashmi", "Sharma", "Iqbal", "Fernandes", "Butt", "Kumar", "Al Nuaimi", "Chaudhry"]

STATUS_MIX = [("moving", 0.64), ("loading", 0.13), ("idle", 0.11), ("maintenance", 0.07), ("offline", 0.05)]


def pick(rnd, weighted):
    r, acc = rnd.random(), 0.0
    for k, w in weighted:
        acc += w
        if r <= acc:
            return k
    return weighted[-1][0]


def pick_hub(rnd, vtype, exclude=None):
    hubs = [(h, w) for h, w in TYPE_HUBS[vtype].items() if h != exclude]
    total = sum(w for _, w in hubs)
    return pick(rnd, [(h, w / total) for h, w in hubs])


def make_vehicle(i, rnd, graph, now):
    vtype = pick(rnd, [(t, v[0]) for t, v in TYPES.items()])
    share, cap, makes, cargos = TYPES[vtype]
    status = pick(rnd, STATUS_MIX)
    origin = pick_hub(rnd, vtype)
    dest = pick_hub(rnd, vtype, exclude=origin)
    compliant = rnd.random() > 0.15

    if status == "loading":
        pt, heading = HUBS[origin]["pt"], rnd.uniform(0, 360)
        # scatter inside the yard
        pt = (pt[0] + rnd.gauss(0, 0.0025), pt[1] + rnd.gauss(0, 0.0025))
    elif status == "maintenance":
        d = rnd.choice([d for d in DEPOTS if d[2] == "service"])
        pt, heading = (d[1][0] + rnd.gauss(0, 0.0008), d[1][1] + rnd.gauss(0, 0.0008)), 0.0
    else:
        path = graph.shortest_path(HUBS[origin]["pt"], HUBS[dest]["pt"], avoid_restricted=compliant)
        cum = cumulative(path)
        pt, heading = interpolate(path, cum, rnd.uniform(0.05, 0.95) * cum[-1])

    cargo, hazmat = rnd.choice(cargos)
    capacity = rnd.randint(*cap)
    fuel = round(min(100, max(4, rnd.gauss(58, 24))), 1)
    speed = round(rnd.uniform(55, 95), 1) if status == "moving" else 0.0
    doc = {
        "_id": f"TRK-{i:04d}",
        "kind": "truck",
        "plate": f"Dubai {rnd.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')} {rnd.randint(10000, 99999)}",
        "type": vtype,
        "make": rnd.choice(makes),
        "company": rnd.choice(COMPANIES),
        "driver": {"name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", "compliant": compliant,
                   "rating": round(rnd.uniform(3.6, 5.0), 1)},
        "capacity_t": capacity,
        "cargo": {"type": cargo, "hazmat": hazmat,
                  "weight_t": 0 if cargo.startswith("Empty") else round(capacity * rnd.uniform(0.35, 0.98), 1)},
        "status": status,
        "location": {"type": "Point", "coordinates": [round(pt[1], 6), round(pt[0], 6)]},
        "heading": round(heading, 1),
        "speed_kmh": speed,
        "fuel_pct": fuel,
        "odometer_km": rnd.randint(20000, 780000),
        "trip": {"origin": origin, "origin_name": HUBS[origin]["name"],
                 "dest": dest, "dest_name": HUBS[dest]["name"]},
        "last_update": now,
    }
    if vtype == "reefer":
        doc["reefer_temp_c"] = round(rnd.uniform(-20, -16) if cargo in ("Frozen food",) else rnd.uniform(2, 6), 1)
        doc["reefer_setpoint_c"] = -18 if cargo == "Frozen food" else 4
    return doc


def refresh_reference_data(db):
    """built-in zones, roads + sea lanes, depots (custom user geofences are kept)"""
    db.zones.delete_many({"builtin": True})
    db.zones.insert_many(zone_documents() + maritime.zone_documents())
    db.routes.delete_many({})
    db.routes.insert_many(route_documents() + maritime.lane_documents())
    db.depots.delete_many({})
    db.depots.insert_many(depot_documents())


def seed(reset=False, fleet_size=None):
    db = sync_db()
    if reset:
        for c in ("vehicles", "zones", "routes", "depots", "telemetry", "events"):
            db.drop_collection(c)
        log.info("dropped existing collections")
    ensure_schema(db)
    now = datetime.now(timezone.utc)

    if db.vehicles.estimated_document_count() and not reset:
        # Schema evolution on a live collection: no downtime, no migration window.
        r = db.vehicles.update_many({"kind": {"$exists": False}}, {"$set": {"kind": "truck"}})
        if r.modified_count:
            log.info("backfilled kind='truck' on %d existing documents", r.modified_count)
        if config.VESSELS and not db.vehicles.count_documents({"kind": "vessel"}, limit=1):
            vessels = maritime.make_fleet(random.Random(7), now, config.VESSELS / 75)
            db.vehicles.insert_many(vessels)
            refresh_reference_data(db)
            log.info("added %d vessels to the existing vehicles collection", len(vessels))
        else:
            log.info("vehicles already present (%d) - skipping seed", db.vehicles.estimated_document_count())
        return False

    n = fleet_size or config.FLEET_SIZE
    rnd = random.Random(42)
    graph = RoadGraph()
    trucks = [make_vehicle(i + 1, rnd, graph, now) for i in range(n)]
    vessels = maritime.make_fleet(random.Random(7), now, config.VESSELS / 75) if config.VESSELS else []
    db.vehicles.insert_many(trucks + vessels)  # one collection, two shapes, one insert
    refresh_reference_data(db)
    log.info("seeded %d trucks + %d vessels, %d zones, %d routes/lanes, %d depots",
             n, len(vessels), db.zones.count_documents({}), db.routes.count_documents({}),
             db.depots.count_documents({}))
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--fleet", type=int, default=None)
    a = ap.parse_args()
    seed(reset=a.reset, fleet_size=a.fleet)
