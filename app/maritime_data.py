"""
Maritime geography + vessel generator.

Vessels live in the SAME `vehicles` collection as trucks. They share a small core
(kind, type, status, location, heading, last_update) and carry their own fields:
AIS identity, dimensions, voyage, TEU / DWT / passengers / bollard pull ...
Points are (lat, lng). Coordinates are hand-placed approximations.
"""
import math
import random
from datetime import datetime, timedelta

from .geo_data import RoadGraph, blob_polygon, cumulative, haversine, interpolate, to_ring

# ---------------------------------------------------------------- sea nodes
N = {
    "JA_B1": (25.010, 55.052), "JA_B2": (25.017, 55.043), "JA_B3": (25.024, 55.034),
    "JA_BASIN": (25.030, 55.028), "JA_CHAN": (25.045, 55.012), "JA_OUTER": (25.080, 54.985),
    "JA_ANCH": (25.120, 54.950), "OFF_JA": (25.160, 54.900),
    "WEST_IN": (25.040, 54.850), "WEST_EXIT": (25.000, 54.550),
    "OFF_DXB": (25.300, 55.100), "PR_OUTER": (25.320, 55.220), "PR_CHAN": (25.288, 55.258),
    "PR_BERTH": (25.268, 55.274),
    "CREEK_WHARF": (25.2668, 55.2990), "CREEK_MOUTH": (25.2740, 55.2895), "CREEK_OUT": (25.300, 55.270),
    "OFF_N": (25.450, 55.300), "HORMUZ1": (25.700, 55.550), "HORMUZ_EXIT": (26.000, 55.900),
}

LANES = {
    "Jebel Ali Berth T1": ["JA_B1", "JA_BASIN"],
    "Jebel Ali Berth T2": ["JA_B2", "JA_BASIN"],
    "Jebel Ali Liquid Terminal T3": ["JA_B3", "JA_BASIN"],
    "Jebel Ali Approach Channel": ["JA_BASIN", "JA_CHAN", "JA_OUTER"],
    "Jebel Ali Anchorage Lane": ["JA_OUTER", "JA_ANCH", "OFF_JA"],
    "Western Offshore Lane": ["OFF_JA", "WEST_EXIT"],
    "Inshore Shortcut (crosses protected zone)": ["JA_OUTER", "WEST_IN", "WEST_EXIT"],
    "Dubai Coastal Lane": ["OFF_JA", "OFF_DXB", "OFF_N"],
    "Mina Rashid Approach": ["OFF_DXB", "PR_OUTER", "PR_CHAN", "PR_BERTH"],
    "Dubai Creek Approach": ["CREEK_WHARF", "CREEK_MOUTH", "CREEK_OUT", "OFF_N"],
    "Creek - Mina Rashid Link": ["CREEK_OUT", "PR_OUTER"],
    "Hormuz Lane": ["OFF_N", "HORMUZ1", "HORMUZ_EXIT"],
}
SEA_ROADS = {name: [N[k] for k in keys] for name, keys in LANES.items()}

PORTS = {
    "Jebel Ali": {"T1": "JA_B1", "T2": "JA_B2", "T3": "JA_B3", "Tug base": "JA_BASIN"},
    "Mina Rashid": {"Cruise terminal": "PR_BERTH"},
    "Deira Creek Wharf": {"Dhow wharf": "CREEK_WHARF"},
}
ANCHORAGE = N["JA_ANCH"]
EXITS = {
    "WEST_EXIT": ["Ruwais", "Khalifa Port", "Dammam", "Doha", "Hamad Port"],
    "HORMUZ_EXIT": ["Mundra", "Nhava Sheva", "Karachi", "Salalah", "Colombo", "Singapore", "Sohar", "Shanghai"],
}


