# Dubai Fleet: a MongoDB geospatial demo (land + sea)

A live map of about 1,500 trucks on Dubai's freight corridors (E11, E311, E611, Al Khail, Jebel Ali to Lehbab) and 75 vessels around Jebel Ali, Mina Rashid and Dubai Creek. Draw a polygon, box, circle or route corridor and MongoDB Atlas returns the trucks and ships inside it. The query re-runs every time you edit the shape.

**The point of the vessels:** trucks and ships live in the **same `vehicles` collection**. They share a small core (`kind`, `type`, `status`, `location`, `heading`), and each kind carries its own fields: trucks have `plate`, `driver`, `fuel_pct`, `trip`; vessels have `ais.mmsi`, `dimensions`, `voyage`, `cargo.teu`. Even within vessels the shape varies: dhows have no IMO number, tugs have `bollard_pull_t` and no cargo, cruise ships have `passengers`. One geo index, one query language, one change stream cover all of them.

Everything runs on your laptop with Python and talks to your Atlas cluster.

![stack](https://img.shields.io/badge/Python-FastAPI%20%2B%20PyMongo%20Async-00ED64) ![map](https://img.shields.io/badge/Map-Leaflet%20%2B%20Geoman-3D9CFF)

---

## Quick start (5 minutes)

**You need:** Python 3.10+ and an Atlas cluster. Add your IP under **Network Access** in Atlas.

```bash
cd mongo-fleet-geo
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
# edit .env and paste your Atlas connection string into MONGODB_URI

python run.py
```

The browser opens at <http://127.0.0.1:8000>. The first run seeds the fleet, which takes about 10 seconds.

> **Upgrading from the trucks-only version?** Run `python run.py --reset` for a clean start. If you just run `python run.py`, it adds the vessels to your existing collection, backfills `kind: "truck"` on the old documents, swaps in the new index and adds the validator, all without dropping anything.

| Command | What it does |
|---|---|
| `python run.py` | Seeds if the database is empty, then starts the simulator and web app |
| `python run.py --reset` | Wipes and reseeds everything (use before a customer demo for a clean slate) |
| `python run.py --fleet 300` | Seeds a smaller fleet (use with `--reset`) |
| `python run.py --no-sim` + `python -m app.simulator` | Runs the writer in a separate terminal (see "Change streams" below) |

> **Cluster size:** the default 1,500 trucks write about 500 updates/sec. That's fine on M10+. On **M0 / Flex**, set `FLEET_SIZE=300` in `.env` and run `python run.py --reset`, because shared tiers cap ops/sec.

---

## What it shows

| Feature | MongoDB capability | Where in the UI |
|---|---|---|
| Draw polygon / box and see trucks inside | `$geoWithin` + `$geometry` on a `2dsphere` index | Search area |
| Draw a circle (radius search) | `$geoWithin` + `$centerSphere` | Search area |
| Route corridor ("trucks within 800 m of this road") | `$or` of `$geoWithin` buffers, each clause index-backed | Search area, Corridor |
| Geo + business filters in one query (type, status, fuel, hazmat) | Compound `2dsphere` index `{location, status, type}` | Filters |
| Live KPIs for the area (counts, tonnage, avg speed, low fuel) | One aggregation with `$facet` | Result cards |
| Which zones / highways a shape touches | `$geoIntersects` against Polygons and LineStrings | "$geoIntersects" line under results |
| Nearest available trucks / nearest depot, with distance and ETA | `$geoNear` with `query`, `maxDistance` | Nearest tab, truck popup |
| Low-fuel trucks rerouted to the closest depot automatically | `$geoNear` inside the simulator | Alerts |
| Geofences: enter/exit alerts, truck-ban violations | Indexed `$geoWithin` per zone + **change streams** to the browser | Geofences tab, toasts |
| Live truck movement with no polling | **Change streams** to WebSocket | The whole map |
| Heatmap of density or congestion | **Time-series collection** + `$group` on a rounded grid | History, Heatmap |
| Replay the last N minutes | Time series + `$dateTrunc` framing in one pipeline | History, Replay |
| Index vs collection scan | `explain("executionStats")` with and without `hint({$natural:1})` | Inspector, Explain |
| See / copy every query | Exact MQL + mongosh syntax | Query inspector (bottom right) |
| **Trucks + ships in one query** ("everything within 6 km of Jebel Ali") | Polymorphic collection, one `2dsphere` index `{location, kind, status, type}` | Search scenarios |
| Filters that only apply where the field exists (fuel ≤ 30% returns trucks only) | Query semantics on missing fields | Filters |
| Anchorage queue, waiting time, inbound ETAs, berth occupancy | `$facet` over vessel-only fields, `$dateDiff` | Maritime tab |
| Harbour speeding (6 kn Jebel Ali, 5 kn Creek) | Zone rule `max_speed_kn` + `$geoWithin` + `speed_kn` filter | Maritime alerts |
| Protected-zone entry | `$geoWithin` per zone, `rules.applies_to: ["vessel"]` | Maritime alerts |
| Vessels going AIS-dark | **Partial index** on `ais.last_msg` (vessels only) | Maritime tab, alerts |
| Close-quarters situations | `$geoNear` between vessels under way | Maritime alerts |
| Dispatch trucks to a berthed ship | `$geoNear` across kinds | Vessel popup, Dispatch trucks |
| Field coverage per kind (shared / truck-only / vessel-only / optional) | Schema analysis with `$objectToArray` + `$reduce` | Model tab |
| Side-by-side documents | Same collection, different shapes | Model tab, Compare |
| New vessel type live (superyacht with `guests`, `helipad`, `tenders[]`) | No migration; the change stream puts it on the map | Model tab |
| Rejected document (vessel without MMSI) | `$jsonSchema` validator with `oneOf` per kind | Model tab |

---

## How it fits together

```
┌──────────────┐   bulk_write every 2s      ┌──────────────────────────┐
│  Simulator   │ ─────────────────────────▶ │     MongoDB Atlas        │
│ (road graph, │   insert_many telemetry    │  vehicles  (2dsphere)    │
│  geofences,  │ ─────────────────────────▶ │  telemetry (time series) │
│  alerts)     │   $geoWithin per zone      │  zones / routes / depots │
└──────────────┘ ◀───────────────────────── │  events   (TTL)          │
                                            └────────────┬─────────────┘
                                                         │ change stream
┌──────────────┐   WebSocket (positions,    ┌────────────▼─────────────┐
│  Browser     │ ◀──── events, counts) ──── │  FastAPI  (PyMongo async)│
│  Leaflet map │ ────── REST geo queries ──▶│  query builders + explain│
└──────────────┘                            └──────────────────────────┘
```

The web app never polls for positions. It opens one change stream on the database and forwards vehicle moves (batched every 500 ms), geofence events and zone counts over a WebSocket.

### Collections

| Collection | Shape | Indexes |
|---|---|---|
| `vehicles` (trucks) | `{_id, kind: "truck", plate, type, status, location: Point, speed_kmh, heading, fuel_pct, cargo{type, weight_t, hazmat}, driver, trip, reefer_temp_c, last_update}` | `{location: "2dsphere", kind, status, type}` |
| `vehicles` (vessels) | `{_id, kind: "vessel", name, type, status, location, speed_kn, heading, flag, operator, ais{mmsi, imo?, callsign, nav_status, last_msg}, dimensions, voyage{phase, port, berth, last_port, next_port, eta, anchored_since}, cargo?{teu \| dwt \| ceu \| weight_t, hazmat, imdg_classes}, passengers?, bollard_pull_t?}` | same 2dsphere index + partial `{ais.mmsi}` unique and `{ais.last_msg}` (`kind: "vessel"` only); `$jsonSchema` validator |
| `telemetry` | time series: trucks `{ts, meta{vid, type, kind}, location, speed, fuel, status}`, vessels `{..., speed_kn, status}` | `{meta.vid, ts}`, `{location: "2dsphere"}`, TTL 3 h |
| `zones` | `{_id, name, kind, geometry: Polygon, rules, inside_count}` | `{geometry: "2dsphere"}` |
| `routes` | roads `{kind: "road", name, geometry: LineString, length_km}` and sea lanes `{kind: "sea_lane", ..., length_nm}` | `{geometry: "2dsphere"}` |
| `depots` | `{_id, name, kind, location: Point}` | `{location: "2dsphere"}` |
| `events` | `{ts, kind, severity, message, vehicle_id, zone_id, location}` | TTL 24 h |

### Project layout

```
run.py              one-command launcher (seed + web + simulator)
app/config.py       settings from .env
app/db.py           Atlas client, collections, indexes, time-series setup
app/geo_data.py     Dubai road graph, hubs, zones, depots (hand-traced)
app/maritime_data.py sea lanes, ports, maritime zones, vessel generator
app/schema.py       the $jsonSchema validator for the polymorphic collection
app/seed.py         synthetic fleet generator (trucks + vessels, one insert)
app/simulator.py    truck movement, telemetry, geofencing, alerts
app/vessel_sim.py   vessel voyages, AIS watchdog, close-quarters monitor
app/queries.py      every MQL query / pipeline the demo runs (pure functions)
app/api.py          FastAPI routes, WebSocket, change-stream watcher
app/static/         the map UI (Leaflet, Geoman, heat plugin are vendored)
```

---

## Settings (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `MONGODB_URI` | none | Atlas SRV string |
| `DB_NAME` | `fleet_geo` | |
| `FLEET_SIZE` | `1500` | Trucks seeded. Use 300 on M0/Flex |
| `VESSELS` | `75` | Vessels seeded into the same collection (0 = trucks only) |
| `VESSEL_TIME_SCALE` | `10` | Ships move 10× real speed |
| `AIS_GAP_SECONDS` | `20` | No AIS message for this long while under way means the vessel is "dark" |
| `CPA_METERS` | `400` | Close-quarters alert distance between vessels |
| `TICK_SECONDS` | `2` | How often trucks move |
| `TIME_SCALE` | `4` | Trucks drive 4× real speed so movement is visible on stage |
| `TELEMETRY_EVERY` | `5` | Time-series point every N ticks (every 10 s) |
| `TELEMETRY_TTL_HOURS` | `3` | Telemetry auto-expires |
| `BAN_ALWAYS_ON` | `true` | Enforce the downtown truck ban at any hour. Set to `false` to use real peak hours (6:30–9, 13–15, 17:30–20) |

---

## Troubleshooting

- **`ServerSelectionTimeoutError`**: your IP isn't on the Atlas access list, or you're on a network that blocks port 27017. Try a phone hotspot.
- **`CERTIFICATE_VERIFY_FAILED` on macOS**: the app already uses `certifi`. If it still fails, run `/Applications/Python 3.x/Install Certificates.command`.
- **Map is blank but trucks show**: the base map tiles come from CARTO/Esri and need internet. Everything else (JS libraries included) is served locally.
- **Heatmap / replay say "not enough history"**: telemetry is written every ~10 s. Let it run 2–3 minutes before the demo. It's best to start the app 10 minutes early.
- **"Loop is not valid" error when drawing**: the polygon crosses itself. Redraw it.
- **Model tab says "No validator"**: the database user can't run `collMod`. Give it the Atlas admin role (or `dbAdmin` on the database) and run `python run.py` again. Everything else still works.
- **Ships sit slightly on land at high zoom**: the sea lanes and berths are hand-placed approximations. Adjust the points in `app/maritime_data.py` if you need them exact.
- **Too many toasts**: toasts show only alerts and custom-geofence events. Hub enter/exit events go to the feed only when "include hub enter/exit" is on.

All data is synthetic: companies, drivers, plates, vessel names, MMSI and IMO numbers are invented. Road geometry is a hand-traced approximation of Dubai's freight corridors.
