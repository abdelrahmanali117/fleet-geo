"""
Static geography for the Dubai trucking demo.

Roads are hand-traced approximations of Dubai's main truck corridors
(E11 Sheikh Zayed Rd, E311 Sheikh Mohammed Bin Zayed Rd, E611 Emirates Rd,
E44 Al Khail Rd, E77 Jebel Ali-Lehbab Rd, plus connectors). Points are (lat, lng).
Any point shared by two roads (exact same tuple) becomes a junction in the graph.
"""
import heapq
import math
import random

# ---------------------------------------------------------------- roads
ROADS = {
    "E11 Sheikh Zayed Rd": [
        (24.905, 54.990), (24.955, 55.035), (25.000, 55.090), (25.030, 55.110),
        (25.044, 55.120), (25.071, 55.145), (25.098, 55.175), (25.118, 55.200),
        (25.140, 55.225), (25.160, 55.240), (25.185, 55.258), (25.200, 55.271),
        (25.217, 55.281), (25.228, 55.288),
    ],
    "Airport Rd (Garhoud)": [
        (25.228, 55.288), (25.235, 55.300), (25.240, 55.320), (25.247, 55.335),
        (25.255, 55.345), (25.262, 55.350),
    ],
    "Port Rashid Link": [(25.228, 55.288), (25.245, 55.285), (25.262, 55.280)],
    "E311 Sheikh Mohammed Bin Zayed Rd": [
        (24.900, 55.140), (24.940, 55.155), (24.985, 55.175), (25.020, 55.205),
        (25.050, 55.240), (25.068, 55.275), (25.080, 55.310), (25.100, 55.350),
        (25.125, 55.385), (25.160, 55.410), (25.195, 55.430), (25.230, 55.440),
        (25.265, 55.440), (25.295, 55.435),
    ],
    "E611 Emirates Rd": [
        (24.870, 55.170), (24.920, 55.230), (24.980, 55.290), (25.040, 55.350),
        (25.100, 55.410), (25.160, 55.470), (25.230, 55.500), (25.300, 55.500),
    ],
    "E44 Al Khail Rd": [
        (25.050, 55.240), (25.060, 55.225), (25.090, 55.215), (25.120, 55.232),
        (25.145, 55.250), (25.160, 55.275), (25.175, 55.305), (25.185, 55.330),
        (25.190, 55.350),
    ],
    "E77 Jebel Ali-Lehbab Rd": [
        (25.005, 55.070), (24.985, 55.110), (24.960, 55.140), (24.940, 55.155),
    ],
    "Jebel Ali Port Access": [(25.005, 55.070), (25.000, 55.090)],
    "Al Quoz Industrial Link": [(25.140, 55.225), (25.130, 55.232), (25.120, 55.232)],
    "Ras Al Khor Rd": [(25.190, 55.350), (25.215, 55.352), (25.240, 55.350), (25.262, 55.350)],
    "Al Aweer Link": [(25.190, 55.350), (25.180, 55.380), (25.160, 55.410)],
    "International City Link": [(25.160, 55.410), (25.160, 55.470)],
    "Northern Link": [(25.295, 55.435), (25.300, 55.500)],
    "Al Qusais Link": [(25.262, 55.350), (25.275, 55.375), (25.285, 55.395), (25.265, 55.440)],
    "Dubai South Link": [(24.900, 55.140), (24.885, 55.160), (24.870, 55.170)],
    "Industrial City Link": [(24.900, 55.140), (24.875, 55.110), (24.855, 55.080)],
    "DIP Link": [(24.985, 55.175), (24.990, 55.160)],
}