# ---------------------------------------------------------------- zones
def buffer_line(points, half_m):
    """polyline (lat,lng) -> polygon buffered by half_m (gentle bends only)"""
    left, right = [], []
    for i, (la, lo) in enumerate(points):
        a = points[max(0, i - 1)]
        b = points[min(len(points) - 1, i + 1)]
        mx = 111320 * math.cos(math.radians(la))
        dx, dy = (b[1] - a[1]) * mx, (b[0] - a[0]) * 110540
        L = math.hypot(dx, dy) or 1
        ox, oy = -dy / L * half_m, dx / L * half_m
        left.append((la + oy / 110540, lo + ox / mx))
        right.append((la - oy / 110540, lo - ox / mx))
    return left + right[::-1]


HARBOUR_JA = buffer_line([N["JA_B1"], N["JA_B2"], N["JA_B3"], N["JA_BASIN"], N["JA_CHAN"]], 900)
HARBOUR_CREEK = buffer_line([N["CREEK_WHARF"], N["CREEK_MOUTH"]], 450)
PROTECTED = blob_polygon(N["WEST_IN"], 6000, seed=99, n=12)
ANCHORAGE_POLY = blob_polygon(ANCHORAGE, 4500, seed=31, n=12)


def zone_documents():
    return [
        {"_id": "sea-ja-anchorage", "name": "Jebel Ali Anchorage", "kind": "anchorage",
         "geometry": {"type": "Polygon", "coordinates": [to_ring(ANCHORAGE_POLY)]},
         "rules": {"alert_on": ["enter", "exit"], "applies_to": ["vessel"]}, "builtin": True},
        {"_id": "sea-ja-harbour", "name": "Jebel Ali Harbour (6 kn limit)", "kind": "harbour",
         "geometry": {"type": "Polygon", "coordinates": [to_ring(HARBOUR_JA)]},
         "rules": {"max_speed_kn": 6, "applies_to": ["vessel"]}, "builtin": True},
        {"_id": "sea-creek", "name": "Dubai Creek (5 kn limit)", "kind": "harbour",
         "geometry": {"type": "Polygon", "coordinates": [to_ring(HARBOUR_CREEK)]},
         "rules": {"max_speed_kn": 5, "applies_to": ["vessel"]}, "builtin": True},
        {"_id": "sea-protected", "name": "Marine Protected Zone", "kind": "protected",
         "geometry": {"type": "Polygon", "coordinates": [to_ring(PROTECTED)]},
         "rules": {"alert_on": ["enter"], "applies_to": ["vessel"]}, "builtin": True},
    ]


def lane_documents():
    return [{
        "_id": f"lane:{name}", "name": name, "kind": "sea_lane",
        "geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in pts]},
        "length_nm": round(sum(haversine(a, b) for a, b in zip(pts, pts[1:])) / 1852, 1),
    } for name, pts in SEA_ROADS.items()]


def sea_graph():
    return RoadGraph(SEA_ROADS, PROTECTED)


def in_harbour(pt):
    from .geo_data import point_in_poly
    return point_in_poly(pt, HARBOUR_JA) or point_in_poly(pt, HARBOUR_CREEK)


# ---------------------------------------------------------------- vessel types
# count, speed range kn, home port, berths, uses anchorage, exits, size(px for UI), class
VESSEL_TYPES = {
    "container_ship": dict(n=20, kn=(12, 18), port="Jebel Ali", berths=["T1", "T2"], anchor=True,
                           exits=["WEST_EXIT", "HORMUZ_EXIT"]),
    "oil_tanker":     dict(n=12, kn=(11, 15), port="Jebel Ali", berths=["T3"], anchor=True,
                           exits=["WEST_EXIT", "HORMUZ_EXIT"]),
    "bulk_carrier":   dict(n=8, kn=(10, 14), port="Jebel Ali", berths=["T2"], anchor=True,
                           exits=["WEST_EXIT", "HORMUZ_EXIT"]),
    "ro_ro":          dict(n=5, kn=(14, 19), port="Jebel Ali", berths=["T1"], anchor=True,
                           exits=["HORMUZ_EXIT"]),
    "passenger":      dict(n=3, kn=(14, 20), port="Mina Rashid", berths=["Cruise terminal"], anchor=False,
                           exits=["HORMUZ_EXIT", "WEST_EXIT"]),
    "dhow":           dict(n=18, kn=(6, 9), port="Deira Creek Wharf", berths=["Dhow wharf"], anchor=False,
                           exits=["HORMUZ_EXIT"]),
    "tug":            dict(n=9, kn=(8, 12), port="Jebel Ali", berths=["Tug base"], anchor=False, exits=[]),
}
VESSEL_STATUSES = ["underway", "anchored", "moored", "dark"]

