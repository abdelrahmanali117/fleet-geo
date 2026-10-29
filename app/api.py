from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pymongo.errors import OperationFailure, PyMongoError

from . import config, queries as Q
from .db import get_db
from .simulator import Simulator

log = logging.getLogger("fleet.api")
STATIC = Path(__file__).parent / "static"


# ------------------------------------------------------------------ helpers
def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, ObjectId):
        return str(o)
    return o


def envelope(collection, op, arg, results, ms, extra=None, **more):
    return clean({
        "results": results,
        "inspector": {
            "collection": collection, "op": op, "ms": round(ms, 1),
            "mql": Q.to_json(arg), "shell": Q.to_shell(collection, op, arg, extra),
        },
        **more,
    })


async def run_agg(coll, pipeline):
    t0 = time.perf_counter()
    try:
        cur = await get_db()[coll].aggregate(pipeline)
        rows = await cur.to_list(None)
    except OperationFailure as e:
        raise HTTPException(400, f"MongoDB rejected the query: {e.details.get('errmsg', str(e)) if e.details else e}")
    return rows, (time.perf_counter() - t0) * 1000


# ------------------------------------------------------------------ live broadcast (change streams -> websockets)
class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.pending_positions = {}
        self.coords = {}  # last known [lng, lat] per vehicle, to merge partial (delta) updates
        self.stream_ok = False
        self.events_seen = 0

    async def send_all(self, msg):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def flusher(self):
        while True:
            await asyncio.sleep(0.5)
            if self.pending_positions and self.clients:
                items = list(self.pending_positions.values())
                self.pending_positions.clear()
                await self.send_all({"type": "positions", "items": items})
            elif not self.clients:
                self.pending_positions.clear()

    async def watch(self):
        db = get_db()
        pipeline = [
            {"$match": {"ns.coll": {"$in": ["vehicles", "events", "zones"]},
                        "operationType": {"$in": ["insert", "update", "replace", "delete"]}}},
            {"$project": {"operationType": 1, "ns": 1, "documentKey": 1, "fullDocument": 1,
                          "updateDescription.updatedFields": 1}},
        ]
        backoff = 1
        while True:
            try:
                if not self.coords:
                    rows = await db.vehicles.find({}, {"location.coordinates": 1}).to_list(None)
                    self.coords = {r["_id"]: list(r["location"]["coordinates"]) for r in rows}
                async with await db.watch(pipeline) as stream:
                    self.stream_ok = True
                    await self.send_all({"type": "stream", "ok": True})
                    log.info("change stream open")
                    backoff = 1
                    async for ch in stream:
                        self.events_seen += 1
                        await self.handle(ch)
            except asyncio.CancelledError:
                raise
            except PyMongoError as e:
                self.stream_ok = False
                await self.send_all({"type": "stream", "ok": False})
                log.warning("change stream error: %s - retrying in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def handle(self, ch):
        coll, op = ch["ns"]["coll"], ch["operationType"]
        _id = ch["documentKey"]["_id"]
        if coll == "vehicles" and op == "update":
            f = ch.get("updateDescription", {}).get("updatedFields", {})
            loc = merge_location(f, self.coords.get(_id))
            if loc is None:
                return
            self.coords[_id] = loc
            prev = self.pending_positions.get(_id)
            status = f.get("status") or (prev[3] if prev else None)
            speed = f.get("speed_kmh", f.get("speed_kn"))
            self.pending_positions[_id] = [_id, loc[0], loc[1], status, f.get("heading"), speed, f.get("fuel_pct")]
        elif coll == "vehicles" and op in ("insert", "replace"):
            doc = ch["fullDocument"]
            self.coords[_id] = list(doc["location"]["coordinates"])
            await self.send_all({"type": "vehicle_added", "row": compact_row(doc)})
        elif coll == "vehicles" and op == "delete":
            self.coords.pop(_id, None)
            self.pending_positions.pop(_id, None)
            await self.send_all({"type": "vehicle_removed", "id": _id})
        elif coll == "events" and op == "insert":
            await self.send_all({"type": "event", "event": clean(ch["fullDocument"])})
        elif coll == "zones":
            f = ch.get("updateDescription", {}).get("updatedFields", {})
            if op == "update" and set(f) == {"inside_count"}:
                await self.send_all({"type": "zone_count", "id": _id, "count": f["inside_count"]})
            else:
                await self.send_all({"type": "zones_changed"})


def compact_row(r):
    """one row per vehicle for the map, whatever its shape"""
    return [r["_id"], *r["location"]["coordinates"], r.get("status"), r.get("type"), r.get("heading", 0),
            r.get("speed_kmh", r.get("speed_kn", 0)), r.get("fuel_pct"), (r.get("cargo") or {}).get("hazmat", False),
            r.get("kind", "truck"), r.get("name")]


def merge_location(f, prev):
    """
    Change events may carry the whole `location` or, with delta-format oplog entries,
    only `location.coordinates` / `location.coordinates.0|1`. Merge onto the last known value.
    """
    c = list(prev) if prev else None
    if "location" in f:
        c = list(f["location"]["coordinates"])
    if "location.coordinates" in f:
        c = list(f["location.coordinates"])
    for i in (0, 1):
        k = f"location.coordinates.{i}"
        if k in f and c is not None:
            c[i] = f[k]
    return c


hub = Hub()
sim: Simulator | None = None


@asynccontextmanager
async def lifespan(app):
    global sim
    tasks = [asyncio.create_task(hub.watch()), asyncio.create_task(hub.flusher())]
    if config.SIMULATE:
        sim = Simulator(get_db())
        tasks.append(asyncio.create_task(sim.run()))
    yield
    if sim:
        sim.stop()
    for t in tasks:
        t.cancel()


app = FastAPI(title="MongoDB Geospatial Fleet Demo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    await ws.send_json({"type": "stream", "ok": hub.stream_ok})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


# ------------------------------------------------------------------ reference data
@app.get("/api/meta")
async def meta():
    return {"simulate": config.SIMULATE, "time_scale": config.TIME_SCALE, "tick": config.TICK_SECONDS,
            "db": config.DB_NAME, "ban_always_on": config.BAN_ALWAYS_ON}


@app.get("/api/vehicles")
async def vehicles():
    rows = await get_db().vehicles.find({}, {"kind": 1, "name": 1, "type": 1, "status": 1, "location.coordinates": 1,
                                             "heading": 1, "speed_kmh": 1, "speed_kn": 1, "fuel_pct": 1,
                                             "cargo.hazmat": 1}).to_list(None)
    return [compact_row(r) for r in rows]


@app.get("/api/vehicles/{vid}")
async def vehicle(vid: str):
    db = get_db()
    doc = await db.vehicles.find_one({"_id": vid})
    if not doc:
        raise HTTPException(404, "not found")
    pipeline = Q.trail_pipeline(vid, 30)
    rows, ms = await run_agg("telemetry", pipeline)
    trail = [r["c"] for r in rows]
    return envelope("telemetry", "aggregate", pipeline, {"vehicle": doc, "trail": trail}, ms)


@app.get("/api/zones")
async def zones():
    return clean(await get_db().zones.find({}).to_list(None))


@app.post("/api/zones")
async def create_zone(body: dict = Body(...)):
    shape = body["shape"]
    if shape["type"] == "Circle":
        geom = Q.intersects_query(shape)["geometry"]["$geoIntersects"]["$geometry"]
    elif shape["type"] == "Polygon":
        geom = {"type": "Polygon", "coordinates": [Q.close_ring(r) for r in shape["coordinates"]]}
    else:
        raise HTTPException(400, "Geofences must be a polygon, rectangle or circle")
    doc = {"_id": f"custom-{uuid.uuid4().hex[:6]}", "name": body.get("name") or "Custom geofence",
           "kind": "custom", "geometry": geom, "rules": {"alert_on": ["enter", "exit"]}, "builtin": False,
           "created_at": datetime.now(timezone.utc)}
    t0 = time.perf_counter()
    try:
        await get_db().zones.insert_one(doc)
    except OperationFailure as e:
        raise HTTPException(400, f"Invalid polygon: {e}")
    return envelope("zones", "insertOne", doc, {"id": doc["_id"]}, (time.perf_counter() - t0) * 1000)


@app.delete("/api/zones/{zid}")
async def delete_zone(zid: str):
    r = await get_db().zones.delete_one({"_id": zid, "builtin": False})
    if not r.deleted_count:
        raise HTTPException(400, "Only custom geofences can be deleted")
    return {"ok": True}


@app.get("/api/routes")
async def routes():
    return clean(await get_db().routes.find({}).to_list(None))


@app.get("/api/depots")
async def depots():
    return clean(await get_db().depots.find({}).to_list(None))


@app.get("/api/events")
async def events(limit: int = 60):
    rows = await get_db().events.find({"kind": {"$in": ALERT_KINDS + ["enter", "exit"]}}) \
        .sort("ts", -1).limit(limit).to_list(None)
    return clean(rows)


ALERT_KINDS = ["violation", "low_fuel", "cold_chain", "speeding", "ais_gap", "ais_resumed", "close_quarters"]


# ------------------------------------------------------------------ geo queries
@app.post("/api/search/within")
async def search_within(body: dict = Body(...)):
    shape, filters = body["shape"], body.get("filters", {})
    pipeline = Q.within_pipeline(shape, filters)
    rows, ms = await run_agg("vehicles", pipeline)
    r = rows[0] if rows else {"vehicles": [], "by_status": [], "by_type": [], "by_kind": [], "summary": []}
    r["summary"] = r["summary"][0] if r["summary"] else {"count": 0}
    return envelope("vehicles", "aggregate", pipeline, r, ms)


@app.post("/api/search/near")
async def search_near(body: dict = Body(...)):
    target = body.get("target", "vehicles")
    pipeline = Q.near_pipeline(body["point"], body.get("filters", {}), int(body.get("limit", 5)),
                               body.get("max_distance_m"), target)
    rows, ms = await run_agg("vehicles" if target == "vehicles" else "depots", pipeline)
    return envelope("vehicles" if target == "vehicles" else "depots", "aggregate", pipeline, rows, ms)


@app.post("/api/search/intersects")
async def search_intersects(body: dict = Body(...)):
    q = Q.intersects_query(body["shape"])
    db = get_db()
    t0 = time.perf_counter()
    try:
        zones, routes = await asyncio.gather(
            db.zones.find(q, {"name": 1, "kind": 1}).to_list(None),
            db.routes.find(q, {"name": 1, "length_km": 1}).to_list(None))
    except OperationFailure as e:
        raise HTTPException(400, str(e))
    return envelope("zones", "find", q, {"zones": zones, "routes": routes}, (time.perf_counter() - t0) * 1000,
                    extra={"name": 1, "kind": 1})


@app.post("/api/explain")
async def explain(body: dict = Body(...)):
    """Explain the spatial $match for the current shape - with the index and with a forced collection scan."""
    q = Q.spatial_match(body["shape"], body.get("filters", {}))
    coll = get_db().vehicles
    out = {}
    for label, hint in (("with_index", None), ("collection_scan", [("$natural", 1)])):
        cur = coll.find(q, {"_id": 1})
        if hint:
            cur = cur.hint(hint)
        try:
            ex = await cur.explain()
        except OperationFailure as e:
            raise HTTPException(400, str(e))
        out[label] = summarize_explain(ex)
    return envelope("vehicles", "find", q, out, 0, extra={"_id": 1})


def summarize_explain(ex):
    stages, indexes = [], []

    def walk(n):
        if isinstance(n, dict):
            if "stage" in n:
                stages.append(n["stage"])
            if "indexName" in n:
                indexes.append(n["indexName"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    qp = ex.get("queryPlanner", {})
    walk(qp.get("winningPlan", {}))
    es = ex.get("executionStats", {})
    return {
        "stages": list(dict.fromkeys(stages)),
        "indexes": list(dict.fromkeys(indexes)),
        "keys_examined": es.get("totalKeysExamined"),
        "docs_examined": es.get("totalDocsExamined"),
        "returned": es.get("nReturned"),
        "ms": es.get("executionTimeMillis"),
    }


# ------------------------------------------------------------------ time series: heatmap + replay
@app.post("/api/heatmap")
async def heatmap(body: dict = Body(...)):
    pipeline = Q.heatmap_pipeline(int(body.get("minutes", 30)), body.get("shape"), body.get("metric", "density"),
                                  kind=body.get("kind") or None)
    rows, ms = await run_agg("telemetry", pipeline)
    pts = [[r["_id"]["y"], r["_id"]["x"], r["n"]] for r in rows if r["_id"]["x"] is not None]
    return envelope("telemetry", "aggregate", pipeline, pts, ms)


@app.post("/api/replay")
async def replay(body: dict = Body(...)):
    minutes = int(body.get("minutes", 10))
    step = max(config.TICK_SECONDS * config.TELEMETRY_EVERY, round(minutes * 60 / 90))
    pipeline = Q.replay_pipeline(minutes, int(step), body.get("shape"))
    rows, ms = await run_agg("telemetry", pipeline)
    frames = [{"t": r["_id"], "p": r["p"]} for r in rows]
    return envelope("telemetry", "aggregate", pipeline, frames, ms, step=step)


# ------------------------------------------------------------------ maritime operations
@app.get("/api/maritime")
async def maritime():
    pipeline = Q.maritime_pipeline()
    rows, ms = await run_agg("vehicles", pipeline)
    r = rows[0] if rows else {}
    r["cargo"] = (r.get("cargo") or [{}])[0] if r.get("cargo") else {}
    return envelope("vehicles", "aggregate", pipeline, r, ms)


# ------------------------------------------------------------------ document model
@app.get("/api/model/fields")
async def model_fields():
    pipeline = Q.field_coverage_pipeline()
    rows, ms = await run_agg("vehicles", pipeline)
    return envelope("vehicles", "aggregate", pipeline, rows[0] if rows else {}, ms)


@app.get("/api/model/sample")
async def model_sample(kind: str = "truck", type: str = ""):
    match = {"kind": kind}
    if type:
        match["type"] = type
    pipeline = [{"$match": match}, {"$sample": {"size": 1}}]
    rows, ms = await run_agg("vehicles", pipeline)
    return envelope("vehicles", "aggregate", pipeline, rows[0] if rows else None, ms)


@app.get("/api/model/governance")
async def model_governance():
    db = get_db()
    info = await (await db.list_collections(filter={"name": "vehicles"})).to_list(None)
    validator = (info[0].get("options", {}) if info else {}).get("validator")
    idx = await db.vehicles.index_information()
    indexes = [{"name": k, "key": [[f, str(d)] for f, d in v["key"]],
                **({"partialFilterExpression": v["partialFilterExpression"]} if "partialFilterExpression" in v else {}),
                **({"unique": True} if v.get("unique") else {})} for k, v in idx.items()]
    options = info[0].get("options", {}) if info else {}
    return clean({"validator": validator, "validationLevel": options.get("validationLevel"),
                  "validationAction": options.get("validationAction"), "indexes": indexes})


@app.post("/api/model/insert")
async def model_insert(body: dict = Body(...)):
    """Live schema demo: insert a brand-new vessel type, or a document that breaks the rules."""
    from .maritime_data import invalid_vessel, superyacht
    now = datetime.now(timezone.utc)
    doc = superyacht(now) if body.get("variant") == "yacht" else invalid_vessel(now)
    coll = get_db().vehicles
    t0 = time.perf_counter()
    try:
        await coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    except OperationFailure as e:
        details = (e.details or {}).get("errInfo")
        return envelope("vehicles", "insertOne", doc, {"ok": False, "error": (e.details or {}).get("errmsg", str(e)),
                                                       "errInfo": details}, (time.perf_counter() - t0) * 1000)
    ok = {"ok": True, "id": doc["_id"]}
    if body.get("variant") != "yacht":  # the validator should have stopped this: clean up and say so
        await coll.delete_one({"_id": doc["_id"]})
        ok = {"ok": False, "error": "Inserted without error - the schema validator is not active on this cluster "
                                    "(the database user needs permission to run collMod). Removed again."}
    return envelope("vehicles", "insertOne", doc, ok, (time.perf_counter() - t0) * 1000)


@app.delete("/api/model/yacht")
async def model_remove_yacht():
    r = await get_db().vehicles.delete_many({"type": "superyacht"})
    return {"deleted": r.deleted_count}
