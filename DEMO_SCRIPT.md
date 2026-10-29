# Demo script: "Where are my trucks, and my ships?" (14–16 min)

**Before you start:** run `python run.py --reset` 10 minutes early so there is telemetry history for the heatmap and replay. Keep the Query Inspector collapsed until step 2.

**Story:** a Dubai logistics operator runs 1,500 trucks between Jebel Ali Port, JAFZA, Dubai South, the industrial areas and retail clusters, and tracks the 75 vessels that feed them at Jebel Ali, Mina Rashid and the Creek. Operations needs to answer "who is where, right now?" in milliseconds, react to events without polling, and look back in time. All of that runs on one database.

---

### 1. The live picture (1 min)
- Point at the map: every dot is a document in `vehicles`, and they're moving right now.
- Point at the top right: **"change stream live · N updates/s."** The browser doesn't poll. One change stream pushes every update.
- KPIs across the top: moving, loading, idle, workshop, offline.

> "This is the operational document: location, status, cargo, driver, fuel, trip, all in one record. No joins to render a truck."

### 2. Draw a polygon (2 min)
- Search area → **Polygon** → draw around Al Quoz / Al Barsha. Click the first point again to close it.
- Results appear: count, tonnage, average speed, low fuel, hazmat, a by-type breakdown, and which zones and highways the shape touches.
- Open the **Query Inspector** (bottom right):
  - `$match` with `$geoWithin` + `$geometry`, then **`$facet`**: the list, counts and KPIs come back in **one round trip**.
  - Switch to **mongosh** → **Copy**. "Paste this in Compass. There's no special API, it's just MQL."
- Drag a vertex. The query re-runs instantly. **Live refresh** re-runs it every 3 s as trucks move in and out.

> "The geo filter and the business filters are one `$match`, served by one compound index `{location: 2dsphere, status, type}`."

### 3. Combine geo with business filters (1 min)
- Click the **reefer** chip, then drag the fuel slider to 30%.
- "Refrigerated trucks in this area with less than 30% fuel." It's still one query and one index.

### 4. Prove the index (1 min)
- Click **Explain**. Compare the two cards:
  - With the 2dsphere index: `IXSCAN`, a few hundred docs examined.
  - Forced collection scan: `COLLSCAN`, every document examined.
- "At 1,500 trucks it's milliseconds either way. At 1.5 million it's the difference between a live map and a spinner."

### 5. Quick scenarios (2 min)
- **Hazmat within 5 km of DXB**: `$centerSphere` radius search (airport safety perimeter).
- **Low fuel along E311 corridor**: draw-a-route search. Show the `$or` of buffered segments in the inspector. "Which trucks on my route are about to run dry?"
- **Heavy trucks in the ban zone**: Dubai restricts heavy trucks downtown at peak hours. Every truck shown is a violation.

### 6. Nearest (1.5 min)
- **Nearest** tab → click anywhere (e.g. a customer site in Al Quoz).
- `$geoNear` returns the closest idle or loading trucks, sorted by distance, with an ETA. Change the type to **reefer**.
- Click a truck → popup → **Nearest depot**. "A breakdown or low fuel: where's the closest service center?"
- Mention that the simulator does this on its own: when a truck drops below 12% fuel, it runs `$geoNear` against `depots` and reroutes. Watch for **⛽ Low fuel reroute** toasts.

### 7. Geofences and change streams (2 min)
- Draw a polygon around a hub (e.g. DXB Cargo Village) → **Geofences** tab → name it *"Customer DC"* → **Save shape**.
- Within seconds, toasts show **➜ Entered Customer DC / ← Left Customer DC**, and the counter next to it updates live.
- "The geofence is a document. The engine checks it with an indexed `$geoWithin` on every tick, writes an event, and the **change stream** delivers it to every connected screen. There's no message broker."
- Point at the **🚫 truck-ban violations** and **❄️ cold-chain alerts** in the feed.
- *Optional power move:* stop the app, run `python run.py --no-sim` in one terminal and `python -m app.simulator` in another. The UI still updates live because it only listens to the database.

