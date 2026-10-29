"""
Pure query builders. Each returns plain dicts/lists so the UI can show the exact MQL.
"""
import json
import math
from datetime import datetime, timedelta, timezone

EARTH_RADIUS_M = 6378100.0

# fields from BOTH shapes - each document simply returns the ones it has
VEHICLE_FIELDS = {
    "_id": 1, "kind": 1, "type": 1, "status": 1, "location": 1, "heading": 1,
    # truck fields
    "plate": 1, "speed_kmh": 1, "fuel_pct": 1, "company": 1, "driver.name": 1, "trip.dest_name": 1, "reefer_temp_c": 1,
    # vessel fields
    "name": 1, "speed_kn": 1, "ais.mmsi": 1, "voyage.next_port": 1, "voyage.phase": 1, "operator": 1,
    # shared sub-document, different contents per kind
    "cargo": 1,
}


# ------------------------------------------------------------------ shapes
def close_ring(ring):
    ring = [[float(x), float(y)] for x, y in ring]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def geo_within(shape):
    """shape: {"type":"Polygon","coordinates":[...]} or {"type":"Circle","center":[lng,lat],"radius":m}"""
    if shape["type"] == "Circle":
        return {"$geoWithin": {"$centerSphere": [shape["center"], shape["radius"] / EARTH_RADIUS_M]}}
    if shape["type"] == "Polygon":
        return {"$geoWithin": {"$geometry": {"type": "Polygon",
                                             "coordinates": [close_ring(r) for r in shape["coordinates"]]}}}
    raise ValueError(f"unsupported shape {shape['type']}")


def corridor_or(line_coords, width_m):
    """
    Corridor search around a drawn route: a $or of $geoWithin clauses -
    one buffered quad per segment + a $centerSphere at each vertex for the joints.
    Every clause can use the 2dsphere index.
    """
    clauses = []
    half = width_m / 2
    for (x1, y1), (x2, y2) in zip(line_coords, line_coords[1:]):
        lat0 = math.radians((y1 + y2) / 2)
        mx = 111320 * math.cos(lat0)          # meters per degree lng
        my = 110540                           # meters per degree lat
        dx, dy = (x2 - x1) * mx, (y2 - y1) * my
        L = math.hypot(dx, dy) or 1.0
        ox, oy = -dy / L * half / mx, dx / L * half / my
        quad = [[x1 + ox, y1 + oy], [x2 + ox, y2 + oy], [x2 - ox, y2 - oy], [x1 - ox, y1 - oy], [x1 + ox, y1 + oy]]
        quad = [[round(a, 6), round(b, 6)] for a, b in quad]
        clauses.append({"location": {"$geoWithin": {"$geometry": {"type": "Polygon", "coordinates": [quad]}}}})
    for x, y in line_coords:
        clauses.append({"location": {"$geoWithin": {"$centerSphere": [[x, y], half / EARTH_RADIUS_M]}}})
    return {"$or": clauses}


def attribute_filters(f):
    q = {}
    if not f:
        return q
    if f.get("kinds"):
        q["kind"] = {"$in": f["kinds"]}
    if f.get("types"):
        q["type"] = {"$in": f["types"]}
    if f.get("statuses"):
        q["status"] = {"$in": f["statuses"]}
    if f.get("max_fuel") is not None and float(f["max_fuel"]) < 100:
        q["fuel_pct"] = {"$lte": float(f["max_fuel"])}
    if f.get("hazmat"):
        q["cargo.hazmat"] = True
    if f.get("min_speed"):
        q["speed_kmh"] = {"$gte": float(f["min_speed"])}
    if f.get("company"):
        q["company"] = f["company"]
    return q


def spatial_match(shape, filters):
    """the $match stage for a drawn shape (polygon, rectangle, circle, or line corridor)"""
    q = attribute_filters(filters)
    if shape["type"] == "LineString":
        q.update(corridor_or(shape["coordinates"], shape.get("width", 600)))
    else:
        q["location"] = geo_within(shape)
    return q


