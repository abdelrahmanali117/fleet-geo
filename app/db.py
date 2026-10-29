import logging

import certifi

from pymongo import ASCENDING, AsyncMongoClient, MongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

from . import config

log = logging.getLogger("fleet.db")

_client = None


def _tls():
    # use certifi's CA bundle so Atlas TLS works on macOS python.org installs out of the box
    return {"tlsCAFile": certifi.where()} if config.MONGODB_URI.startswith("mongodb+srv") or "tls=true" in config.MONGODB_URI else {}


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        if not config.MONGODB_URI:
            raise RuntimeError("MONGODB_URI is not set. Copy .env.example to .env and add your Atlas connection string.")
        _client = AsyncMongoClient(config.MONGODB_URI, appname="fleet-geo-demo", tz_aware=True, **_tls())
    return _client


def get_db():
    return get_client()[config.DB_NAME]


def sync_db():
    """Sync client used by the seeder / CLI."""
    if not config.MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not set. Copy .env.example to .env and add your Atlas connection string.")
    return MongoClient(config.MONGODB_URI, appname="fleet-geo-seed", tz_aware=True,
                       serverSelectionTimeoutMS=15000, **_tls())[config.DB_NAME]


def ensure_schema(db):
    """Create collections + indexes (sync). Safe to run repeatedly."""
    existing = set(db.list_collection_names())

    # Time-series collection for raw GPS telemetry
    if "telemetry" not in existing:
        try:
            db.create_collection(
                "telemetry",
                timeseries={"timeField": "ts", "metaField": "meta", "granularity": "seconds"},
                expireAfterSeconds=config.TELEMETRY_TTL_HOURS * 3600,
            )
            log.info("created time-series collection 'telemetry'")
        except (CollectionInvalid, OperationFailure) as e:
            log.warning("time-series create failed (%s) - falling back to a regular collection", e)
            db.create_collection("telemetry")
            db.telemetry.create_index("ts", expireAfterSeconds=config.TELEMETRY_TTL_HOURS * 3600)

    # Polymorphic `vehicles` collection (trucks + vessels) with a $jsonSchema validator
    apply_validator(db, "vehicles" in existing)

    # Geospatial indexes
    # one compound 2dsphere index serves pure geo queries and geo + kind/status/type filters
    # (only one 2dsphere index on the collection keeps $geoNear unambiguous)
    if "location_status_type" in db.vehicles.index_information():
        db.vehicles.drop_index("location_status_type")  # v1 index, superseded below
    db.vehicles.create_index([("location", "2dsphere"), ("kind", ASCENDING), ("status", ASCENDING),
                              ("type", ASCENDING)], name="location_kind_status_type")
    # Partial indexes: only vessel documents are indexed - trucks don't have these fields
    db.vehicles.create_index([("ais.mmsi", ASCENDING)], name="ais_mmsi_unique_vessels", unique=True,
                             partialFilterExpression={"kind": "vessel"})
    db.vehicles.create_index([("ais.last_msg", ASCENDING)], name="ais_last_msg_vessels",
                             partialFilterExpression={"kind": "vessel"})
    db.zones.create_index([("geometry", "2dsphere")], name="geometry_2dsphere")
    db.routes.create_index([("geometry", "2dsphere")], name="geometry_2dsphere")
    db.depots.create_index([("location", "2dsphere")], name="location_2dsphere")
    try:
        db.telemetry.create_index([("meta.vid", ASCENDING), ("ts", ASCENDING)], name="vid_ts")
        db.telemetry.create_index([("location", "2dsphere")], name="location_2dsphere")
    except OperationFailure as e:
        log.warning("telemetry secondary index skipped: %s", e)

    # Events expire after a day
    db.events.create_index("ts", expireAfterSeconds=86400, name="ts_ttl")


def apply_validator(db, exists):
    from .schema import VEHICLE_VALIDATOR
    try:
        if exists:
            db.command("collMod", "vehicles", validator=VEHICLE_VALIDATOR,
                       validationLevel="moderate", validationAction="error")
        else:
            db.create_collection("vehicles", validator=VEHICLE_VALIDATOR,
                                 validationLevel="moderate", validationAction="error")
        log.info("schema validator active on 'vehicles'")
    except OperationFailure as e:
        log.warning("could not apply schema validator (needs dbAdmin/Atlas admin): %s", e)
        if not exists:
            db.create_collection("vehicles")