FLAGS = {"UAE": 470, "Panama": 352, "Liberia": 636, "Marshall Islands": 538, "Singapore": 563,
         "Malta": 248, "Hong Kong": 477, "India": 419, "Oman": 461}
PREFIX = ["Gulf", "Desert", "Arabian", "Pearl", "Falcon", "Oasis", "Coral", "Dune", "Sapphire", "Horizon",
          "Najm", "Saqr", "Marjan", "Sahel", "Bahr", "Silver", "Amber", "Crescent"]
SUFFIX = ["Pioneer", "Star", "Voyager", "Spirit", "Trader", "Express", "Navigator", "Glory", "Breeze",
          "Fortune", "Crest", "Venture", "Harmony", "Endeavour"]
DHOW_NAMES = ["Al Noor", "Al Salam", "Al Bahar", "Al Rahma", "Al Khair", "Al Fajr", "Al Amal", "Al Nasim",
              "Al Yaqoot", "Al Murjan", "Al Hilal", "Al Waha"]
OPERATORS = {
    "container_ship": ["Desert Star Lines", "Gulf Horizon Shipping", "Crescent Container Line"],
    "oil_tanker": ["Pearl Tankers", "Amber Energy Shipping"],
    "bulk_carrier": ["Coral Bulk", "Dune Dry Cargo"],
    "ro_ro": ["Sahel Car Carriers"],
    "passenger": ["Arabian Cruises"],
    "dhow": ["Creek Traders Co.", "Musandam Dhow Freight", "Al Ras Trading"],
    "tug": ["JA Marine Services"],
}


def nav_status(status):
    return {"underway": "Under way using engine", "anchored": "At anchor", "moored": "Moored",
            "dark": "No signal"}[status]


def berth_spot(rnd, node_key, spread_m=280):
    lat, lng = N[node_key]
    return (lat + rnd.gauss(0, spread_m) / 110540, lng + rnd.gauss(0, spread_m) / (111320 * math.cos(math.radians(lat))))


def anchor_spot(rnd):
    lat, lng = ANCHORAGE
    r, a = 3200 * math.sqrt(rnd.random()), rnd.uniform(0, 2 * math.pi)
    return (lat + r * math.cos(a) / 110540, lng + r * math.sin(a) / (111320 * math.cos(math.radians(lat))))