### 8. Time series: heatmap and replay (1.5 min)
- **History** → **Show heatmap** → density over the last 30 min. Switch the metric to **Congestion** (speed < 25 km/h).
- Inspector: a `$group` over the **time-series collection**, rounding coordinates to a ~100 m grid.
- **Load replay** → ▶. "The last 10 minutes of the fleet, rebuilt with one aggregation using `$dateTrunc`." If a shape is drawn, only that area replays.
- "Hot operational data and history live in the same cluster. Telemetry expires by itself with a TTL."

### 9. Land meets sea: the flexible document model (4 min)
This is the section to slow down for. Everything so far worked on trucks *and* ships without you saying so.

- Search → **⚓🚚 Jebel Ali port: trucks + ships**. One `$geoWithin`: trucks, container ships, tankers and tugs, counted together. The cards show **road tonnes** from trucks and **TEU** from ships, computed by the same `$group`.
  > "Same collection, same index, same query. Ask a relational schema for that and you get a `UNION` across a trucks table and a vessels table, or a sparse table with 80 nullable columns."
- Drag the **Fuel ≤** slider to 30%. Vessels disappear: they have no `fuel_pct`, so the filter simply doesn't match them. Nothing breaks.
- Click a container ship → popup → **{ } Document**. Then click a truck → **{ } Document**. Different shapes, side by side in the inspector.
- **Model** tab:
  - Counts by `kind` and `type`.
  - **Field coverage**: an aggregation measures which fields each kind actually has. A handful of shared fields power the common queries; truck-only and vessel-only fields sit beside them. "Optional" marks fields that vary *within* a kind: dhows have no `ais.imo`, tugs have no `cargo`.
  - **Compare documents side by side**: pick *truck · reefer* vs *vessel · container ship*. Green keys are shared, orange keys belong to one shape.
  - **Insert a superyacht (new type)**: a brand-new vessel type with fields nobody has (`guests`, `helipad`, a `tenders` array). It shows up on the map off the Palm within a second, via the change stream. "No migration, no `ALTER TABLE`, no deploy."
  - **Insert vessel without MMSI** → rejected (error 121). "Flexible doesn't mean ungoverned. The `$jsonSchema` validator requires the shared core for everyone, plus a plate and driver for trucks or an AIS MMSI for vessels. Everything else is free."
  - **Indexes**: one 2dsphere index serves both kinds. The partial indexes on `ais.mmsi` (unique) and `ais.last_msg` only contain vessel documents, so trucks cost nothing there.
- **Maritime** tab: anchorage queue with waiting times (`$dateDiff`), inbound ETAs, berth occupancy, AIS-dark vessels, all from vessel-only fields in the same collection. Point at the alerts: **🚤 harbour speeding** (the 6 kn limit is a field on the zone document), **🛑 protected zone**, **📡 went dark** (found via the partial index), **⚠️ close quarters** (`$geoNear` between ships).
- Click a berthed container ship → **Dispatch trucks**: `$geoNear` from the ship's position, filtered to idle container trucks. "Port and road operations meet in one query."
- **History** → heatmap → **Vessels only**: the shipping lanes appear from the time-series collection. Vessel telemetry has `speed_kn`, truck telemetry has `speed` and `fuel`: the time-series collection is flexible too.

### Close
> "Trucks and ships, live location, rich filters, nearest-neighbor, geofencing, event streaming and history, all in one Atlas cluster with one query language, and one collection that took on a new kind of asset without a migration. Adding Atlas Search on driver or cargo text, or Vector Search for similar-route matching, doesn't need another system."

---

**Likely questions**
- *Isn't one collection for everything messy?* Only if it has no rules. Here there's a discriminator (`kind`), a shared core the queries rely on, a validator per kind, and partial indexes for kind-specific fields. That's MongoDB's polymorphic pattern. Split into separate collections when access patterns diverge, not because the fields differ.
- *Scale?* Geo indexes are B-tree on S2 cells and shard well (shard key e.g. `{region, _id}`). Change streams work across shards.
- *Accuracy?* `2dsphere` uses spherical geometry (WGS84), and distances are in meters.
- *Existing event bus?* The MongoDB Kafka connector can publish the same change events to Kafka.
- *Why not PostGIS?* PostGIS is strong on GIS analysis. The pitch here is operational: the document model plus geo plus streaming plus time series in one platform, without ETL.