# ---------------------------------------------------------------- hubs (must be road points)
HUBS = {
    "JAP":    {"name": "Jebel Ali Port",            "pt": (25.005, 55.070), "kind": "port",       "r": 2600},
    "JAFZA":  {"name": "JAFZA Free Zone",           "pt": (24.985, 55.110), "kind": "free_zone",  "r": 2000},
    "DWC":    {"name": "Dubai South Logistics (DWC)", "pt": (24.885, 55.160), "kind": "airport",  "r": 2400},
    "DIP":    {"name": "Dubai Investments Park",    "pt": (24.990, 55.160), "kind": "industrial", "r": 1600},
    "DIC":    {"name": "Dubai Industrial City",     "pt": (24.855, 55.080), "kind": "industrial", "r": 2200},
    "ALQUOZ": {"name": "Al Quoz Industrial",        "pt": (25.130, 55.232), "kind": "industrial", "r": 1400},
    "MOE":    {"name": "Al Barsha Retail Cluster",  "pt": (25.118, 55.200), "kind": "retail",     "r": 900},
    "MARINA": {"name": "Marina Retail Cluster",     "pt": (25.071, 55.145), "kind": "retail",     "r": 900},
    "PRASHID":{"name": "Mina Rashid",               "pt": (25.262, 55.280), "kind": "port",       "r": 1000},
    "DXB":    {"name": "DXB Cargo Village",         "pt": (25.262, 55.350), "kind": "airport",    "r": 1300},
    "AWEER":  {"name": "Al Aweer Produce Market",   "pt": (25.180, 55.380), "kind": "market",     "r": 1100},
    "QUSAIS": {"name": "Al Qusais Industrial",      "pt": (25.285, 55.395), "kind": "industrial", "r": 1300},
    "INTL":   {"name": "International City / Dragon Mart", "pt": (25.160, 55.410), "kind": "retail", "r": 1400},
    "NORTH":  {"name": "Sharjah Border Gateway",    "pt": (25.300, 55.500), "kind": "gateway",    "r": 1200},
}

# Truck-ban zone (Dubai restricts heavy trucks on central roads at peak hours)
DOWNTOWN_BAN = [
    (25.180, 55.250), (25.176, 55.262), (25.222, 55.300),
    (25.234, 55.292), (25.230, 55.278), (25.195, 55.252),
]
BAN_HOURS = [[6.5, 9.0], [13.0, 15.0], [17.5, 20.0]]  # local Dubai time

DEPOTS = [
    ("Service Center - Jebel Ali",      (25.030, 55.110), "service"),
    ("Truck Stop - Lehbab Rd",          (24.960, 55.140), "fuel"),
    ("Fuel Station - Al Barsha South",  (25.068, 55.275), "fuel"),
    ("Service Center - Al Quoz",        (25.140, 55.225), "service"),
    ("Fuel Station - International City", (25.160, 55.410), "fuel"),
    ("Fuel Station - Al Warqa",         (25.230, 55.440), "fuel"),
    ("Fuel Station - Emirates Rd South", (24.920, 55.230), "fuel"),
    ("Service Center - Al Qusais",      (25.265, 55.440), "service"),
    ("Truck Stop - Dubai South",        (24.885, 55.160), "fuel"),
    ("Fuel Station - Emirates Rd North", (25.100, 55.410), "fuel"),
]

# Which hubs each truck type tends to shuttle between (weights)
TYPE_HUBS = {
    "container": {"JAP": 6, "JAFZA": 4, "DIP": 3, "DWC": 3, "ALQUOZ": 2, "QUSAIS": 2, "PRASHID": 1, "NORTH": 2},
    "reefer":    {"JAP": 3, "DWC": 3, "AWEER": 5, "DXB": 3, "MOE": 2, "MARINA": 2, "INTL": 2},
    "tanker":    {"JAFZA": 6, "DIC": 3, "DIP": 2, "QUSAIS": 2, "NORTH": 2, "DWC": 2},
    "flatbed":   {"JAP": 3, "DIC": 4, "DIP": 3, "ALQUOZ": 3, "DWC": 2, "NORTH": 1},
    "box":       {"ALQUOZ": 4, "MOE": 4, "MARINA": 3, "INTL": 3, "QUSAIS": 3, "DXB": 3, "JAFZA": 2},
    "tipper":    {"DIC": 5, "DWC": 3, "DIP": 3, "ALQUOZ": 2, "NORTH": 2},
}

# ---------------------------------------------------------------- geometry helpers
R_EARTH = 6371008.8


def haversine(a, b):
    """meters between (lat,lng) tuples"""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(h))


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def point_in_poly(pt, poly):
    """ray casting; pt and poly vertices are (lat,lng)"""
    y, x = pt
    inside = False
    n = len(poly)
    for i in range(n):
        y1, x1 = poly[i]
        y2, x2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def to_ring(latlngs):
    """(lat,lng) list -> closed GeoJSON ring [[lng,lat],...]"""
    ring = [[round(p[1], 6), round(p[0], 6)] for p in latlngs]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def blob_polygon(center, radius_m, seed, n=11):
    """irregular polygon around a center - looks hand drawn rather than a perfect circle"""
    rnd = random.Random(seed)
    lat, lng = center
    pts = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        r = radius_m * rnd.uniform(0.75, 1.18)
        dlat = (r * math.cos(ang)) / 111320
        dlng = (r * math.sin(ang)) / (111320 * math.cos(math.radians(lat)))
        pts.append((lat + dlat, lng + dlng))
    return pts