def make_vessel(i, vtype, rnd, graph, now):
    prof = VESSEL_TYPES[vtype]
    flag = rnd.choice(["UAE", "Oman", "India"]) if vtype == "dhow" else ("UAE" if vtype == "tug" else rnd.choice(list(FLAGS)))
    mmsi = FLAGS[flag] * 1_000_000 + rnd.randint(100000, 999999)
    if vtype == "dhow":
        name = f"{rnd.choice(DHOW_NAMES)} {rnd.randint(2, 99)}"
    elif vtype == "tug":
        name = f"JA Tug {i:02d}"
    else:
        name = f"{rnd.choice(PREFIX)} {rnd.choice(SUFFIX)}"
    port, berth = prof["port"], rnd.choice(prof["berths"])
    exit_key = rnd.choice(prof["exits"]) if prof["exits"] else None
    foreign = "Khasab" if vtype == "dhow" else (rnd.choice(EXITS[exit_key]) if exit_key else None)

    # --- type-specific shape (this is the point: different fields per type)
    if vtype == "container_ship":
        length = rnd.randint(260, 400)
        dims = {"length_m": length, "beam_m": round(length * 0.15), "draught_m": round(rnd.uniform(11, 16), 1)}
        teu = rnd.randrange(4000, 24000, 500)
        cargo = {"type": "Containers", "teu": teu, "reefer_plugs": rnd.randrange(400, 1800, 50),
                 "hazmat": rnd.random() < 0.3}
        if cargo["hazmat"]:
            cargo["imdg_classes"] = rnd.sample([3, 8, 9], k=rnd.randint(1, 2))
        crew = rnd.randint(20, 28)
    elif vtype == "oil_tanker":
        length = rnd.randint(180, 330)
        dims = {"length_m": length, "beam_m": round(length * 0.18), "draught_m": round(rnd.uniform(10, 20), 1)}
        product = rnd.choice(["Crude oil", "Gasoil", "Jet A-1", "Naphtha"])
        cargo = {"type": product, "dwt": rnd.randrange(40000, 300000, 5000), "hazmat": True,
                 "imdg_classes": [3], "segregated_ballast": True}
        crew = rnd.randint(22, 30)
    elif vtype == "bulk_carrier":
        length = rnd.randint(170, 290)
        dims = {"length_m": length, "beam_m": round(length * 0.16), "draught_m": round(rnd.uniform(9, 17), 1)}
        cargo = {"type": rnd.choice(["Grain", "Cement clinker", "Aggregates", "Iron ore"]),
                 "dwt": rnd.randrange(30000, 180000, 5000), "holds": rnd.randint(5, 9), "hazmat": False}
        crew = rnd.randint(19, 25)
    elif vtype == "ro_ro":
        length = rnd.randint(180, 230)
        dims = {"length_m": length, "beam_m": 32, "draught_m": round(rnd.uniform(8, 10), 1)}
        cargo = {"type": "Vehicles", "ceu": rnd.randrange(4000, 8000, 100), "decks": rnd.randint(10, 13),
                 "hazmat": False}
        crew = rnd.randint(20, 26)
    elif vtype == "passenger":
        length = rnd.randint(290, 340)
        dims = {"length_m": length, "beam_m": 40, "draught_m": round(rnd.uniform(8, 9), 1)}
        cargo = None
        crew = rnd.randint(1100, 1600)
    elif vtype == "dhow":
        length = rnd.randint(25, 60)
        dims = {"length_m": length, "beam_m": round(length * 0.27, 1), "draught_m": round(rnd.uniform(2.5, 4.5), 1)}
        cargo = {"type": rnd.choice(["General cargo", "Electronics", "Foodstuffs", "Building materials", "Tyres"]),
                 "weight_t": rnd.randrange(80, 450, 10), "hazmat": False, "hull": "wooden"}
        crew = rnd.randint(6, 14)
    else:  # tug
        length = rnd.randint(28, 36)
        dims = {"length_m": length, "beam_m": 12, "draught_m": round(rnd.uniform(4.5, 6), 1)}
        cargo = None
        crew = rnd.randint(5, 8)

    # --- initial phase + position
    r = rnd.random()
    if vtype == "tug":
        phase = "moored" if r < 0.6 else "assist"
    elif vtype in ("dhow", "passenger"):
        phase = "moored" if r < 0.45 else rnd.choice(["inbound", "outbound"])
    else:
        phase = "anchored" if r < 0.3 else "moored" if r < 0.55 else rnd.choice(["inbound", "approach", "outbound"])

    berth_node = PORTS[port][berth]
    exit_pt = N[exit_key] if exit_key else None
    heading, speed = rnd.uniform(0, 360), 0.0
    anchored_since = None
    if phase == "moored":
        pt, status = berth_spot(rnd, berth_node, 120 if vtype in ("dhow", "tug") else 280), "moored"
    elif phase == "anchored":
        pt, status = anchor_spot(rnd), "anchored"
        anchored_since = now - timedelta(minutes=rnd.randint(40, 36 * 60))
    else:
        if phase == "assist":
            a, b = N["JA_BASIN"], rnd.choice([N["JA_OUTER"], N["JA_ANCH"]])
        elif phase == "inbound":
            a, b = exit_pt, (ANCHORAGE if prof["anchor"] else N[berth_node])
        elif phase == "approach":
            a, b = ANCHORAGE, N[berth_node]
        else:  # outbound
            a, b = N[berth_node], exit_pt
        nodes = graph.shortest_path(graph.nearest_node(a), graph.nearest_node(b))
        cum = cumulative(nodes)
        pt, heading = interpolate(nodes, cum, rnd.uniform(0.1, 0.9) * cum[-1])
        status, speed = "underway", round(rnd.uniform(*prof["kn"]), 1)

    inbound_like = phase in ("inbound", "anchored", "approach", "moored")
    voyage = {
        "phase": phase,
        "port": port,
        "berth": berth,
        "last_port": foreign if inbound_like else port,
        "next_port": port if inbound_like else foreign,
    }
    if vtype == "tug":
        voyage.update(last_port="Jebel Ali", next_port="Jebel Ali")
    if anchored_since:
        voyage["anchored_since"] = anchored_since
    if exit_key:
        voyage["exit"] = exit_key

    ais = {"mmsi": mmsi, "callsign": f"{rnd.choice('ACDHJLV')}{rnd.choice('ABCDEFGHJ')}{rnd.randint(1000, 9999)}",
           "class": "B" if vtype == "dhow" else "A", "nav_status": nav_status(status), "last_msg": now}
    if vtype != "dhow":  # traditional dhows often have no IMO number: the field is simply absent
        ais["imo"] = rnd.randint(9100000, 9990000)

    doc = {
        "_id": f"VSL-{i:04d}",
        "kind": "vessel",
        "type": vtype,
        "name": name,
        "status": status,
        "location": {"type": "Point", "coordinates": [round(pt[1], 6), round(pt[0], 6)]},
        "heading": round(heading, 1),
        "speed_kn": speed,
        "flag": flag,
        "operator": rnd.choice(OPERATORS[vtype]),
        "ais": ais,
        "dimensions": dims,
        "voyage": voyage,
        "crew": crew,
        "compliant": rnd.random() > 0.18,
        "last_update": now,
    }
    if cargo:
        doc["cargo"] = cargo
    if vtype == "passenger":
        doc["passengers"] = rnd.randrange(2400, 5400, 50)
        doc["amenities"] = rnd.sample(["pools", "theatre", "spa", "kids club", "casino-free decks", "planetarium"], k=3)
    if vtype == "tug":
        doc["bollard_pull_t"] = rnd.choice([50, 60, 70, 80, 90])
        doc["fifi_class"] = rnd.choice(["FiFi 1", "FiFi 2", None]) or "none"
    return doc