# ------------------------------------------------------------------ pipelines
def within_pipeline(shape, filters, limit=3000):
    match = spatial_match(shape, filters)
    return [
        {"$match": match},
        {"$facet": {
            "vehicles": [{"$project": VEHICLE_FIELDS}, {"$limit": limit}],
            "by_status": [{"$group": {"_id": "$status", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}],
            "by_type": [{"$group": {"_id": "$type", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}],
            "by_kind": [{"$group": {"_id": "$kind", "count": {"$sum": 1}}}],
            "summary": [{"$group": {
                "_id": None,
                "count": {"$sum": 1},
                "trucks": {"$sum": {"$cond": [{"$eq": ["$kind", "truck"]}, 1, 0]}},
                "vessels": {"$sum": {"$cond": [{"$eq": ["$kind", "vessel"]}, 1, 0]}},
                "avg_speed_moving": {"$avg": {"$cond": [{"$eq": ["$status", "moving"]}, "$speed_kmh", None]}},
                "avg_kn_underway": {"$avg": {"$cond": [{"$eq": ["$status", "underway"]}, "$speed_kn", None]}},
                "avg_fuel": {"$avg": "$fuel_pct"},
                # road tonnage from trucks, TEU from container ships: same $group, different fields
                "road_tonnes": {"$sum": {"$cond": [{"$eq": ["$kind", "truck"]}, "$cargo.weight_t", 0]}},
                "teu": {"$sum": "$cargo.teu"},
                "hazmat": {"$sum": {"$cond": [{"$eq": ["$cargo.hazmat", True]}, 1, 0]}},
                # guard on kind: vessels have no fuel_pct, and null < 15 would be true in aggregation
                "low_fuel": {"$sum": {"$cond": [{"$and": [{"$eq": ["$kind", "truck"]},
                                                          {"$lt": ["$fuel_pct", 15]}]}, 1, 0]}},
            }}],
        }},
    ]


def near_pipeline(point, filters, limit=5, max_distance_m=None, target="vehicles"):
    geo = {
        "near": {"type": "Point", "coordinates": point},
        "distanceField": "distance_m",
        "spherical": True,
        "key": "location",
    }
    if max_distance_m:
        geo["maxDistance"] = max_distance_m
    if target == "vehicles":
        q = attribute_filters(filters)
        if q:
            geo["query"] = q
        return [
            {"$geoNear": geo},
            {"$limit": limit},
            {"$project": {**VEHICLE_FIELDS, "distance_m": {"$round": ["$distance_m", 0]},
                          # rough city ETA at 45 km/h
                          "eta_min": {"$round": [{"$divide": ["$distance_m", 750]}, 1]}}},
        ]
    return [{"$geoNear": geo}, {"$limit": limit},
            {"$addFields": {"distance_m": {"$round": ["$distance_m", 0]}}}]


def intersects_query(shape):
    if shape["type"] == "Circle":
        # $geoIntersects needs GeoJSON: approximate circle as 32-gon
        lng, lat = shape["center"]
        r = shape["radius"]
        ring = []
        for i in range(33):
            a = 2 * math.pi * i / 32
            ring.append([round(lng + r * math.sin(a) / (111320 * math.cos(math.radians(lat))), 6),
                         round(lat + r * math.cos(a) / 110540, 6)])
        geom = {"type": "Polygon", "coordinates": [ring]}
    elif shape["type"] == "Polygon":
        geom = {"type": "Polygon", "coordinates": [close_ring(r) for r in shape["coordinates"]]}
    else:
        geom = {"type": shape["type"], "coordinates": shape["coordinates"]}
    return {"geometry": {"$geoIntersects": {"$geometry": geom}}}


def heatmap_pipeline(minutes, shape=None, metric="density", grid_decimals=3, kind=None):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    match = {"ts": {"$gte": since}}
    if kind:
        match["meta.kind"] = kind
    if shape:
        match["location"] = geo_within(shape) if shape["type"] != "LineString" else None
        if match["location"] is None:
            match.pop("location")
    if metric == "slow":
        match["speed"] = {"$lt": 25}
    return [
        {"$match": match},
        {"$group": {
            "_id": {
                "x": {"$round": [{"$arrayElemAt": ["$location.coordinates", 0]}, grid_decimals]},
                "y": {"$round": [{"$arrayElemAt": ["$location.coordinates", 1]}, grid_decimals]},
            },
            "n": {"$sum": 1},
        }},
        {"$sort": {"n": -1}},
        {"$limit": 15000},
    ]


def replay_pipeline(minutes, step_s, shape=None):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    match = {"ts": {"$gte": since}}
    if shape and shape["type"] != "LineString":
        match["location"] = geo_within(shape)
    return [
        {"$match": match},
        {"$group": {
            "_id": {"t": {"$dateTrunc": {"date": "$ts", "unit": "second", "binSize": step_s}}, "v": "$meta.vid"},
            "c": {"$last": "$location.coordinates"},
            "s": {"$last": "$status"},
            "k": {"$last": "$meta.kind"},
        }},
        {"$group": {"_id": "$_id.t",
                    "p": {"$push": ["$_id.v", {"$arrayElemAt": ["$c", 0]}, {"$arrayElemAt": ["$c", 1]}, "$s", "$k"]}}},
        {"$sort": {"_id": 1}},
    ]


def maritime_pipeline():
    """Port operations picture - vessel-only fields, same collection as the trucks."""
    return [
        {"$match": {"kind": "vessel"}},
        {"$facet": {
            "by_status": [{"$group": {"_id": "$status", "n": {"$sum": 1}}}],
            "anchorage": [
                {"$match": {"status": "anchored"}},
                {"$project": {"name": 1, "type": 1, "location": 1, "voyage.last_port": 1, "dimensions.length_m": 1,
                              "wait_min": {"$dateDiff": {"startDate": "$voyage.anchored_since",
                                                         "endDate": "$$NOW", "unit": "minute"}}}},
                {"$sort": {"wait_min": -1}},
            ],
            "inbound": [
                {"$match": {"status": {"$in": ["underway", "dark"]}, "voyage.phase": {"$in": ["inbound", "approach"]}}},
                {"$project": {"name": 1, "type": 1, "location": 1, "status": 1, "voyage.last_port": 1,
                              "voyage.port": 1, "voyage.phase": 1,
                              "eta_min": {"$dateDiff": {"startDate": "$$NOW", "endDate": "$voyage.eta",
                                                        "unit": "minute"}}}},
                {"$sort": {"eta_min": 1}},
                {"$limit": 15},
            ],
            "berths": [
                {"$match": {"status": "moored"}},
                {"$group": {"_id": "$voyage.port", "n": {"$sum": 1},
                            "vessels": {"$push": {"_id": "$_id", "name": "$name", "type": "$type",
                                                  "berth": "$voyage.berth"}}}},
                {"$sort": {"n": -1}},
            ],
            "dark": [
                {"$match": {"status": "dark"}},
                {"$project": {"name": 1, "type": 1, "location": 1,
                              "silent_s": {"$dateDiff": {"startDate": "$ais.last_msg", "endDate": "$$NOW",
                                                         "unit": "second"}}}},
            ],
            "cargo": [
                {"$group": {"_id": None, "teu_inbound": {"$sum": {"$cond": [
                    {"$in": ["$voyage.phase", ["inbound", "anchored", "approach"]]}, "$cargo.teu", 0]}},
                    "teu_alongside": {"$sum": {"$cond": [{"$eq": ["$status", "moored"]}, "$cargo.teu", 0]}},
                    "passengers": {"$sum": "$passengers"},
                    "hazmat_vessels": {"$sum": {"$cond": [{"$eq": ["$cargo.hazmat", True]}, 1, 0]}}}},
            ],
        }},
    ]


def field_coverage_pipeline():
    """
    Schema analysis in one aggregation: which field paths (2 levels deep) appear in
    which kind of document, and how often. This is the flexible document model, measured.
    """
    return [
        {"$facet": {
            "totals": [{"$group": {"_id": "$kind", "n": {"$sum": 1}}}],
            "types": [{"$group": {"_id": {"kind": "$kind", "type": "$type"}, "n": {"$sum": 1}}},
                      {"$sort": {"n": -1}}],
            "fields": [
                {"$project": {"kind": 1, "paths": {"$reduce": {
                    "input": {"$objectToArray": "$$ROOT"},
                    "initialValue": [],
                    "in": {"$concatArrays": ["$$value", {"$cond": [
                        {"$and": [{"$eq": [{"$type": "$$this.v"}, "object"]}, {"$ne": ["$$this.k", "location"]}]},
                        {"$map": {"input": {"$objectToArray": "$$this.v"}, "as": "c",
                                  "in": {"$concat": ["$$this.k", ".", "$$c.k"]}}},
                        ["$$this.k"],
                    ]}]},
                }}}},
                {"$unwind": "$paths"},
                {"$group": {"_id": {"p": "$paths", "k": "$kind"}, "n": {"$sum": 1}}},
                {"$group": {"_id": "$_id.p", "kinds": {"$push": {"k": "$_id.k", "n": "$n"}}}},
                {"$sort": {"_id": 1}},
            ],
        }},
    ]


def trail_pipeline(vid, minutes=30):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return [
        {"$match": {"meta.vid": vid, "ts": {"$gte": since}}},
        {"$sort": {"ts": 1}},
        {"$project": {"_id": 0, "ts": 1, "c": "$location.coordinates", "speed": 1}},
    ]


# ------------------------------------------------------------------ pretty printing for the inspector
class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            return {"$date": o.isoformat().replace("+00:00", "Z")}
        return str(o)


def to_json(obj):
    return json.loads(json.dumps(obj, cls=_Enc))


def to_shell(collection, op, arg, extra=None):
    """mongosh-pasteable string"""
    txt = json.dumps(arg, cls=_Enc, indent=2)
    # {"$date": "..."} -> ISODate("...")
    import re
    txt = re.sub(r'\{\s*"\$date":\s*"([^"]+)"\s*\}', r'ISODate("\1")', txt)
    if op == "find" and extra:
        return f"db.{collection}.find(\n{txt},\n{json.dumps(extra, indent=2)}\n)"
    return f"db.{collection}.{op}(\n{txt}\n)"
