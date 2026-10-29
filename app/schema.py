"""
Governed flexibility: one collection, many shapes, a few rules.

Every document must have the shared core (kind, type, status, location).
Then each kind has its own minimum: trucks need a plate + driver, vessels need
an AIS identity (MMSI) + name. Everything else is free to vary per document.
"""
TRUCK_STATUSES = ["moving", "loading", "idle", "maintenance", "offline"]
VESSEL_STATUSES = ["underway", "anchored", "moored", "dark"]

VEHICLE_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["kind", "type", "status", "location"],
        "properties": {
            "kind": {"enum": ["truck", "vessel"], "description": "discriminator for the polymorphic collection"},
            "type": {"bsonType": "string"},
            "location": {
                "bsonType": "object",
                "required": ["type", "coordinates"],
                "properties": {
                    "type": {"enum": ["Point"]},
                    "coordinates": {"bsonType": "array", "minItems": 2, "maxItems": 2},
                },
            },
        },
        "oneOf": [
            {
                "properties": {"kind": {"enum": ["truck"]}, "status": {"enum": TRUCK_STATUSES}},
                "required": ["plate", "driver"],
            },
            {
                "properties": {
                    "kind": {"enum": ["vessel"]},
                    "status": {"enum": VESSEL_STATUSES},
                    "ais": {"bsonType": "object", "required": ["mmsi"],
                            "properties": {"mmsi": {"bsonType": ["int", "long"]}}},
                },
                "required": ["ais", "name"],
            },
        ],
    }
}
