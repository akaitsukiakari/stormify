// Stormify dashboard: feed + polygon map.
(() => {
  "use strict";

  // NWS-style colors for common products; everything else falls back by kind.
  const EVENT_COLORS = {
    "Tornado Warning": "#ff1f1f",
    "Tornado Watch": "#ffd400",
    "Severe Thunderstorm Warning": "#ff9f1a",
    "Severe Thunderstorm Watch": "#db7093",
    "Flash Flood Warning": "#1fbf4a",
    "Flash Flood Emergency": "#8b0000",
    "Flood Warning": "#00b050",
    "Special Weather Statement": "#ffe4b5",
    "Extreme Wind Warning": "#ff8c00",
    "Hurricane Warning": "#dc143c",
    "Tropical Storm Warning": "#b22222",
    "Winter Storm Warning": "#ff69b4",
    "Blizzard Warning": "#ff4500",
    "Red Flag Warning": "#ff1493",
    "Tsunami Warning": "#fd6347",
    "Hurricane Watch": "#ff00ff",
    "Tropical Storm Watch": "#f08080",
    "Storm Surge Warning": "#b524f7",
    "Storm Surge Watch": "#db7ff7",
    "Tropical Cyclone Public Advisory": "#ff4fa3",
    "Tropical Cyclone Update": "#ff4fa3",
    "Mesoscale Discussion": "#4fc3f7",
  };
  // SPC's own categorical outlook colors, lowest to highest risk.
  const RISK_COLORS = { TSTM: "#c1e9c1", MRGL: "#66a366", SLGT: "#ffe066", ENH: "#ffa366", MDT: "#e06666", HIGH: "#ee99ee" };
  const RISK_NAMES = { TSTM: "Thunderstorms", MRGL: "Marginal", SLGT: "Slight", ENH: "Enhanced", MDT: "Moderate", HIGH: "High" };
  const KIND_COLORS = {
    Emergency: "#ff2e63", Warning: "#ff6b3d", Watch: "#ffd23f", Advisory: "#5eb3ff",
    Statement: "#b4a7ff", Outlook: "#7fd4c1", Discussion: "#4fc3f7", Message: "#8b94a5", Product: "#c9b88a", Other: "#8b94a5",
  };
  const KINDS = ["Emergency", "Warning", "Watch", "Advisory", "Statement", "Outlook", "Discussion", "Product"];
  const TAG_LABELS = {
    emergency: "EMERGENCY", pds: "PDS", considerable: "CONSIDERABLE", destructive: "DESTRUCTIVE",
    observed: "OBSERVED", "tornado-possible": "TOR POSSIBLE", test: "TEST",
    "major-hurricane": "MAJOR", "hurricane-warning": "HU WARNING", "surge-warning": "SURGE WARNING",
    "ts-warning": "TS WARNING", "hurricane-watch": "HU WATCH", "surge-watch": "SURGE WATCH", "ts-watch": "TS WATCH",
    "watch-likely": "WATCH LIKELY", "risk-mdt": "MODERATE RISK", "risk-high": "HIGH RISK",
    g3: "G3", g4: "G4", g5: "G5", s3: "S3", s4: "S4", s5: "S5", r3: "R3", r4: "R4", r5: "R5",
  };
  // NHC, SPC and SWPC products read better by their own title ("Tropical Storm Isaias Public Advisory
  // Number 4", "Day 1: Enhanced risk", "Watch: Geomagnetic Storm Category G3 Predicted").
  const TITLED = new Set(["nhc", "spc", "swpc"]);
  const labelFor = (a) => (TITLED.has(a.source) && a.headline) ? a.headline : a.event;
  const isOutlook = (a) => a.source === "spc" && a.kind === "Outlook";
  const topRisk = (a) => ((a.params || {}).risk_labels || []).slice(-1)[0];

  // "BOU (Denver/Boulder, CO)": keep the code, add where it is when we know.
  const OFFICES = window.STORMIFY_OFFICES || {};
  const officeLabel = (code) => code && OFFICES[code] ? `${code} (${OFFICES[code]})` : (code || "");

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const colorFor = (a) => (a.tags || []).includes("emergency") ? "#ff2e63"
    : (isOutlook(a) && RISK_COLORS[topRisk(a)]) || EVENT_COLORS[a.event] || KIND_COLORS[a.kind] || "#8b94a5";

  let meta = { timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, time_display: ["local", "event", "zulu"] };
  let alerts = [];
  let total = 0;
  // Cards are drawn a page at a time so a long list doesn't bog down a phone.
  const LIST_PAGE = 100;
  let shown = LIST_PAGE;
  const bodies = new Map();  // alert id -> full text, fetched when a card or sheet opens
  const mobile = window.matchMedia("(max-width: 900px)");
  let selected = new URLSearchParams(location.search).get("alert");
  const activeKinds = new Set();
  const layers = new Map();
  // Alert types switched off from the legend, by legend key; remembered between visits.
  const hiddenKeys = new Set();
  try { JSON.parse(localStorage.getItem("hiddenPolys") || "[]").forEach((k) => hiddenKeys.add(k)); } catch { /* ignore */ }
  const saveHidden = () => { try { localStorage.setItem("hiddenPolys", JSON.stringify([...hiddenKeys])); } catch { /* ignore */ } };

  // ---- map ----------------------------------------------------------------
  const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([39, -97], 4);
  // CARTO's dark tiles need a free key; without one, fall back to standard OpenStreetMap tiles.
  const mapKey = document.body.dataset.mapKey;
  const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
  if (mapKey) {
    L.tileLayer(`https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=${encodeURIComponent(mapKey)}`, {
      attribution: OSM_ATTR + ' &copy; <a href="https://carto.com/attributions">CARTO</a>',
      subdomains: "abcd", maxZoom: 19,
    }).addTo(map);
  } else {
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: OSM_ATTR, maxZoom: 19 }).addTo(map);
  }
  const polyGroup = L.featureGroup().addTo(map);

  // ---- legend: what's drawn in the current view, so screenshots explain themselves ----
  const Legend = L.Control.extend({
    options: { position: "bottomleft" },
    onAdd() {
      const el = L.DomUtil.create("div", "legend");
      L.DomEvent.disableClickPropagation(el);
      L.DomEvent.disableScrollPropagation(el);
      el.addEventListener("click", (e) => {
        const all = e.target.closest("[data-all]");
        if (all) { setAllHidden(all.dataset.all === "hide"); return; }
        const row = e.target.closest(".legend-row[data-key]");
        if (row) { toggleKey(row.dataset.key); return; }
        if (!e.target.closest(".legend-head")) return;
        el.classList.toggle("collapsed");
        try { localStorage.setItem("legendCollapsed", el.classList.contains("collapsed") ? "1" : ""); } catch { /* ignore */ }
      });
      el.addEventListener("keydown", (e) => {
        const row = (e.key === "Enter" || e.key === " ") && e.target.closest(".legend-row[data-key]");
        if (row) { e.preventDefault(); toggleKey(row.dataset.key); }
      });
      try { if (localStorage.getItem("legendCollapsed")) el.classList.add("collapsed"); } catch { /* ignore */ }
      return el;
    },
  });
  const legend = new Legend().addTo(map);
  window.stormify = { map };  // handy for debugging from the console

  // ---- time formatting: your local / event local / Zulu -----------------------
  function offsetLabel(iso) {
    const m = /([+-])(\d{2}):?(\d{2})$/.exec(iso || "");
    if (!m) return "";
    return `UTC${m[1]}${parseInt(m[2], 10)}${m[3] !== "00" ? ":" + m[3] : ""}`;
  }
  function clock(d, tz) {
    try {
      return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: tz, timeZoneName: "short" });
    } catch { return null; }
  }
  function fmtTimes(iso, eventTz) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const parts = [];
    for (const k of meta.time_display) {
      if (k === "local") parts.push(clock(d, meta.timezone) || clock(d));
      else if (k === "event") {
        const e = eventTz && clock(d, eventTz);
        if (e) parts.push(e);
        else {
          // Show the issuing office's own wall-clock time from the ISO string.
          const m = /T(\d{2}):(\d{2})/.exec(iso);
          if (m) {
            let h = parseInt(m[1], 10); const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
            parts.push(`${h}:${m[2]} ${ap} ${offsetLabel(iso)}`.trim());
          }
        }
      } else if (k === "zulu") {
        parts.push(`${String(d.getUTCHours()).padStart(2, "0")}${String(d.getUTCMinutes()).padStart(2, "0")}Z`);
      }
    }
    return [...new Set(parts)].join(" · ");
  }
  function dayLabel(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: meta.timezone });
  }

  // ---- data -----------------------------------------------------------------
  function params() {
    const p = new URLSearchParams();
    p.set("hours", $("f-hours").value);
    if ($("f-action").value) p.set("action", $("f-action").value);
    if (activeKinds.size) p.set("kind", [...activeKinds].join(","));
    if ($("f-office").value.trim()) p.set("office", $("f-office").value.trim());
    if ($("f-event").value.trim()) p.set("event", $("f-event").value.trim());
    if ($("f-q").value.trim()) p.set("q", $("f-q").value.trim());
    if ($("f-active").checked) p.set("active", "1");
    if (selected) p.set("id", selected);
    p.set("lite", "1");
    return p;
  }

  async function getJSON(url, opts) {
    const r = await fetch(url, { credentials: "same-origin", ...opts });
    if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
    return r;
  }

  // The minute refresh usually brings back exactly what's already drawn (the server answers 304 and
  // the browser hands back its cached copy); skip the redraw then so a phone isn't rebuilding
  // hundreds of polygons and cards for nothing.
  let lastQuery = "", lastText = "";
  async function load(reset) {
    try {
      const q = params().toString();
      const [r] = await Promise.all([getJSON("/api/alerts?" + q), metaReady]);
      const text = await r.text();
      if (reset === true) shown = LIST_PAGE;
      if (reset === true || q !== lastQuery || text !== lastText) {
        const j = JSON.parse(text);
        alerts = j.alerts || [];
        total = j.total ?? alerts.length;
        lastQuery = q; lastText = text;
        render();
      }
      $("updated").textContent = "updated " + new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    } catch (e) { if (e.message !== "login") $("count").textContent = "Could not load alerts"; }
  }

  async function loadMeta() {
    try {
      const r = await getJSON("/api/meta");
      meta = { ...meta, ...(await r.json()) };
      $("dl-offices").innerHTML = (meta.offices || []).map((o) => `<option value="${esc(o)}"${OFFICES[o] ? ` label="${esc(o)} (${esc(OFFICES[o])})"` : ""}>`).join("");
      $("dl-events").innerHTML = (meta.events || []).map((e) => `<option value="${esc(e)}">`).join("");
    } catch { /* keep defaults */ }
  }

  async function loadHealth() {
    try {
      const r = await fetch("/api/health");
      const h = await r.json();
      const el = $("health");
      el.className = "health " + h.status;
      const age = h.seconds_since_success;
      $("health-text").textContent = h.status === "ok"
        ? `live · polled ${age}s ago · ${h.active_alerts ?? 0} active`
        : `poller stale${h.last_error ? " · " + h.last_error : ""}`;
    } catch { $("health-text").textContent = "status unknown"; }
  }

  // ---- render -----------------------------------------------------------------
  function render() {
    $("count").textContent = total > alerts.length
      ? `Newest ${alerts.length.toLocaleString()} of ${total.toLocaleString()} alerts`
      : `${alerts.length.toLocaleString()} alert${alerts.length === 1 ? "" : "s"}`;
    polyGroup.clearLayers();
    layers.clear();
    const list = $("list");
    if (!alerts.length) {
      renderLegend();
      list.innerHTML = `<div class="empty">Nothing here for these filters.<br>Quiet skies, or widen the time range.</div>`;
      return;
    }
    // Only the newest Day 1 outlook is drawn by default: outlooks cover half the country, and every
    // issuance plus Days 2 and 3 on top of each other would bury everything else. Older ones and
    // Days 2-3 draw when you pick them.
    newestDay1 = alerts.find((a) => isOutlook(a) && (a.params || {}).day === 1)?.id;
    // Draw outlooks under watches under warnings under emergencies.
    [...alerts].sort((a, b) => drawOrder(a) - drawOrder(b)).forEach((a) => {
      if (isOutlook(a) && a.id !== newestDay1 && a.id !== selected) return;
      addLayer(a);
    });
    highlight(selected);
    renderLegend();

    renderList();

    if (selected && layers.has(selected) && !map._userMoved) {
      map.fitBounds(layers.get(selected).bounds, { maxZoom: 9, padding: [30, 30] });
    } else if (polyGroup.getLayers().length && !map._userMoved) {
      map.fitBounds(polyGroup.getBounds(), { maxZoom: 7, padding: [20, 20] });
    }
  }

  let newestDay1 = null;
  const ORDER = { Watch: 0, Advisory: 1, Statement: 1, Discussion: 1, Warning: 2, Emergency: 3 };
  const drawOrder = (a) => isOutlook(a) ? -1 : (ORDER[a.kind] ?? 1);

  function addLayer(a) {
    if (!a.geometry || layers.has(a.id)) return layers.get(a.id);
    const c = colorFor(a);
    const watch = a.kind === "Watch";
    // Shapes built from zone outlines (no polygon of their own) get a dashed edge.
    const zoned = (a.params || {}).geometry_source === "zones";
    const subs = [], opts = {};
    let data = a.geometry, style = { color: c, weight: watch ? 1.5 : 2.5, fillColor: c, fillOpacity: watch ? 0.08 : 0.22, dashArray: zoned ? "5 4" : null };
    if (isOutlook(a)) {
      // One feature per risk area so each gets SPC's color.
      const labels = (a.params || {}).risk_labels || [];
      data = { type: "FeatureCollection", features: (a.geometry.geometries || []).map((g, i) => ({ type: "Feature", geometry: g, properties: { risk: labels[i] } })) };
      opts.onEachFeature = (f, l) => subs.push(l);
      style = (f) => {
        const rc = RISK_COLORS[f.properties.risk] || c;
        return { color: rc, weight: 1.5, fillColor: rc, fillOpacity: 0.18 };
      };
    }
    const layer = L.geoJSON(data, {
      ...opts,
      style,
      // Storm centers (NHC advisories) are points; their forecast track is a line.
      pointToLayer: (_, latlng) => L.circleMarker(latlng, { radius: 7, color: c, weight: 2, fillColor: c, fillOpacity: 0.7 }),
    });
    layer.on("click", (e) => { L.DomEvent.stopPropagation(e); mapTap(a); });
    layer.bindTooltip(`${esc(labelFor(a))} · ${esc(officeLabel(a.office))}`, { sticky: true });
    layer.addTo(polyGroup);
    if (isOutlook(a)) layer.bringToBack();
    layer.bounds = layer.getBounds();  // the legend checks these on every pan; work them out once
    layer.alert = a;
    layer.subs = subs;  // outlook risk areas, so each level can be hidden on its own
    layers.set(a.id, layer);
    return layer;
  }

  // Legend key for an alert: storms by name, everything else by event (emergencies on their own).
  // Outlooks are keyed per risk level instead ("risk:SLGT").
  function alertKey(a) {
    const storm = a.source === "nhc" ? ((a.params || {}).storm || a.event) : "";
    if (storm) return "nhc:" + storm;
    return a.event + ((a.tags || []).includes("emergency") ? ":emergency" : "");
  }

  // Put back on the map only what isn't switched off, in draw order. The selected alert always
  // shows, so the Map button and a shared link still find it.
  function applyVisibility() {
    polyGroup.clearLayers();
    [...layers.values()].sort((x, y) => drawOrder(x.alert) - drawOrder(y.alert)).forEach((layer) => {
      const a = layer.alert, force = a.id === selected;
      if (layer.subs.length) {
        layer.clearLayers();
        layer.subs.forEach((l) => { if (force || !hiddenKeys.has("risk:" + l.feature.properties.risk)) layer.addLayer(l); });
        if (layer.getLayers().length) polyGroup.addLayer(layer);
      } else if (force || !hiddenKeys.has(alertKey(a))) {
        polyGroup.addLayer(layer);
      }
    });
  }

  function toggleKey(key) {
    hiddenKeys.has(key) ? hiddenKeys.delete(key) : hiddenKeys.add(key);
    saveHidden();
    applyVisibility();
    highlight(selected);
    renderLegend();
  }

  // Hide all switches off every type loaded right now; types that show up later still draw.
  function setAllHidden(hide) {
    hiddenKeys.clear();
    if (hide) {
      for (const layer of layers.values()) {
        if (layer.subs.length) layer.subs.forEach((l) => hiddenKeys.add("risk:" + l.feature.properties.risk));
        else hiddenKeys.add(alertKey(layer.alert));
      }
    }
    saveHidden();
    applyVisibility();
    highlight(selected);
    renderLegend();
  }

  const PIN = `<svg viewBox="0 0 12 16" width="10" height="13" aria-hidden="true"><path d="M6 0a6 6 0 0 0-6 6c0 4.5 6 10 6 10s6-5.5 6-10a6 6 0 0 0-6-6zm0 8.5A2.5 2.5 0 1 1 6 3.5a2.5 2.5 0 0 1 0 5z" fill="currentColor"/></svg>`;

  function cardHtml(a, cls = "") {
    const tags = (a.tags || []).filter((t) => TAG_LABELS[t]).map((t) => `<span class="badge tag">${TAG_LABELS[t]}</span>`).join("");
    const upd = a.message_type && a.message_type !== "Alert" ? `<span class="badge upd">${esc(a.message_type.toUpperCase())}</span>` : "";
    const push = a.action === "push" ? `<span class="badge push">PUSHED</span>` : "";
    const threat = [a.hail_in ? `${a.hail_in}" hail` : "", a.wind_mph ? `${a.wind_mph} mph` : ""].filter(Boolean).join(" · ");
    const sent = fmtTimes(a.sent, a.event_tz);
    const exp = fmtTimes(a.expires || a.ends, a.event_tz);
    return `<div class="card${cls}" data-id="${esc(a.id)}" style="--c:${colorFor(a)}">
      <div class="title">${tags}${esc(labelFor(a))} <span class="muted">· ${esc(officeLabel(a.office) || a.sender_name)}</span> ${upd} ${push}${a.geometry ? `<button class="jump" title="Show on the map">${PIN}Map</button>` : ""}</div>
      <div class="meta">${threat ? esc(threat) + " · " : ""}${esc(a.area_desc)}</div>
      <div class="times"><span class="muted">${esc(dayLabel(a.sent))}</span> ${esc(sent)}${exp ? ` <span class="muted">→ until</span> ${esc(exp)}` : ""}</div>
      ${a.reason ? `<div class="reason">${esc(a.reason)}</div>` : ""}
      <pre>${bodies.has(a.id) ? esc(bodies.get(a.id)) : "Loading full text…"}</pre>
    </div>`;
  }

  function renderList() {
    // Keep the selected card on the page even if it sits past the first page.
    const at = selected ? alerts.findIndex((a) => a.id === selected) : -1;
    if (at >= shown) shown = Math.ceil((at + 1) / LIST_PAGE) * LIST_PAGE;
    const left = alerts.length - shown;
    $("list").innerHTML = alerts.slice(0, shown).map((a) => cardHtml(a, a.id === selected ? " sel open" : "")).join("")
      + (left > 0 ? `<button class="more" id="more">Show ${Math.min(left, LIST_PAGE)} more <span class="muted">(${left.toLocaleString()} left)</span></button>` : "")
      + (total > alerts.length ? `<div class="empty small">Showing the newest ${alerts.length.toLocaleString()}. Narrow the filters or time range to see older ones.</div>` : "");
    if (selected) fillBody(selected);
  }

  // Full text isn't in the list payload; fetch it once per alert when it's opened.
  async function fillBody(id) {
    if (!bodies.has(id)) {
      try {
        const r = await getJSON("/api/alert?id=" + encodeURIComponent(id));
        if (!r.ok) throw new Error("load");
        const a = (await r.json()).alert;
        bodies.set(id, [a.nws_headline, a.description, a.instruction].filter(Boolean).join("\n\n") || a.headline || "");
      } catch (e) {
        if (e.message === "login") return;
        document.querySelectorAll(`[data-id="${CSS.escape(id)}"] pre`).forEach((el) => { el.textContent = "Could not load the full text."; });
        return;
      }
    }
    document.querySelectorAll(`[data-id="${CSS.escape(id)}"] pre`).forEach((el) => { el.textContent = bodies.get(id); });
  }

  // On a phone the list sits under the map, so a tap shows the alert in a sheet over the page
  // instead of scrolling away. On a wide screen the list is beside the map, so it scrolls there.
  function mapTap(a) {
    if (!mobile.matches) { select(a.id, false); return; }
    openSheet(a, "Tapped on the map");
  }

  // Phone: the map (42vh) sits on top and the sheet (50vh) below it, so the polygon stays in view.
  let returnTo = null;  // where to scroll back to when a sheet opened from the list is closed
  function openSheet(a, label) {
    selected = a.id;
    const u = new URL(location); u.searchParams.set("alert", a.id); history.replaceState(null, "", u);
    document.querySelectorAll("#list .card").forEach((el) => el.classList.toggle("sel", el.dataset.id === a.id));
    highlight(a.id);
    $("sheet-label").textContent = label;
    $("sheet-body").innerHTML = cardHtml(a, " open");
    $("sheet").hidden = false;
    $("sheet").scrollTop = 0;
    $("map").scrollIntoView({ block: "start", behavior: "smooth" });
    fillBody(a.id);
  }
  function closeSheet(back) {
    if ($("sheet").hidden) return;
    $("sheet").hidden = true;
    if (back === true && returnTo !== null) window.scrollTo({ top: returnTo, behavior: "smooth" });
    returnTo = null;
  }

  // The list's Map button: zoom to the alert's polygon and pick it out. On a phone the list is below
  // the map, so scroll up to it and show the alert in the sheet; ✕ brings you back to your place.
  function jump(id) {
    const a = alerts.find((x) => x.id === id);
    const layer = a && addLayer(a);
    if (!a || !layer) return;
    map._userMoved = true;  // stay put on the next refresh
    if (mobile.matches) {
      if ($("sheet").hidden) returnTo = window.scrollY;
      openSheet(a, "Back to the list");
      map.fitBounds(layer.bounds, { maxZoom: 10, padding: [24, 24] });
    } else {
      select(id, false, 10);
    }
  }

  // Thicker outline on the selected polygon, drawn above its neighbors.
  let lit = null;
  function highlight(id) {
    if (lit) lit.subs.length ? lit.subs.forEach((l) => l.base && l.setStyle(l.base)) : lit.eachLayer((l) => l.setStyle(l.base));
    lit = null;
    if (hiddenKeys.size) applyVisibility();
    const layer = id && layers.get(id);
    if (!layer) return;
    layer.eachLayer((l) => {
      l.base ??= { weight: l.options.weight, fillOpacity: l.options.fillOpacity };
      l.setStyle({ weight: l.base.weight + 2.5, fillOpacity: Math.max(l.base.fillOpacity, 0.4) });
    });
    layer.bringToFront();
    lit = layer;
  }

  function swatch(e) {
    if (e.storm) {
      return `<svg viewBox="0 0 28 14" width="28" height="14"><line x1="7" y1="7" x2="27" y2="7" stroke="${e.color}" stroke-width="2.5"/>`
        + `<circle cx="7" cy="7" r="5" fill="${e.color}" fill-opacity="0.7" stroke="${e.color}" stroke-width="2"/></svg>`;
    }
    const watch = e.kind === "Watch" || e.kind === "Outlook";
    return `<svg viewBox="0 0 28 14" width="28" height="14"><rect x="1.5" y="1.5" width="25" height="11" rx="2" fill="${e.color}" `
      + `fill-opacity="${watch ? 0.15 : 0.35}" stroke="${e.color}" stroke-width="${watch ? 1.5 : 2.5}"/></svg>`;
  }

  // One row per distinct thing drawn inside the current view, most severe first.
  function renderLegend() {
    const el = legend.getContainer();
    const view = map.getBounds();
    const rank = { Emergency: 0, Warning: 1, Advisory: 2, Statement: 2, Watch: 3 };
    const rows = new Map();
    for (const a of alerts) {
      const layer = layers.get(a.id);
      if (!layer || !layer.bounds.isValid() || !view.intersects(layer.bounds)) continue;
      if (isOutlook(a)) {
        // One row per risk level drawn, SPC's colors.
        for (const r of (a.params || {}).risk_labels || []) {
          const key = "risk:" + r;
          if (!rows.has(key)) rows.set(key, { key, kind: "Outlook", color: RISK_COLORS[r] || colorFor(a), n: 1, rank: 10 + Object.keys(RISK_COLORS).indexOf(r) * -0.01, label: `Day ${(a.params || {}).day || ""} outlook: ${RISK_NAMES[r] || r}`, zoned: false });
        }
        continue;
      }
      const storm = a.source === "nhc" ? ((a.params || {}).storm || a.event) : "";
      const emergency = (a.tags || []).includes("emergency");
      const key = alertKey(a);
      const row = rows.get(key);
      if (row) { row.n += 1; if ((a.params || {}).geometry_source === "zones") row.zoned = true; continue; }
      rows.set(key, {
        key, storm, kind: a.kind, color: colorFor(a), n: 1, zoned: (a.params || {}).geometry_source === "zones",
        label: storm || (emergency && !/emergency/i.test(a.event) ? `${a.event} (emergency)` : a.event),
        rank: emergency ? -1 : (rank[a.kind] ?? 2),
      });
    }
    if (!rows.size) { el.hidden = true; return; }
    el.hidden = false;
    const items = [...rows.values()].sort((x, y) => (x.storm ? 0 : 1) - (y.storm ? 0 : 1) || x.rank - y.rank || x.label.localeCompare(y.label));
    const hasStorm = items.some((e) => e.storm);
    const hasZoned = items.some((e) => e.zoned);
    const now = new Date().toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: meta.timezone, timeZoneName: "short" });
    const anyOff = items.some((e) => hiddenKeys.has(e.key));
    const anyOn = items.some((e) => !hiddenKeys.has(e.key));
    el.innerHTML = `<div class="legend-head"><span>Legend</span><span class="legend-toggle" aria-hidden="true"></span></div>
      <div class="legend-body">
        <div class="legend-all">
          <button type="button" data-all="show"${anyOff ? "" : " disabled"}>Show all</button>
          <button type="button" data-all="hide"${anyOn ? "" : " disabled"}>Hide all</button>
        </div>
        ${items.map((e) => { const off = hiddenKeys.has(e.key); return `<div class="legend-row${off ? " off" : ""}" data-key="${esc(e.key)}" role="button" tabindex="0" aria-pressed="${!off}" title="${off ? "Show" : "Hide"} on the map">${swatch(e)}<span>${esc(e.label)}${e.n > 1 ? ` <span class="muted">×${e.n}</span>` : ""}</span></div>`; }).join("")}
        ${hasStorm ? `<div class="legend-note">Dot: storm center · line: forecast track</div>` : ""}
        ${hasZoned ? `<div class="legend-note">Dashed: drawn from the alert's zones</div>` : ""}
        <div class="legend-note">Stormify · ${esc(now)}</div>
      </div>`;
  }

  function select(id, toggle = true, maxZoom = 9) {
    const was = selected === id;
    selected = id;
    if (!document.querySelector(`#list .card[data-id="${CSS.escape(id)}"]`)) renderList();
    document.querySelectorAll(".card").forEach((el) => {
      const on = el.dataset.id === id;
      el.classList.toggle("sel", on);
      if (on) el.classList.toggle("open", toggle ? !el.classList.contains("open") || !was : true);
      if (on && el.classList.contains("open")) fillBody(id);
      if (on && !toggle) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    const pick = alerts.find((x) => x.id === id);
    const layer = pick && addLayer(pick);
    highlight(id);
    if (layer) map.fitBounds(layer.bounds, { maxZoom, padding: [30, 30] });
    const u = new URL(location); u.searchParams.set("alert", id); history.replaceState(null, "", u);
  }

  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3500);
  }

  // ---- wiring ---------------------------------------------------------------
  $("f-kinds").innerHTML = KINDS.map((k) => `<button class="chip" data-kind="${k}">${k}</button>`).join("");
  $("f-kinds").addEventListener("click", (e) => {
    const k = e.target.dataset.kind; if (!k) return;
    activeKinds.has(k) ? activeKinds.delete(k) : activeKinds.add(k);
    e.target.classList.toggle("on");
    load(true);
  });
  let debounce;
  const reload = () => load(true);
  ["f-hours", "f-action", "f-active"].forEach((id) => $(id).addEventListener("change", reload));
  ["f-office", "f-event", "f-q"].forEach((id) => $(id).addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(reload, 350); }));

  // ---- filter bar: collapses to one line, which matters on a phone ----------------
  // Starts collapsed on a phone and open on a wide screen; after that it remembers your choice.
  function summarize() {
    const parts = [$("f-hours").selectedOptions[0].text];
    if ($("f-action").value) parts.push($("f-action").selectedOptions[0].text);
    if (activeKinds.size) parts.push([...activeKinds].join(", "));
    for (const id of ["f-office", "f-event"]) if ($(id).value.trim()) parts.push($(id).value.trim());
    if ($("f-q").value.trim()) parts.push(`"${$("f-q").value.trim()}"`);
    if ($("f-active").checked) parts.push("active only");
    $("f-summary").textContent = parts.join(" · ");
  }
  function setFilters(open, remember) {
    $("filters").classList.toggle("collapsed", !open);
    $("f-toggle").setAttribute("aria-expanded", String(open));
    if (remember) { try { localStorage.setItem("filtersOpen", open ? "1" : "0"); } catch { /* ignore */ } }
    map.invalidateSize();  // on a wide screen the map just changed height
  }
  let filtersOpen = !mobile.matches;
  try { const v = localStorage.getItem("filtersOpen"); if (v) filtersOpen = v === "1"; } catch { /* ignore */ }
  setFilters(filtersOpen, false);
  $("f-toggle").addEventListener("click", () => setFilters($("filters").classList.contains("collapsed"), true));
  $("filters-body").addEventListener("change", summarize);
  $("filters-body").addEventListener("input", summarize);
  $("f-kinds").addEventListener("click", summarize);
  summarize();
  $("list").addEventListener("click", (e) => {
    if (e.target.closest("#more")) { shown += LIST_PAGE; renderList(); return; }
    const card = e.target.closest(".card");
    if (card && e.target.closest(".jump")) { jump(card.dataset.id); return; }
    if (card && !window.getSelection().toString()) select(card.dataset.id);
  });
  map.on("moveend", renderLegend);
  map.on("click", closeSheet);
  $("sheet-close").addEventListener("click", () => closeSheet(true));
  $("sheet-body").addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (card && e.target.closest(".jump")) jump(card.dataset.id);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(true); });
  mobile.addEventListener("change", closeSheet);
  // Stop auto-fitting once you've panned or zoomed the map yourself.
  ["mousedown", "wheel", "touchstart"].forEach((ev) => $("map").addEventListener(ev, () => { map._userMoved = true; }, { passive: true }));
  $("test-btn").addEventListener("click", async () => {
    const r = await getJSON("/api/test-notification", { method: "POST" });
    const j = await r.json();
    toast(r.ok ? "Test notification sent." : (j.error || "Test failed."));
  });

  // Meta and alerts load side by side; alerts wait for meta only before drawing (it sets the time zone).
  const metaReady = loadMeta();
  load();
  loadHealth();
  // No point polling the Pi from a tab nobody's looking at; catch up as soon as it's visible again.
  setInterval(() => { if (!document.hidden) load(); }, 60000);
  setInterval(() => { if (!document.hidden) loadHealth(); }, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { load(); loadHealth(); } });
})();
