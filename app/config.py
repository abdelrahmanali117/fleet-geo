import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


MONGODB_URI = os.getenv("MONGODB_URI", "")
DB_NAME = os.getenv("DB_NAME", "fleet_geo")

FLEET_SIZE = int(os.getenv("FLEET_SIZE", "1500"))
VESSELS = int(os.getenv("VESSELS", "75"))                    # vessels in the same collection (0 = trucks only)
VESSEL_TIME_SCALE = float(os.getenv("VESSEL_TIME_SCALE", "10"))  # ships are slow: speed them up more
AIS_GAP_SECONDS = int(os.getenv("AIS_GAP_SECONDS", "20"))   # no AIS message for this long = "dark"
CPA_METERS = int(os.getenv("CPA_METERS", "400"))            # close-quarters alert distance
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2"))
TIME_SCALE = float(os.getenv("TIME_SCALE", "4"))            # sim speed-up: 4 = trucks move 4x real speed
TELEMETRY_EVERY = int(os.getenv("TELEMETRY_EVERY", "5"))     # write telemetry every N ticks
TELEMETRY_TTL_HOURS = int(os.getenv("TELEMETRY_TTL_HOURS", "3"))
GEOFENCE_EVERY = int(os.getenv("GEOFENCE_EVERY", "1"))       # check geofences every N ticks
BAN_ALWAYS_ON = _bool("BAN_ALWAYS_ON", True)                # demo mode: enforce truck ban at any hour
SIMULATE = _bool("SIMULATE", True)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