def make_fleet(rnd, now, scale=1.0):
    graph = sea_graph()
    docs, i = [], 1
    for vtype, prof in VESSEL_TYPES.items():
        for _ in range(max(1, round(prof["n"] * scale))):
            docs.append(make_vessel(i, vtype, rnd, graph, now))
            i += 1
    return docs


def superyacht(now):
    """A brand-new vessel type with brand-new fields - inserted live, no migration."""
    return {
        "_id": "VSL-YACHT-01",
        "kind": "vessel",
        "type": "superyacht",
        "name": "Desert Pearl",
        "status": "anchored",
        "location": {"type": "Point", "coordinates": [55.092, 25.118]},
        "heading": 300.0,
        "speed_kn": 0.0,
        "flag": "Cayman Islands",
        "ais": {"mmsi": 319123456, "imo": 9876543, "class": "A", "nav_status": "At anchor", "last_msg": now},
        "dimensions": {"length_m": 95, "beam_m": 15, "draught_m": 4.2},
        "guests": {"max": 12, "cabins": 6},
        "helipad": True,
        "tenders": [{"name": "Pearl I", "type": "limousine tender", "length_m": 10},
                    {"name": "Pearl II", "type": "jet ski", "length_m": 3}],
        "voyage": {"phase": "anchored", "port": "Dubai Harbour", "next_port": "Dubai Harbour",
                   "anchored_since": now},
        "last_update": now,
    }


def invalid_vessel(now):
    """Missing ais.mmsi -> rejected by the $jsonSchema validator."""
    return {
        "_id": "VSL-INVALID-01",
        "kind": "vessel",
        "type": "tug",
        "name": "Unregistered Boat",
        "status": "underway",
        "location": {"type": "Point", "coordinates": [55.06, 25.05]},
        "speed_kn": 9.5,
        "last_update": now,
    }
