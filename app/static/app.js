/* Dubai Fleet - MongoDB geospatial demo UI (trucks + vessels in one collection) */
(() => {
"use strict";

const COLORS = {
  // trucks
  moving: "#00ED64", loading: "#3D9CFF", idle: "#FFC010", maintenance: "#B45AF2", offline: "#6B7B83",
  // vessels
  underway: "#36D7F5", anchored: "#FF9F43", moored: "#8F9CFF", dark: "#FF5C5C",
};
const TRUCK_TYPES = ["container", "box", "reefer", "flatbed", "tanker", "tipper"];
const VESSEL_TYPES = ["container_ship", "oil_tanker", "bulk_carrier", "ro_ro", "passenger", "dhow", "tug"];
const TRUCK_STATUSES = ["moving", "loading", "idle", "maintenance", "offline"];
const VESSEL_STATUSES = ["underway", "anchored", "moored", "dark"];
const VSIZE = { container_ship: 22, oil_tanker: 22, bulk_carrier: 20, ro_ro: 20, passenger: 24, dhow: 13, tug: 12, superyacht: 17 };
const ZONE_COLORS = { port: "#3D9CFF", free_zone: "#5ec8ff", airport: "#9d8cff", industrial: "#f0a35e", retail: "#e76fb4",
  market: "#7ddc8a", gateway: "#c9d36b", restricted: "#FF5C5C", custom: "#00ED64",
  anchorage: "#FF9F43", harbour: "#36D7F5", protected: "#1ABC9C" };
const ALERT_KINDS = ["violation", "low_fuel", "cold_chain", "speeding", "ais_gap", "ais_resumed", "close_quarters"];
const SHIP_SVG = '<svg viewBox="0 0 20 20" width="100%" height="100%"><path d="M10 1.2 L14.6 7.4 L14.6 18.6 L5.4 18.6 L5.4 7.4 Z" fill="currentColor" stroke="#001E2B" stroke-width="1.3" stroke-linejoin="round"/></svg>';
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const fmt = (n, d = 0) => (n == null || isNaN(n)) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
const pretty = (s) => (s || "").replace(/_/g, " ");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const dur = (min) => min == null ? "–" : min >= 60 ? `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, "0")}m` : `${min} min`;

// ------------------------------------------------------------------ map
const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([25.12, 55.15], 10);
const bases = {
  "Dark": L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    { maxZoom: 19, subdomains: "abcd", attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>' }),
  "Light": L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    { maxZoom: 19, subdomains: "abcd", attribution: '&copy; OpenStreetMap &copy; CARTO' }),
  "Satellite": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Tiles &copy; Esri" }),
};
bases.Dark.addTo(map);
const renderer = L.canvas({ padding: 0.3 });
const layers = {
  roads: L.layerGroup().addTo(map),
  lanes: L.layerGroup().addTo(map),
  zones: L.layerGroup().addTo(map),
  depots: L.layerGroup(),
  trucks: L.layerGroup().addTo(map),
  vessels: L.layerGroup().addTo(map),
  replay: L.layerGroup(),
  near: L.layerGroup().addTo(map),
  trail: L.layerGroup().addTo(map),
};
L.control.layers(bases, {
  "Trucks": layers.trucks, "Vessels": layers.vessels, "Truck corridors": layers.roads, "Sea lanes": layers.lanes,
  "Zones": layers.zones, "Depots": layers.depots,
}, { position: "topright" }).addTo(map);
map.pm.setGlobalOptions({ pathOptions: { color: "#00ED64", weight: 2, fillOpacity: 0.08, dashArray: "6 4" }, allowSelfIntersection: false, snappable: false });

// ------------------------------------------------------------------ state
const S = {
  items: new Map(),      // id -> {id, kind, type, status, name, m: marker, ...}   (trucks AND vessels)
  shape: null, shapeLayer: null,
  highlight: null,       // Set of ids from last search
  zones: [], routes: [], depots: [],
  zoneLayers: new Map(),
  alerts: 0, replay: null, lastQuery: null, inspector: null, view: "mql", updates: 0, tab: "search", nearPin: null,
};

// ------------------------------------------------------------------ helpers
async function api(path, body, method) {
  const opt = body ? { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : { method: method || "GET" };
  const r = await fetch(path, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    toast("Query failed", esc(msg), "critical");
    throw new Error(msg);
  }
  return r.json();
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function toast(title, body, kind = "info", onclick) {
  const box = $("#toasts");
  while (box.children.length >= 4) box.firstChild.remove();
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<b>${title}</b><span>${body}</span>`;
  el.onclick = () => { onclick && onclick(); el.remove(); };
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === "critical" ? 7000 : 5000);
}
const shipDot = (status) => `<i class="dot shipdot" style="color:${COLORS[status] || "#ccc"}">${SHIP_SVG}</i>`;
const itemDot = (kind, status) => kind === "vessel" ? shipDot(status) : `<i class="dot" style="background:${COLORS[status]}"></i>`;

// ------------------------------------------------------------------ fleet markers
function truckStyle(t) {
  const dim = S.highlight && !S.highlight.has(t.id);
  const hit = S.highlight && S.highlight.has(t.id);
  return { radius: hit ? 5.5 : 3.6, color: hit ? "#ffffff" : COLORS[t.status], weight: hit ? 1.5 : 0,
    fillColor: COLORS[t.status], fillOpacity: dim ? 0.16 : 0.95 };
}
function vesselStyle(v) {
  const el = v.m.getElement() && v.m.getElement().firstElementChild;
  if (!el) return;
  el.style.color = COLORS[v.status] || "#ccc";
  el.style.transform = `rotate(${v.heading || 0}deg)`;
  el.classList.toggle("hit", !!(S.highlight && S.highlight.has(v.id)));
  el.classList.toggle("dim", !!(S.highlight && !S.highlight.has(v.id)));
  el.classList.toggle("dark", v.status === "dark");
}
function restyle(it) { it.kind === "vessel" ? vesselStyle(it) : it.m.setStyle(truckStyle(it)); }
function restyleAll() { for (const it of S.items.values()) restyle(it); }

function upsertItem(row) {
  // [id, lng, lat, status, type, heading, speed, fuel, hazmat, kind, name]
  const [id, lng, lat, status, type, heading, speed, fuel, hazmat, kind, name] = row;
  let it = S.items.get(id);
  if (it) return it;
  it = { id, kind: kind || "truck", status, type, heading, speed, fuel, hazmat, name };
  if (it.kind === "vessel") {
    const s = VSIZE[type] || 16;
    it.m = L.marker([lat, lng], { keyboard: false, icon: L.divIcon({ className: "vsl", html: `<div class="ship" style="width:${s}px;height:${s}px">${SHIP_SVG}</div>`, iconSize: [s, s], iconAnchor: [s / 2, s / 2] }) })
      .bindTooltip(`${esc(name)} · ${pretty(type)}`, { direction: "top", offset: [0, -8] })
      .addTo(layers.vessels);
    it.m.on("add", () => vesselStyle(it));
    vesselStyle(it);
  } else {
    it.m = L.circleMarker([lat, lng], { renderer, ...truckStyle(it) }).addTo(layers.trucks);
  }
  it.m.on("click", () => openItem(id));
  S.items.set(id, it);
  return it;
}
function removeItem(id) {
  const it = S.items.get(id);
  if (!it) return;
  it.m.remove();
  S.items.delete(id);
}
async function loadFleet() {
  const rows = await api("/api/vehicles");
  rows.forEach(upsertItem);
  updateKpis();
}
function applyPositions(items) {
  if (S.replay) return;
  for (const [id, lng, lat, status, heading, speed, fuel] of items) {
    const it = S.items.get(id);
    if (!it) continue;
    const changed = status && status !== it.status;
    if (status) it.status = status;
    if (speed != null) it.speed = speed;
    if (fuel != null) it.fuel = fuel;
    it.m.setLatLng([lat, lng]);
    if (it.kind === "vessel") { if (heading != null) it.heading = heading; vesselStyle(it); }
    else if (changed) it.m.setStyle(truckStyle(it));
  }
  S.updates += items.length;
}
function updateKpis() {
  const c = {}; let trucks = 0, vessels = 0;
  for (const it of S.items.values()) {
    c[it.status] = (c[it.status] || 0) + 1;
    it.kind === "vessel" ? vessels++ : trucks++;
  }
  $("#k-total").textContent = fmt(trucks);
  $("#k-vessels").textContent = fmt(vessels);
  for (const k of [...TRUCK_STATUSES, ...VESSEL_STATUSES]) { const el = $(`#k-${k}`); if (el) el.textContent = fmt(c[k] || 0); }
  $("#k-alerts").textContent = fmt(S.alerts);
}
setInterval(updateKpis, 1500);
setInterval(() => {
  const el = $("#live-text");
  if ($("#live").classList.contains("ok")) el.textContent = `change stream live · ${fmt(S.updates / 3)} updates/s`;
  S.updates = 0;
}, 3000);

// ------------------------------------------------------------------ popups
function popupAt(ll, html) {
  return L.popup({ maxWidth: 340, autoPanPaddingTopLeft: [20, 20],
    autoPanPaddingBottomRight: [$("#inspector").classList.contains("collapsed") ? 20 : 600, 60] }).setLatLng(ll).setContent(html).openOn(map);
}
function showRaw(doc, ms) {
  showInspector({ collection: "vehicles", op: "findOne", ms: ms || 0, mql: doc,
    shell: `db.vehicles.findOne({ _id: "${doc._id}" })` }, `the full ${doc.kind || "truck"} document as stored`);
  $("#inspector").classList.remove("collapsed");
}
async function openItem(id) {
  const it = S.items.get(id);
  const res = await api(`/api/vehicles/${encodeURIComponent(id)}`);
  const v = res.results.vehicle;
  showInspector(res.inspector, `Last 30 min track for ${esc(v.name || id)} from the time-series collection`);
  layers.trail.clearLayers();
  if (res.results.trail.length > 1) {
    L.polyline(res.results.trail.map(([x, y]) => [y, x]), { color: "#ffffff", weight: 2.5, opacity: 0.8, dashArray: "4 5" }).addTo(layers.trail);
  }
  const ll = it ? it.m.getLatLng() : L.latLng(v.location.coordinates[1], v.location.coordinates[0]);
  const pop = popupAt(ll, v.kind === "vessel" ? vesselHtml(v) : truckHtml(v));
  const el = pop.getElement();
  const on = (act, fn) => { const b = el.querySelector(`[data-act="${act}"]`); if (b) b.onclick = () => { map.closePopup(); fn(); }; };
  on("raw", () => showRaw(v, res.inspector.ms));
  on("depot", () => runNear([ll.lng, ll.lat], { target: "depots", limit: 3 }));
  on("backup", () => runNear([ll.lng, ll.lat], { target: "trucks", limit: 5, filters: { kinds: ["truck"], types: [v.type], statuses: ["idle", "loading"] }, exclude: v._id }));
  on("dispatch", () => runNear([ll.lng, ll.lat], { target: "trucks", limit: 8, max_km: 30,
    filters: { kinds: ["truck"], types: v.type === "ro_ro" ? ["flatbed", "box"] : ["container", "flatbed"], statuses: ["idle", "loading"] },
    title: `Trucks to dispatch for ${v.name}` }));
  on("tug", () => runNear([ll.lng, ll.lat], { target: "vessels", limit: 3, filters: { kinds: ["vessel"], types: ["tug"] }, exclude: v._id }));
}
function truckHtml(v) {
  const fuelColor = v.fuel_pct < 15 ? "var(--danger)" : v.fuel_pct < 35 ? "var(--warn)" : "var(--accent)";
  const reefer = v.reefer_temp_c != null
    ? `<tr><td>Reefer temp</td><td><b style="color:${v.reefer_temp_c > v.reefer_setpoint_c + 3 ? "var(--danger)" : "inherit"}">${v.reefer_temp_c}°C</b> <span class="muted">(set ${v.reefer_setpoint_c}°C)</span></td></tr>` : "";
  return `<div class="pop">
    <h3>${v._id} <span class="badge" style="background:${COLORS[v.status]}">${v.status}</span> <span class="kindtag">truck</span></h3>
    <div class="sub">${esc(v.plate)} · ${esc(v.make)} · ${esc(v.company)}</div>
    <table>
      <tr><td>Type</td><td>${v.type} · ${v.capacity_t} t</td></tr>
      <tr><td>Cargo</td><td>${esc(v.cargo?.type)} (${v.cargo?.weight_t} t)${v.cargo?.hazmat ? ' <span class="badge" style="background:var(--danger)">HAZMAT</span>' : ""}</td></tr>
      <tr><td>Driver</td><td>${esc(v.driver?.name)} · ★ ${v.driver?.rating}</td></tr>
      <tr><td>Trip</td><td>${esc(v.trip?.origin_name || "–")} → <b>${esc(v.trip?.dest_name)}</b></td></tr>
      <tr><td>Speed</td><td>${fmt(v.speed_kmh)} km/h</td></tr>
      <tr><td>Fuel</td><td><div class="fuel"><i style="width:${v.fuel_pct}%;background:${fuelColor}"></i></div>${fmt(v.fuel_pct)}%</td></tr>
      ${reefer}
      <tr><td>Last update</td><td>${new Date(v.last_update).toLocaleTimeString()}</td></tr>
    </table>
    <div class="actions">
      <button class="ghost" data-act="depot">Nearest depot</button>
      <button class="ghost" data-act="backup">Backup truck</button>
      <button class="ghost" data-act="raw">{ } Document</button>
    </div></div>`;
}
function vesselHtml(v) {
  const c = v.cargo || {};
  const cargoBits = [];
  if (c.teu) cargoBits.push(`${fmt(c.teu)} TEU`);
  if (c.dwt) cargoBits.push(`${fmt(c.dwt)} DWT`);
  if (c.ceu) cargoBits.push(`${fmt(c.ceu)} car units`);
  if (c.weight_t) cargoBits.push(`${fmt(c.weight_t)} t`);
  const rows = [];
  rows.push(`<tr><td>MMSI / IMO</td><td>${v.ais?.mmsi ?? "–"} / ${v.ais?.imo ?? '<span class="muted">none (field absent)</span>'}</td></tr>`);
  rows.push(`<tr><td>AIS</td><td>class ${v.ais?.class || "–"} · ${esc(v.ais?.nav_status || "")}</td></tr>`);
  rows.push(`<tr><td>Speed / heading</td><td>${fmt(v.speed_kn, 1)} kn · ${fmt(v.heading)}°</td></tr>`);
  if (v.dimensions) rows.push(`<tr><td>Size</td><td>${v.dimensions.length_m} × ${v.dimensions.beam_m} m · draught ${v.dimensions.draught_m} m</td></tr>`);
  if (v.cargo) rows.push(`<tr><td>Cargo</td><td>${esc(c.type || "")}${cargoBits.length ? " · " + cargoBits.join(" · ") : ""}${c.hazmat ? ` <span class="badge" style="background:var(--danger)">IMDG ${(c.imdg_classes || []).join(",")}</span>` : ""}</td></tr>`);
  if (v.passengers) rows.push(`<tr><td>Passengers</td><td>${fmt(v.passengers)} + ${fmt(v.crew)} crew</td></tr>`);
  if (v.bollard_pull_t) rows.push(`<tr><td>Bollard pull</td><td>${v.bollard_pull_t} t · ${esc(v.fifi_class || "")}</td></tr>`);
  if (v.guests) rows.push(`<tr><td>Guests</td><td>${v.guests.max} in ${v.guests.cabins} cabins${v.helipad ? " · helipad" : ""}</td></tr>`);
  if (v.tenders) rows.push(`<tr><td>Tenders</td><td>${v.tenders.map((t) => esc(t.name)).join(", ")}</td></tr>`);
  const vy = v.voyage || {};
  rows.push(`<tr><td>Voyage</td><td>${esc(vy.last_port || "–")} → <b>${esc(vy.next_port || "–")}</b> <span class="muted">(${pretty(vy.phase)})</span></td></tr>`);
  if (vy.berth) rows.push(`<tr><td>Berth</td><td>${esc(vy.port || "")} · ${esc(vy.berth)}</td></tr>`);
  if (vy.eta) rows.push(`<tr><td>ETA</td><td>${new Date(vy.eta).toLocaleTimeString()}</td></tr>`);
  if (vy.anchored_since) rows.push(`<tr><td>At anchor since</td><td>${new Date(vy.anchored_since).toLocaleString()}</td></tr>`);
  const canDispatch = ["container_ship", "ro_ro", "bulk_carrier"].includes(v.type) && vy.port === "Jebel Ali" && vy.phase !== "outbound";
  return `<div class="pop">
    <h3>${esc(v.name)} <span class="badge" style="background:${COLORS[v.status]}">${v.status}</span> <span class="kindtag sea">vessel</span></h3>
    <div class="sub">${pretty(v.type)} · ${esc(v.flag || "")} flag · ${esc(v.operator || "")}</div>
    <table>${rows.join("")}</table>
    <div class="actions">
      ${canDispatch ? '<button class="ghost" data-act="dispatch">Dispatch trucks</button>' : ""}
      ${v.type !== "tug" ? '<button class="ghost" data-act="tug">Nearest tug</button>' : ""}
      <button class="ghost" data-act="raw">{ } Document</button>
    </div></div>`;
}

// ------------------------------------------------------------------ static layers
async function loadStatic() {
  const [zones, routes, depots] = await Promise.all([api("/api/zones"), api("/api/routes"), api("/api/depots")]);
  S.routes = routes; S.depots = depots;
  layers.roads.clearLayers(); layers.lanes.clearLayers();
  routes.forEach((r) => {
    if (r.kind === "sea_lane") L.geoJSON(r.geometry, { style: { color: "#36D7F5", weight: 1.5, opacity: 0.35, dashArray: "2 6" }, interactive: false }).addTo(layers.lanes);
    else L.geoJSON(r.geometry, { style: { color: "#2f8fb3", weight: 2, opacity: 0.45 }, interactive: false }).addTo(layers.roads);
  });
  layers.depots.clearLayers();
  depots.forEach((d) => {
    const [x, y] = d.location.coordinates;
    L.marker([y, x], { icon: L.divIcon({ className: "", html: `<div style="font-size:16px">${d.kind === "fuel" ? "⛽" : "🔧"}</div>`, iconSize: [18, 18] }) })
      .bindTooltip(d.name).addTo(layers.depots);
  });
  renderZones(zones);
}
function renderZones(zones) {
  S.zones = zones;
  layers.zones.clearLayers();
  S.zoneLayers.clear();
  zones.forEach((z) => {
    const c = ZONE_COLORS[z.kind] || "#aaa";
    const strong = ["restricted", "protected"].includes(z.kind);
    const lyr = L.geoJSON(z.geometry, { style: { color: c, weight: strong ? 2 : 1.2, fillOpacity: strong ? 0.12 : 0.06, dashArray: strong ? "5 5" : null } });
    lyr.bindTooltip(z.name, { className: "zone-label", sticky: true });
    lyr.on("click", (e) => { if (S.tab === "search" && !map.pm.globalDrawModeEnabled()) { L.DomEvent.stopPropagation(e); useZoneAsShape(z); } });
    lyr.addTo(layers.zones);
    S.zoneLayers.set(z._id, lyr);
  });
  renderZoneList();
}
function renderZoneList() {
  const box = $("#z-list");
  const sorted = [...S.zones].sort((a, b) => (a.kind === "custom" ? -1 : 0) - (b.kind === "custom" ? -1 : 0) || (b.inside_count || 0) - (a.inside_count || 0));
  box.innerHTML = sorted.map((z) => `<div class="z" data-id="${z._id}">
    <i style="background:${ZONE_COLORS[z.kind] || "#aaa"}"></i>
    <span>${esc(z.name)}<small> ${pretty(z.kind)}${z.rules?.applies_to ? " · " + z.rules.applies_to.join("/") + "s" : ""}</small></span>
    <b id="zc-${z._id}">${z.inside_count ?? "–"}</b>
    ${z.builtin ? "<span></span>" : `<button data-del="${z._id}" title="Delete">✕</button>`}
  </div>`).join("");
  box.querySelectorAll(".z").forEach((el) => el.onclick = (e) => {
    const del = e.target.getAttribute("data-del");
    if (del) { api(`/api/zones/${del}`, null, "DELETE"); return; }
    useZoneAsShape(S.zones.find((q) => q._id === el.dataset.id));
  });
}
function useZoneAsShape(z) {
  const lyr = L.geoJSON(z.geometry).getLayers()[0];
  setShape(lyr, "Polygon", true);
  map.fitBounds(lyr.getBounds(), { padding: [60, 60] });
  switchTab("search");
}

// ------------------------------------------------------------------ drawing
let drawMode = null;
$$("[data-draw]").forEach((b) => b.onclick = () => {
  const mode = b.dataset.draw;
  map.pm.disableDraw();
  $$("[data-draw]").forEach((x) => x.classList.remove("active"));
  if (drawMode === mode) { drawMode = null; return; }
  drawMode = mode;
  b.classList.add("active");
  $("#corridor-row").classList.toggle("hidden", mode !== "Line");
  map.pm.enableDraw(mode, { finishOn: mode === "Line" ? "dblclick" : null });
});
map.on("pm:create", (e) => {
  $$("[data-draw]").forEach((x) => x.classList.remove("active"));
  drawMode = null;
  setShape(e.layer, e.shape === "Rectangle" ? "Polygon" : e.shape, false);
});
function setShape(layer, type, fromZone) {
  if (S.shapeLayer) map.removeLayer(S.shapeLayer);
  S.shapeLayer = layer;
  if (fromZone) layer.setStyle({ color: "#00ED64", weight: 2, fillOpacity: 0.08, dashArray: "6 4" });
  layer.addTo(map);
  layer.pm.enable({ allowSelfIntersection: false });
  const rerun = debounce(() => { S.shape = shapeOf(layer, type); runSearch(); }, 250);
  layer.on("pm:edit pm:dragend pm:markerdragend", rerun);
  S.shape = shapeOf(layer, type);
  $("#corridor-row").classList.toggle("hidden", type !== "Line");
  runSearch();
}
function shapeOf(layer, type) {
  if (type === "Circle") { const c = layer.getLatLng(); return { type: "Circle", center: [c.lng, c.lat], radius: layer.getRadius() }; }
  const g = layer.toGeoJSON().geometry;
  if (type === "Line") return { type: "LineString", coordinates: g.coordinates, width: +$("#corridor-w").value };
  return { type: "Polygon", coordinates: g.coordinates };
}
$("#clear-shape").onclick = clearShape;
function clearShape() {
  map.pm.disableDraw();
  if (S.shapeLayer) map.removeLayer(S.shapeLayer);
  S.shapeLayer = null; S.shape = null; S.highlight = null;
  restyleAll();
  $("#results").classList.add("hidden");
  $("#search-hint").classList.remove("hidden");
}
$("#corridor-w").oninput = debounce(() => {
  $("#corridor-wv").textContent = `${$("#corridor-w").value} m`;
  if (S.shape && S.shape.type === "LineString") { S.shape.width = +$("#corridor-w").value; runSearch(); }
}, 150);

// ------------------------------------------------------------------ filters
function chips(el, groups, colors) {
  el.innerHTML = groups.map(([label, values]) => (label ? `<span class="chiplbl">${label}</span>` : "") +
    values.map((v) => `<span class="chip" data-v="${v}">${colors ? `<i style="background:${colors[v]}"></i>` : ""}${pretty(v)}</span>`).join("")).join("");
  el.querySelectorAll(".chip").forEach((c) => c.onclick = () => { c.classList.toggle("on"); if (S.shape) runSearch(); });
}
chips($("#f-kinds"), [["", ["truck", "vessel"]]]);
chips($("#f-types"), [["Road", TRUCK_TYPES], ["Sea", VESSEL_TYPES]]);
chips($("#f-status"), [["Road", TRUCK_STATUSES], ["Sea", VESSEL_STATUSES]], COLORS);
function filters() {
  const on = (sel) => [...document.querySelectorAll(`${sel} .chip.on`)].map((c) => c.dataset.v);
  const f = { kinds: on("#f-kinds"), types: on("#f-types"), statuses: on("#f-status") };
  const fuel = +$("#f-fuel").value;
  if (fuel < 100) f.max_fuel = fuel;
  if ($("#f-hazmat").checked) f.hazmat = true;
  return f;
}
function setFilters({ kinds = [], types = [], statuses = [], max_fuel = 100, hazmat = false } = {}) {
  $$("#f-kinds .chip").forEach((c) => c.classList.toggle("on", kinds.includes(c.dataset.v)));
  $$("#f-types .chip").forEach((c) => c.classList.toggle("on", types.includes(c.dataset.v)));
  $$("#f-status .chip").forEach((c) => c.classList.toggle("on", statuses.includes(c.dataset.v)));
  $("#f-fuel").value = max_fuel; $("#f-fuelv").textContent = max_fuel < 100 ? `${max_fuel}%` : "any";
  $("#f-hazmat").checked = hazmat;
}
$("#f-fuel").oninput = debounce(() => { const v = +$("#f-fuel").value; $("#f-fuelv").textContent = v < 100 ? `${v}%` : "any"; if (S.shape) runSearch(); }, 150);
$("#f-hazmat").onchange = () => S.shape && runSearch();

// ------------------------------------------------------------------ search
let searching = false;
async function runSearch(silent) {
  if (!S.shape || searching || S.replay) return;
  searching = true;
  try {
    const payload = { shape: S.shape, filters: filters() };
    const [res, touch] = await Promise.all([
      api("/api/search/within", payload),
      silent ? Promise.resolve(null) : api("/api/search/intersects", { shape: S.shape }).catch(() => null),
    ]);
    S.lastQuery = { kind: "within", payload };
    const r = res.results;
    S.highlight = new Set(r.vehicles.map((v) => v._id));
    restyleAll();
    renderResults(r, res.inspector.ms);
    if (touch) renderTouch(touch.results);
    showInspector(res.inspector, `${fmt(r.summary.trucks || 0)} trucks + ${fmt(r.summary.vessels || 0)} vessels from one collection`);
  } catch (e) { console.warn(e); }
  finally { searching = false; }
}
setInterval(() => { if ($("#f-live").checked && S.shape && S.tab === "search" && !document.hidden) runSearch(true); }, 3000);

function renderResults(r, ms) {
  $("#results").classList.remove("hidden");
  $("#search-hint").classList.add("hidden");
  const s = r.summary;
  $("#r-count").textContent = fmt(s.count);
  $("#r-trucks").textContent = fmt(s.trucks || 0);
  $("#r-vessels").textContent = fmt(s.vessels || 0);
  $("#r-teu").textContent = fmt(s.teu || 0);
  $("#r-tonnes").textContent = fmt(s.road_tonnes || 0);
  $("#r-low").textContent = fmt(s.low_fuel || 0);
  $("#r-haz").textContent = fmt(s.hazmat || 0);
  $("#r-ms").textContent = fmt(ms, 0);
  const max = Math.max(1, ...r.by_type.map((x) => x.count));
  $("#r-types").innerHTML = r.by_type.map((x) => `<div class="bar"><span>${pretty(x._id)}</span><div><i class="${VESSEL_TYPES.includes(x._id) ? "sea" : ""}" style="width:${x.count / max * 100}%"></i></div><b>${x.count}</b></div>`).join("");
  // vessels first: they are few, and would otherwise sit behind hundreds of trucks
  const list = [...r.vehicles].sort((a, b) => (a.kind === "vessel" ? 0 : 1) - (b.kind === "vessel" ? 0 : 1)).slice(0, 150);
  $("#r-list").innerHTML = list.map((v) => v.kind === "vessel"
    ? `<div class="item" data-id="${v._id}">${shipDot(v.status)}
        <div><b>${esc(v.name)}</b> <span class="muted">${pretty(v.type)}</span><small class="muted">→ ${esc(v.voyage?.next_port || "")} · MMSI ${v.ais?.mmsi || "–"}</small></div>
        <div class="right">${fmt(v.speed_kn, 1)} kn<small class="muted">${v.cargo?.teu ? fmt(v.cargo.teu) + " TEU" : pretty(v.voyage?.phase)}</small></div></div>`
    : `<div class="item" data-id="${v._id}"><i class="dot" style="background:${COLORS[v.status]}"></i>
        <div><b>${v._id}</b> <span class="muted">${v.type}</span><small class="muted">${esc(v.cargo?.type || "")} → ${esc(v.trip?.dest_name || "")}</small></div>
        <div class="right">${fmt(v.speed_kmh)} km/h<small class="muted">fuel ${fmt(v.fuel_pct)}%</small></div></div>`).join("")
    + (r.vehicles.length > list.length ? `<div class="more">+ ${fmt(r.vehicles.length - list.length)} more on the map</div>` : "");
  $$("#r-list .item").forEach((el) => el.onclick = () => focusItem(el.dataset.id));
}
function renderTouch(t) {
  const z = t.zones.map((x) => `<span class="tag">${esc(x.name)}</span>`).join("") || "none";
  const r = t.routes.map((x) => `<span class="tag ${x.kind === "sea_lane" ? "sea" : ""}">${esc(x.name)}</span>`).join("") || "none";
  $("#r-touch").innerHTML = `<b style="color:var(--text)">$geoIntersects</b> zones: ${z}<br>roads &amp; sea lanes: ${r}`;
}
function focusItem(id) {
  const it = S.items.get(id);
  if (!it) return;
  const z = Math.max(map.getZoom(), 14), size = map.getSize();
  const c = map.project(it.m.getLatLng(), z).add([size.x * 0.18, -size.y * 0.05]);
  map.setView(map.unproject(c, z), z);
  openItem(id);
}

// ------------------------------------------------------------------ scenarios
$$("[data-scn]").forEach((b) => b.onclick = () => {
  const k = b.dataset.scn;
  const zone = (id) => S.zones.find((z) => z._id === id);
  const circle = (lat, lng, r) => { const c = L.circle([lat, lng], { radius: r }); setShape(c, "Circle", true); map.fitBounds(c.getBounds(), { padding: [40, 40] }); };
  const corridor = (lanes, w, f) => {
    const coords = [];
    lanes.forEach((name) => { const r = S.routes.find((x) => x.name === name); if (r) r.geometry.coordinates.forEach((p) => { const last = coords[coords.length - 1]; if (!last || last[0] !== p[0] || last[1] !== p[1]) coords.push(p); }); });
    setFilters(f);
    $("#corridor-w").value = w; $("#corridor-wv").textContent = `${w} m`;
    const line = L.polyline(coords.map(([x, y]) => [y, x]));
    setShape(line, "Line", true);
    map.fitBounds(line.getBounds(), { padding: [40, 40] });
  };
  if (k === "reefer-port") { setFilters({ types: ["reefer"] }); useZoneAsShape(zone("hub-jap")); }
  else if (k === "hazmat-dxb") { setFilters({ hazmat: true }); circle(25.262, 55.350, 5000); }
  else if (k === "lowfuel-e311") { corridor(["E311 Sheikh Mohammed Bin Zayed Rd"], 1200, { max_fuel: 25 }); }
  else if (k === "ban") { setFilters({ types: ["container", "reefer", "flatbed", "tanker", "tipper"], statuses: ["moving"] }); useZoneAsShape(zone("restricted-downtown")); }
  else if (k === "port-picture") { setFilters({}); circle(25.022, 55.050, 6500); }
  else if (k === "anchorage") { setFilters({ kinds: ["vessel"], statuses: ["anchored"] }); useZoneAsShape(zone("sea-ja-anchorage")); }
  else if (k === "hazmat-landsea") { setFilters({ hazmat: true }); circle(25.030, 55.040, 16000); }
  else if (k === "dhows") { corridor(["Dubai Creek Approach", "Hormuz Lane"], 3000, { types: ["dhow"] }); }
});

// ------------------------------------------------------------------ nearest
const NEAR_OPTS = {
  trucks: { types: TRUCK_TYPES, statuses: [["idle,loading", "Idle or loading (free soon)"], ["moving", "Moving"], ["", "Any"]] },
  vessels: { types: VESSEL_TYPES, statuses: [["", "Any"], ["underway", "Under way"], ["anchored", "At anchor"], ["moored", "Moored"]] },
};
function fillNearSelects() {
  const t = $("#n-target").value, o = NEAR_OPTS[t];
  $("#n-type-row").classList.toggle("hidden", !o); $("#n-status-row").classList.toggle("hidden", !o);
  if (!o) return;
  $("#n-type").innerHTML = `<option value="">Any type</option>` + o.types.map((x) => `<option value="${x}">${pretty(x)}</option>`).join("");
  $("#n-status").innerHTML = o.statuses.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
}
fillNearSelects();
map.on("click", (e) => {
  if (S.tab !== "near" || map.pm.globalDrawModeEnabled()) return;
  runNear([e.latlng.lng, e.latlng.lat]);
});
$("#n-target").onchange = () => { fillNearSelects(); if (S.nearPin) runNear(S.nearPin); };
["n-type", "n-status"].forEach((id) => $(`#${id}`).onchange = () => S.nearPin && runNear(S.nearPin));
$("#n-limit").oninput = () => { $("#n-limitv").textContent = $("#n-limit").value; };
$("#n-max").oninput = () => { $("#n-maxv").textContent = `${$("#n-max").value} km`; };
$("#n-limit").onchange = $("#n-max").onchange = () => S.nearPin && runNear(S.nearPin);

async function runNear(point, opts = {}) {
  S.nearPin = point;
  switchTab("near");
  const sel = opts.target || $("#n-target").value;
  const target = sel === "depots" ? "depots" : "vehicles";
  let f = opts.filters;
  if (!f && target === "vehicles") {
    f = { kinds: [sel === "vessels" ? "vessel" : "truck"] };
    if ($("#n-type").value) f.types = [$("#n-type").value];
    if ($("#n-status").value) f.statuses = $("#n-status").value.split(",");
  }
  const limit = (opts.limit || +$("#n-limit").value) + (opts.exclude ? 1 : 0);
  const body = { point, target, limit, filters: f || {}, max_distance_m: (opts.max_km || +$("#n-max").value) * 1000 };
  const res = await api("/api/search/near", body);
  const rows = res.results.filter((r) => r._id !== opts.exclude);
  S.lastQuery = { kind: "near", payload: body };
  showInspector(res.inspector, opts.title || `$geoNear returned ${rows.length} results sorted by distance`);
  layers.near.clearLayers();
  const pin = L.latLng(point[1], point[0]);
  L.marker(pin, { icon: L.divIcon({ className: "", html: '<div class="pin"></div>', iconSize: [22, 22], iconAnchor: [11, 22] }) }).addTo(layers.near);
  rows.forEach((r, i) => {
    const [x, y] = r.location.coordinates;
    const ll = L.latLng(y, x);
    L.polyline([pin, ll], { color: i === 0 ? "#00ED64" : "#7fb8ff", weight: i === 0 ? 3 : 1.5, opacity: 0.9, dashArray: i === 0 ? null : "4 4" }).addTo(layers.near);
    L.circleMarker(ll, { radius: 7, color: "#fff", weight: 2, fillColor: target === "depots" ? "#FFC010" : COLORS[r.status], fillOpacity: 1 })
      .bindTooltip(`${i + 1}. ${(r.distance_m / 1000).toFixed(1)} km`, { permanent: true, direction: "right", className: "dist-label" })
      .on("click", () => target === "vehicles" && openItem(r._id))
      .addTo(layers.near);
  });
  if (rows.length) map.fitBounds(L.latLngBounds([pin, ...rows.map((r) => [r.location.coordinates[1], r.location.coordinates[0]])]), { padding: [80, 80], maxZoom: 14 });
  $("#n-title").textContent = opts.title || "";
  $("#n-list").innerHTML = rows.map((r, i) => target === "depots"
    ? `<div class="item"><i class="dot" style="background:#FFC010"></i><div><b>${i + 1}. ${esc(r.name)}</b><small class="muted">${r.kind}</small></div>
        <div class="right"><b>${(r.distance_m / 1000).toFixed(1)} km</b></div></div>`
    : r.kind === "vessel"
    ? `<div class="item" data-id="${r._id}">${shipDot(r.status)}
        <div><b>${i + 1}. ${esc(r.name)}</b> <span class="muted">${pretty(r.type)}</span><small class="muted">${esc(r.operator || "")} · ${r.status}</small></div>
        <div class="right"><b>${(r.distance_m / 1000).toFixed(1)} km</b><small class="muted">${fmt(r.distance_m / 1852, 1)} nm</small></div></div>`
    : `<div class="item" data-id="${r._id}"><i class="dot" style="background:${COLORS[r.status]}"></i>
        <div><b>${i + 1}. ${r._id}</b> <span class="muted">${r.type}</span><small class="muted">${esc(r.driver?.name || "")} · ${esc(r.company || "")}</small></div>
        <div class="right"><b>${(r.distance_m / 1000).toFixed(1)} km</b><small class="muted">~${fmt(r.eta_min)} min</small></div></div>`).join("")
    || `<p class="hint">Nothing within ${body.max_distance_m / 1000} km. Widen the max distance.</p>`;
  $$("#n-list .item[data-id]").forEach((el) => el.onclick = () => focusItem(el.dataset.id));
}

// ------------------------------------------------------------------ geofences + events
$("#z-save").onclick = async () => {
  if (!S.shape || S.shape.type === "LineString") { toast("Draw a shape first", "Use polygon, box or radius on the Search tab, then save it here.", "warning"); return; }
  const name = $("#z-name").value.trim() || `Geofence ${new Date().toLocaleTimeString()}`;
  const res = await api("/api/zones", { name, shape: S.shape });
  showInspector(res.inspector, `Geofence saved. The simulator picks it up within a few ticks`);
  $("#z-name").value = "";
  toast("Geofence created", `${esc(name)}: you'll get an alert when trucks or vessels enter or leave.`, "ok");
};
const EVENT_TITLES = {
  low_fuel: "⛽ Low fuel reroute", cold_chain: "❄️ Cold-chain alert", speeding: "🚤 Harbour speeding",
  ais_gap: "📡 Vessel went dark", ais_resumed: "📡 AIS signal restored", close_quarters: "⚠️ Close quarters",
};
function eventTitle(e) {
  if (e.kind === "violation") return e.vehicle_kind === "vessel" ? "🛑 Protected-zone violation" : "🚫 Truck-ban violation";
  return EVENT_TITLES[e.kind] || (e.kind === "enter" ? `➜ Entered ${esc(e.zone_name)}` : `← Left ${esc(e.zone_name)}`);
}
function feedEl(e) {
  const el = document.createElement("div");
  el.className = `ev ${e.severity}`;
  el.innerHTML = `<span class="evk">${e.vehicle_kind === "vessel" ? "⛴" : "🚚"}</span>${esc(e.message)}<small class="muted">${new Date(e.ts).toLocaleTimeString()} · ${esc(e.plate || e.name || e.vehicle_id)}</small>`;
  el.onclick = () => focusItem(e.vehicle_id);
  return el;
}
function addEvent(e, live) {
  const isAlert = ALERT_KINDS.includes(e.kind);
  const isCustom = e.zone_kind === "custom";
  if (isAlert && live && e.kind !== "ais_resumed") S.alerts++;
  const push = (feed) => { live ? feed.prepend(feedEl(e)) : feed.appendChild(feedEl(e)); while (feed.children.length > 80) feed.lastChild.remove(); };
  if (isAlert || isCustom || $("#e-all").checked) push($("#e-list"));
  if (e.vehicle_kind === "vessel" && (isAlert || ["anchorage"].includes(e.zone_kind))) push($("#m-feed"));
  if (live && (isAlert || isCustom)) {
    throttledToast(eventTitle(e), esc(e.message), e.severity === "info" ? "ok" : e.severity, () => focusItem(e.vehicle_id));
  }
}
let lastToast = 0, suppressed = 0;
function throttledToast(t, b, k, fn) {
  const now = Date.now();
  if (now - lastToast < 1500) { suppressed++; return; }
  lastToast = now;
  toast(t, suppressed ? `${b}<br><span class="muted">+${suppressed} more in the feed</span>` : b, k, fn);
  suppressed = 0;
}
async function loadEvents() {
  const rows = await api("/api/events?limit=80");
  $("#e-list").innerHTML = ""; $("#m-feed").innerHTML = "";
  rows.forEach((e) => addEvent(e, false));
}
$("#e-all").onchange = loadEvents;

// ------------------------------------------------------------------ maritime tab
async function loadMaritime() {
  const res = await api("/api/maritime");
  const r = res.results;
  const st = Object.fromEntries((r.by_status || []).map((x) => [x._id, x.n]));
  $("#m-underway").textContent = fmt(st.underway || 0);
  $("#m-anchored").textContent = fmt(st.anchored || 0);
  $("#m-moored").textContent = fmt(st.moored || 0);
  $("#m-dark").textContent = fmt(st.dark || 0);
  const waits = (r.anchorage || []).map((a) => a.wait_min).filter((x) => x != null);
  $("#m-wait").textContent = waits.length ? dur(Math.round(waits.reduce((a, b) => a + b, 0) / waits.length)) : "–";
  $("#m-teu").textContent = fmt(r.cargo?.teu_inbound || 0);
  const row = (v, right, sub) => `<div class="item" data-id="${v._id}">${shipDot(v.status || "anchored")}
      <div><b>${esc(v.name)}</b> <span class="muted">${pretty(v.type)}</span><small class="muted">${sub}</small></div>
      <div class="right">${right}</div></div>`;
  $("#m-anch").innerHTML = (r.anchorage || []).map((v) => row({ ...v, status: "anchored" }, `<b>${dur(v.wait_min)}</b><small class="muted">waiting</small>`,
    `from ${esc(v.voyage?.last_port || "–")} · ${v.dimensions?.length_m || "?"} m`)).join("") || '<p class="hint">No vessels waiting.</p>';
  $("#m-in").innerHTML = (r.inbound || []).map((v) => row(v, `<b>${v.eta_min == null ? "–" : v.eta_min <= 0 ? "arriving" : dur(v.eta_min)}</b><small class="muted">ETA</small>`,
    `${esc(v.voyage?.last_port || "")} → ${esc(v.voyage?.port || "")} · ${pretty(v.voyage?.phase)}`)).join("") || '<p class="hint">No inbound vessels.</p>';
  $("#m-berths").innerHTML = (r.berths || []).map((b) => `<div class="berth"><b>${esc(b._id || "–")}</b><span>${b.n} alongside</span>
      <div>${b.vessels.map((v) => `<span class="tag sea" data-id="${v._id}">${esc(v.name)}</span>`).join("")}</div></div>`).join("");
  $("#m-darklist").innerHTML = (r.dark || []).map((v) => row({ ...v, status: "dark" }, `<b>${fmt(v.silent_s)}s</b><small class="muted">silent</small>`, "no AIS position")).join("");
  $$("#tab-sea .item[data-id], #tab-sea .tag[data-id]").forEach((el) => el.onclick = () => focusItem(el.dataset.id));
  return res;
}
let seaTimer = null;
$("#m-query").onclick = async () => { const res = await loadMaritime(); showInspector(res.inspector, "port picture: one $facet over vessel-only fields"); $("#inspector").classList.remove("collapsed"); };
$("#m-anchzone").onclick = () => { setFilters({ kinds: ["vessel"] }); useZoneAsShape(S.zones.find((z) => z._id === "sea-ja-anchorage")); };
$("#m-vessels").onclick = () => { setFilters({ kinds: ["vessel"] }); switchTab("search"); if (S.shape) runSearch(); else toast("Vessels only", "Filter set. Draw an area on the map to search.", "ok"); };

// ------------------------------------------------------------------ document model tab
async function loadModel() {
  const [fres, gov] = await Promise.all([api("/api/model/fields"), api("/api/model/governance")]);
  const r = fres.results;
  S.modelInspector = fres.inspector;
  const totals = Object.fromEntries((r.totals || []).map((t) => [t._id || "untyped", t.n]));
  $("#d-counts").innerHTML = `<div class="cards c2">
      <div class="card"><b>${fmt(totals.truck || 0)}</b><span>kind: "truck"</span></div>
      <div class="card sea"><b>${fmt(totals.vessel || 0)}</b><span>kind: "vessel"</span></div></div>`;
  const maxT = Math.max(1, ...(r.types || []).map((t) => t.n));
  $("#d-types").innerHTML = (r.types || []).map((t) => `<div class="bar"><span>${pretty(t._id.type)}</span><div><i class="${t._id.kind === "vessel" ? "sea" : ""}" style="width:${t.n / maxT * 100}%"></i></div><b>${t.n}</b></div>`).join("");
  // field coverage matrix
  const nT = totals.truck || 0, nV = totals.vessel || 0;
  const rows = (r.fields || []).map((f) => {
    const t = (f.kinds.find((k) => k.k === "truck") || {}).n || 0;
    const v = (f.kinds.find((k) => k.k === "vessel") || {}).n || 0;
    const pt = nT ? t / nT : 0, pv = nV ? v / nV : 0;
    const cls = pt > 0 && pv > 0 ? "shared" : pt > 0 ? "truck" : "vessel";
    return { path: f._id, pt, pv, cls, optional: (pt > 0 && pt < 0.999) || (pv > 0 && pv < 0.999) };
  }).filter((x) => x.path !== "_id");
  const order = { shared: 0, truck: 1, vessel: 2 };
  rows.sort((a, b) => order[a.cls] - order[b.cls] || a.path.localeCompare(b.path));
  const cell = (p, cls) => p === 0 ? `<td class="none">–</td>` : `<td><div class="cov ${cls}"><i style="width:${p * 100}%"></i></div><span>${Math.round(p * 100)}%</span></td>`;
  const counts = { shared: 0, truck: 0, vessel: 0 };
  rows.forEach((x) => counts[x.cls]++);
  $("#d-legend").innerHTML = `<span class="lg shared">${counts.shared} shared</span><span class="lg truck">${counts.truck} truck-only</span><span class="lg vessel">${counts.vessel} vessel-only</span><span class="lg opt">${rows.filter((x) => x.optional).length} optional</span>`;
  $("#d-fields").innerHTML = `<table><thead><tr><th>field path</th><th>trucks</th><th>vessels</th></tr></thead><tbody>` +
    rows.map((x) => `<tr class="${x.cls}"><td><code>${esc(x.path)}</code>${x.optional ? ' <span class="opt">optional</span>' : ""}</td>${cell(x.pt, "t")}${cell(x.pv, "v")}</tr>`).join("") + `</tbody></table>`;
  // governance
  const js = gov.validator && gov.validator.$jsonSchema;
  $("#d-validator").innerHTML = js
    ? `<div class="gov ok">✔ <b>$jsonSchema validator active</b> <span class="muted">(${gov.validationLevel} / ${gov.validationAction})</span>
        <ul><li>every document: <code>${js.required.join("</code>, <code>")}</code></li>
        ${js.oneOf.map((o) => `<li><code>kind: "${o.properties.kind.enum[0]}"</code> also needs <code>${o.required.join("</code>, <code>")}</code>${o.properties.ais ? " (with <code>ais.mmsi</code>)" : ""}</li>`).join("")}
        <li>everything else is free to vary per document</li></ul></div>`
    : `<div class="gov warn">⚠ No validator on this cluster (the database user may lack collMod permission). Flexibility still works; governance demo won't.</div>`;
  $("#d-indexes").innerHTML = gov.indexes.map((i) => `<div class="idx"><code>${esc(i.name)}</code>
      <small>${i.key.map(([k, d]) => `${k}: ${d}`).join(", ")}${i.unique ? " · unique" : ""}${i.partialFilterExpression ? ` · <b>partial</b> ${esc(JSON.stringify(i.partialFilterExpression))}` : ""}</small></div>`).join("");
  S.validator = gov.validator;
  return fres;
}
$("#d-matrix-q").onclick = async () => { const res = await loadModel(); showInspector(res.inspector, "schema analysis with $objectToArray + $reduce, in one aggregation"); $("#inspector").classList.remove("collapsed"); };
$("#d-schema").onclick = () => {
  if (!S.validator) { toast("No validator", "The schema validator is not active on this cluster.", "warning"); return; }
  showInspector({ collection: "vehicles", op: "collMod", ms: 0, mql: { collMod: "vehicles", validator: S.validator, validationLevel: "moderate", validationAction: "error" },
    shell: `db.runCommand(${JSON.stringify({ collMod: "vehicles", validator: S.validator, validationLevel: "moderate" }, null, 2)})` }, "the rules: shared core + per-kind minimum");
  $("#inspector").classList.remove("collapsed");
};
async function tryInsert(variant) {
  const res = await api("/api/model/insert", { variant });
  const r = res.results;
  if (variant === "yacht" && r.ok) {
    toast("✔ Inserted a superyacht", "New type, new fields (guests, helipad, tenders[]): no migration, no downtime. It's on the map now.", "ok");
    map.setView([25.118, 55.092], 13);
    showInspector(res.inspector, "a brand-new vessel type with fields no other document has");
  } else if (variant === "invalid" && !r.ok && r.errInfo) {
    toast("✖ Rejected by the validator", "A vessel without an AIS MMSI is not allowed. The flexible schema still has rules.", "critical");
    showInspector({ ...res.inspector, mql: { attempted: res.inspector.mql, error: r.error, errInfo: r.errInfo } }, "Document failed validation (code 121)");
  } else {
    toast(r.ok ? "Done" : "Note", esc(r.error || "ok"), r.ok ? "ok" : "warning");
    showInspector(res.inspector, r.error || "");
  }
  $("#inspector").classList.remove("collapsed");
}
$("#d-yacht").onclick = () => tryInsert("yacht");
$("#d-invalid").onclick = () => tryInsert("invalid");
$("#d-unyacht").onclick = async () => { const r = await api("/api/model/yacht", null, "DELETE"); toast("Removed", `${r.deleted} superyacht document(s) deleted`, "ok"); };

// side-by-side comparison modal
function fillCompareSelects() {
  $("#c-left").innerHTML = TRUCK_TYPES.map((t) => `<option value="truck|${t}">truck · ${t}</option>`).join("") + VESSEL_TYPES.map((t) => `<option value="vessel|${t}">vessel · ${pretty(t)}</option>`).join("");
  $("#c-right").innerHTML = $("#c-left").innerHTML;
  $("#c-left").value = "truck|reefer"; $("#c-right").value = "vessel|container_ship";
}
fillCompareSelects();
function renderDoc(doc, shared) {
  if (!doc) return '<span class="muted">no document of this type</span>';
  const lines = Object.entries(doc).map(([k, v]) => {
    let val = JSON.stringify(v, null, 2).replace(/\n/g, "\n  ");
    val = val.replace(/\[\s+(-?\d+\.?\d*),\s+(-?\d+\.?\d*)\s+\]/g, "[$1, $2]");
    return `  <span class="dk ${shared.has(k) ? "shared" : "own"}">"${esc(k)}"</span>: ${highlightJSON(val)}`;
  });
  return `{\n${lines.join(",\n")}\n}`;
}
async function loadCompare() {
  const [lk, lt] = $("#c-left").value.split("|"), [rk, rt] = $("#c-right").value.split("|");
  const [a, b] = await Promise.all([api(`/api/model/sample?kind=${lk}&type=${lt}`), api(`/api/model/sample?kind=${rk}&type=${rt}`)]);
  const A = a.results, B = b.results;
  const ka = new Set(Object.keys(A || {})), kb = new Set(Object.keys(B || {}));
  const shared = new Set([...ka].filter((k) => kb.has(k)));
  $("#c-a").innerHTML = renderDoc(A, shared);
  $("#c-b").innerHTML = renderDoc(B, shared);
  $("#c-stats").innerHTML = `<span class="lg shared">${shared.size} shared top-level fields</span><span class="lg own">${ka.size - shared.size} only left</span><span class="lg own">${kb.size - shared.size} only right</span><span class="muted">same collection · same indexes · same queries</span>`;
}
$("#d-compare").onclick = () => { $("#modal").classList.remove("hidden"); loadCompare(); };
$("#c-left").onchange = $("#c-right").onchange = loadCompare;
$("#c-shuffle").onclick = loadCompare;
$("#c-close").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

// ------------------------------------------------------------------ heatmap
let heat = null;
$("#h-run").onclick = async () => {
  const body = { minutes: +$("#h-min").value, metric: $("#h-metric").value, kind: $("#h-kind").value };
  if ($("#h-shape").checked && S.shape) body.shape = S.shape;
  const res = await api("/api/heatmap", body);
  const pts = res.results;
  if (heat) map.removeLayer(heat);
  const max = Math.max(1, ...pts.slice(0, 50).map((p) => p[2])) * 0.6;
  heat = L.heatLayer(pts, { radius: 14, blur: 16, max, minOpacity: 0.25,
    gradient: body.metric === "slow" ? { 0.3: "#ffc010", 0.7: "#ff7a3d", 1: "#ff2d2d" } : { 0.2: "#023b5a", 0.45: "#0a8f8f", 0.7: "#00ED64", 1: "#eaff7a" } }).addTo(map);
  showInspector(res.inspector, `${fmt(pts.length)} grid cells from the telemetry time-series collection`);
  if (!pts.length) toast("No telemetry yet", "The simulator writes a point every ~10 s. Give it a minute.", "warning");
};
$("#h-clear").onclick = () => { if (heat) map.removeLayer(heat); heat = null; };

// ------------------------------------------------------------------ replay
$("#p-load").onclick = async () => {
  const body = { minutes: +$("#p-min").value };
  if (S.shape && S.shape.type !== "LineString") body.shape = S.shape;
  const res = await api("/api/replay", body);
  const frames = res.results;
  showInspector(res.inspector, `${frames.length} frames of ${res.step}s from telemetry`);
  if (frames.length < 2) { toast("Not enough history yet", "Let the simulator run a few minutes, then try again.", "warning"); return; }
  map.closePopup();
  S.replay = { frames, i: 0, markers: new Map(), timer: null };
  map.removeLayer(layers.trucks); map.removeLayer(layers.vessels);
  layers.replay.clearLayers().addTo(map);
  $("#p-ctrl").classList.remove("hidden");
  $("#p-slider").max = frames.length - 1; $("#p-slider").value = 0;
  $("#p-info").textContent = `${frames.length} frames · ${res.step}s step`;
  drawFrame(0);
};
function drawFrame(i) {
  const R = S.replay; if (!R) return;
  R.i = i;
  const f = R.frames[i];
  const seen = new Set();
  for (const [id, x, y, st, k] of f.p) {
    seen.add(id);
    let m = R.markers.get(id);
    if (!m) {
      const sea = k === "vessel";
      m = L.circleMarker([y, x], { renderer, radius: sea ? 5.5 : 3.6, weight: sea ? 1.5 : 0, color: "#fff", fillOpacity: 0.95, fillColor: COLORS[st] || COLORS.moving }).addTo(layers.replay);
      R.markers.set(id, m);
    } else { m.setLatLng([y, x]); if (!map.hasLayer(m)) m.addTo(layers.replay); }
  }
  for (const [id, m] of R.markers) if (!seen.has(id)) layers.replay.removeLayer(m);
  $("#p-time").textContent = new Date(f.t).toLocaleTimeString();
  $("#p-slider").value = i;
}
$("#p-slider").oninput = () => drawFrame(+$("#p-slider").value);
$("#p-play").onclick = () => {
  const R = S.replay; if (!R) return;
  if (R.timer) { clearInterval(R.timer); R.timer = null; $("#p-play").textContent = "▶"; return; }
  $("#p-play").textContent = "❚❚";
  R.timer = setInterval(() => { drawFrame((R.i + 1) % R.frames.length); }, 250);
};
$("#p-exit").onclick = exitReplay;
function exitReplay() {
  if (!S.replay) return;
  clearInterval(S.replay.timer);
  S.replay = null;
  map.removeLayer(layers.replay);
  layers.trucks.addTo(map); layers.vessels.addTo(map);
  $("#p-ctrl").classList.add("hidden");
  $("#p-play").textContent = "▶";
}

// ------------------------------------------------------------------ inspector
function highlightJSON(txt) {
  return txt.replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/"(\$[a-zA-Z]+)"(?=\s*:)/g, '<span class="op">"$1"</span>')
    .replace(/"([^"$][^"]*)"(?=\s*:)/g, '<span class="k">"$1"</span>')
    .replace(/:\s*"([^"]*)"/g, ': <span class="s">"$1"</span>')
    .replace(/(ISODate\("[^"]+"\))/g, '<span class="s">$1</span>')
    .replace(/([:\[,]\s*)(-?\d+\.?\d*)/g, '$1<span class="n">$2</span>');
}
function compactJSON(obj) {
  return JSON.stringify(obj, null, 2).replace(/\[\s+(-?\d+\.?\d*),\s+(-?\d+\.?\d*)\s+\]/g, "[$1, $2]");
}
function showInspector(ins, summary) {
  S.inspector = ins;
  $("#i-sum").innerHTML = `<b style="color:var(--text)">db.${ins.collection}.${ins.op}()</b> · ${fmt(ins.ms, 1)} ms · ${summary || ""}`;
  $("#i-explain-out").classList.add("hidden");
  renderCode();
}
function renderCode() {
  const ins = S.inspector; if (!ins) return;
  const txt = S.view === "shell" ? ins.shell.replace(/\[\s+(-?\d+\.?\d*),\s+(-?\d+\.?\d*)\s+\]/g, "[$1, $2]") : compactJSON(ins.mql);
  $("#i-code").innerHTML = highlightJSON(txt);
}
$$("#inspector .seg button").forEach((b) => b.onclick = () => {
  $$("#inspector .seg button").forEach((x) => x.classList.remove("active")); b.classList.add("active");
  S.view = b.dataset.view; renderCode();
});
$("#i-toggle").onclick = () => $("#inspector").classList.toggle("collapsed");
$("#inspector header b").onclick = () => $("#inspector").classList.toggle("collapsed");
$("#i-copy").onclick = async () => {
  if (!S.inspector) return;
  try { await navigator.clipboard.writeText(S.view === "shell" ? S.inspector.shell : JSON.stringify(S.inspector.mql, null, 2)); toast("Copied", S.view === "shell" ? "Paste into mongosh or Compass" : "MQL copied as JSON", "ok"); } catch (_) {}
};
$("#i-explain").onclick = async () => {
  if (!S.shape) { toast("Explain needs a drawn shape", "Draw a search area first. Explain compares the 2dsphere index against a collection scan.", "warning"); return; }
  const res = await api("/api/explain", { shape: S.shape, filters: filters() });
  const a = res.results.with_index, b = res.results.collection_scan;
  const card = (x, title, cls) => `<div class="ex ${cls}"><h4>${title}</h4><div class="st">${x.stages.join(" → ")}${x.indexes.length ? `<br>index: ${x.indexes.join(", ")}` : ""}</div>
    <table><tr><td>Keys examined</td><td>${fmt(x.keys_examined)}</td></tr><tr><td>Docs examined</td><td>${fmt(x.docs_examined)}</td></tr>
    <tr><td>Returned</td><td>${fmt(x.returned)}</td></tr><tr><td>Server time</td><td>${fmt(x.ms)} ms</td></tr></table></div>`;
  $("#i-explain-out").innerHTML = card(a, "With 2dsphere index", "good") + card(b, "Forced collection scan", "bad");
  $("#i-explain-out").classList.remove("hidden");
  $("#inspector").classList.remove("collapsed");
  S.inspector = { ...res.inspector, ms: a.ms };
  renderCode();
  $("#i-sum").innerHTML = `<b style="color:var(--text)">explain()</b> · docs examined ${fmt(a.docs_examined)} vs ${fmt(b.docs_examined)}`;
};

// ------------------------------------------------------------------ tabs
function switchTab(name) {
  S.tab = name;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  map.getContainer().style.cursor = name === "near" ? "crosshair" : "";
  clearInterval(seaTimer);
  if (name === "sea") { loadMaritime(); seaTimer = setInterval(() => !document.hidden && loadMaritime(), 5000); }
  if (name === "model") loadModel();
}
$$(".tabs button").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));

// ------------------------------------------------------------------ websocket
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "positions") applyPositions(msg.items);
    else if (msg.type === "event") addEvent(msg.event, true);
    else if (msg.type === "vehicle_added") { upsertItem(msg.row); updateKpis(); }
    else if (msg.type === "vehicle_removed") { removeItem(msg.id); updateKpis(); }
    else if (msg.type === "zone_count") {
      const z = S.zones.find((q) => q._id === msg.id);
      if (z) z.inside_count = msg.count;
      const el = document.getElementById(`zc-${msg.id}`);
      if (el) { el.textContent = msg.count; el.parentElement.classList.add("flash"); setTimeout(() => el.parentElement.classList.remove("flash"), 600); }
    } else if (msg.type === "zones_changed") api("/api/zones").then(renderZones);
    else if (msg.type === "stream") {
      $("#live").className = `live ${msg.ok ? "ok" : "bad"}`;
      $("#live-text").textContent = msg.ok ? "change stream live" : "change stream reconnecting…";
    }
  };
  ws.onclose = () => { $("#live").className = "live bad"; $("#live-text").textContent = "disconnected, retrying…"; setTimeout(connect, 2000); };
}

// ------------------------------------------------------------------ boot
(async function boot() {
  try { await Promise.all([loadStatic(), loadFleet(), loadEvents()]); } catch (e) { console.error(e); }
  connect();
})();

window.__fleet = { S, map };
})();