# ---------------------------------------------------------------- road graph
class RoadGraph:
    """Generic polyline graph. Used for the road network and (in maritime_data) the sea lanes."""

    def __init__(self, roads=None, restricted_poly=None):
        roads = ROADS if roads is None else roads
        restricted_poly = DOWNTOWN_BAN if restricted_poly is None else restricted_poly
        self.adj = {}  # node -> list[(node, meters, road)]
        for road, pts in roads.items():
            for a, b in zip(pts, pts[1:]):
                d = haversine(a, b)
                self.adj.setdefault(a, []).append((b, d, road))
                self.adj.setdefault(b, []).append((a, d, road))
        self.nodes = list(self.adj)
        self.restricted = {n for n in self.nodes if point_in_poly(n, restricted_poly)}

    def nearest_node(self, pt):
        return min(self.nodes, key=lambda n: haversine(n, pt))

    def shortest_path(self, src, dst, avoid_restricted=True):
        """Dijkstra. Compliant drivers pay a heavy penalty for truck-ban roads."""
        dist = {src: 0.0}
        prev = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == dst:
                break
            if d > dist.get(u, 1e18):
                continue
            for v, w, _ in self.adj[u]:
                if avoid_restricted and (u in self.restricted or v in self.restricted):
                    w *= 4.0
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dst not in dist:
            return [src]
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        return path[::-1]

    def route_from(self, pt, goal, avoid_restricted=True):
        """
        Best node path from an arbitrary point (e.g. mid-road) to goal: picks the entry node that
        minimises (straight-line hop + network distance), so vehicles don't U-turn to a node behind them.
        """
        dist, nxt = {goal: 0.0}, {}
        pq = [(0.0, goal)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, 1e18):
                continue
            for v, w, _ in self.adj[u]:
                if avoid_restricted and (u in self.restricted or v in self.restricted):
                    w *= 4.0
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    nxt[v] = u
                    heapq.heappush(pq, (nd, v))
        start = min(dist, key=lambda n: haversine(pt, n) + dist[n])
        path = [start]
        while path[-1] != goal:
            path.append(nxt[path[-1]])
        return path


def zone_documents():
    docs = []
    for i, (hid, h) in enumerate(HUBS.items()):
        docs.append({
            "_id": f"hub-{hid.lower()}",
            "name": h["name"],
            "kind": h["kind"],
            "hub": hid,
            "geometry": {"type": "Polygon", "coordinates": [to_ring(blob_polygon(h["pt"], h["r"], seed=i + 7))]},
            "rules": {"alert_on": ["enter", "exit"]},
            "builtin": True,
        })
    docs.append({
        "_id": "restricted-downtown",
        "name": "Downtown Truck-Ban Zone",
        "kind": "restricted",
        "geometry": {"type": "Polygon", "coordinates": [to_ring(DOWNTOWN_BAN)]},
        "rules": {"ban_hours": BAN_HOURS, "alert_on": ["enter"], "applies_to": ["truck"], "exempt_types": ["box"]},
        "builtin": True,
    })
    return docs


def route_documents():
    return [
        {
            "_id": name,
            "name": name,
            "kind": "road",
            "geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in pts]},
            "length_km": round(sum(haversine(a, b) for a, b in zip(pts, pts[1:])) / 1000, 1),
        }
        for name, pts in ROADS.items()
    ]


def depot_documents():
    return [
        {"_id": f"depot-{i+1:02d}", "name": n, "kind": k,
         "location": {"type": "Point", "coordinates": [p[1], p[0]]}}
        for i, (n, p, k) in enumerate(DEPOTS)
    ]


# ---------------------------------------------------------------- polyline helpers
def cumulative(path):
    out = [0.0]
    for a, b in zip(path, path[1:]):
        out.append(out[-1] + haversine(a, b))
    return out


def interpolate(path, cum, s):
    """position + heading at distance s (meters) along path"""
    if s <= 0:
        return path[0], bearing(path[0], path[1]) if len(path) > 1 else 0.0
    if s >= cum[-1]:
        return path[-1], bearing(path[-2], path[-1]) if len(path) > 1 else 0.0
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= s:
            lo = mid
        else:
            hi = mid
    a, b = path[lo], path[hi]
    seg = cum[hi] - cum[lo] or 1.0
    t = (s - cum[lo]) / seg
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), bearing(a, b)


def densify(path, step_m=400):
    """add intermediate points so trucks follow the road smoothly"""
    out = [path[0]]
    for a, b in zip(path, path[1:]):
        n = max(1, int(haversine(a, b) // step_m))
        for i in range(1, n + 1):
            t = i / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out
