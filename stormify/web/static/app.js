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
  };
  const KIND_COLORS = {
    Emergency: "#ff2e63", Warning: "#ff6b3d", Watch: "#ffd23f", Advisory: "#5eb3ff",
    Statement: "#b4a7ff", Outlook: "#7fd4c1", Message: "#8b94a5", Product: "#c9b88a", Other: "#8b94a5",
  };
  const KINDS = ["Emergency", "Warning", "Watch", "Advisory", "Statement", "Outlook", "Product"];
  const TAG_LABELS = {
    emergency: "EMERGENCY", pds: "PDS", considerable: "CONSIDERABLE", destructive: "DESTRUCTIVE",
    observed: "OBSERVED", "tornado-possible": "TOR POSSIBLE", test: "TEST",
    "major-hurricane": "MAJOR", "hurricane-warning": "HU WARNING", "surge-warning": "SURGE WARNING",
    "ts-warning": "TS WARNING", "hurricane-watch": "HU WATCH", "surge-watch": "SURGE WATCH", "ts-watch": "TS WATCH",
  };
  // NHC products read better by their own title ("Tropical Storm Isaias Public Advisory Number 4").
  const labelFor = (a) => (a.source === "nhc" && a.headline) ? a.headline : a.event;

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const colorFor = (a) => (a.tags || []).includes("emergency") ? "#ff2e63" : (EVENT_COLORS[a.event] || KIND_COLORS[a.kind] || "#8b94a5");

  let meta = { timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, time_display: ["local", "event", "zulu"] };
  let alerts = [];
  let selected = new URLSearchParams(location.search).get("alert");
  const activeKinds = new Set();
  const layers = new Map();

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
        if (!e.target.closest(".legend-head")) return;
        el.classList.toggle("collapsed");
        try { localStorage.setItem("legendCollapsed", el.classList.contains("collapsed") ? "1" : ""); } catch { /* ignore */ }
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
    return p;
  }

  async function getJSON(url, opts) {
    const r = await fetch(url, { credentials: "same-origin", ...opts });
    if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
    return r;
  }

  async function load() {
    try {
      const r = await getJSON("/api/alerts?" + params());
      alerts = (await r.json()).alerts || [];
      render();
      $("updated").textContent = "updated " + new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    } catch (e) { if (e.message !== "login") $("count").textContent = "Could not load alerts"; }
  }

  async function loadMeta() {
    try {
      const r = await getJSON("/api/meta");
      meta = { ...meta, ...(await r.json()) };
      $("dl-offices").innerHTML = (meta.offices || []).map((o) => `<option value="${esc(o)}">`).join("");
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
    $("count").textContent = `${alerts.length} alert${alerts.length === 1 ? "" : "s"}`;
    polyGroup.clearLayers();
    layers.clear();
    const list = $("list");
    if (!alerts.length) {
      renderLegend();
      list.innerHTML = `<div class="empty">Nothing here for these filters.<br>Quiet skies, or widen the time range.</div>`;
      return;
    }
    // Draw watches under warnings under emergencies.
    const order = { Watch: 0, Advisory: 1, Statement: 1, Warning: 2, Emergency: 3 };
    [...alerts].sort((a, b) => (order[a.kind] ?? 1) - (order[b.kind] ?? 1)).forEach((a) => {
      if (!a.geometry) return;
      const c = colorFor(a);
      const layer = L.geoJSON(a.geometry, {
        style: { color: c, weight: a.kind === "Watch" ? 1.5 : 2.5, fillColor: c, fillOpacity: a.kind === "Watch" ? 0.08 : 0.22 },
        // Storm centers (NHC advisories) are points; their forecast track is a line.
        pointToLayer: (_, latlng) => L.circleMarker(latlng, { radius: 7, color: c, weight: 2, fillColor: c, fillOpacity: 0.7 }),
      });
      layer.on("click", () => select(a.id, false));
      layer.bindTooltip(`${esc(labelFor(a))} · ${esc(a.office)}`, { sticky: true });
      layer.addTo(polyGroup);
      layers.set(a.id, layer);
    });
    renderLegend();

    list.innerHTML = alerts.map((a) => {
      const tags = (a.tags || []).filter((t) => TAG_LABELS[t]).map((t) => `<span class="badge tag">${TAG_LABELS[t]}</span>`).join("");
      const upd = a.message_type && a.message_type !== "Alert" ? `<span class="badge upd">${esc(a.message_type.toUpperCase())}</span>` : "";
      const push = a.action === "push" ? `<span class="badge push">PUSHED</span>` : "";
      const threat = [a.hail_in ? `${a.hail_in}" hail` : "", a.wind_mph ? `${a.wind_mph} mph` : ""].filter(Boolean).join(" · ");
      const sent = fmtTimes(a.sent, a.event_tz);
      const exp = fmtTimes(a.expires || a.ends, a.event_tz);
      const body = [a.nws_headline, a.description, a.instruction].filter(Boolean).join("\n\n");
      return `<div class="card${a.id === selected ? " sel open" : ""}" data-id="${esc(a.id)}" style="--c:${colorFor(a)}">
        <div class="title">${tags}${esc(labelFor(a))} <span class="muted">· ${esc(a.office || a.sender_name)}</span> ${upd} ${push}</div>
        <div class="meta">${threat ? esc(threat) + " · " : ""}${esc(a.area_desc)}</div>
        <div class="times"><span class="muted">${esc(dayLabel(a.sent))}</span> ${esc(sent)}${exp ? ` <span class="muted">→ until</span> ${esc(exp)}` : ""}</div>
        ${a.reason ? `<div class="reason">${esc(a.reason)}</div>` : ""}
        <pre>${esc(body || a.headline)}</pre>
      </div>`;
    }).join("");

    if (selected && layers.has(selected)) {
      map.fitBounds(layers.get(selected).getBounds(), { maxZoom: 9, padding: [30, 30] });
    } else if (polyGroup.getLayers().length && !map._userMoved) {
      map.fitBounds(polyGroup.getBounds(), { maxZoom: 7, padding: [20, 20] });
    }
  }

  function swatch(e) {
    if (e.storm) {
      return `<svg viewBox="0 0 28 14" width="28" height="14"><line x1="7" y1="7" x2="27" y2="7" stroke="${e.color}" stroke-width="2.5"/>`
        + `<circle cx="7" cy="7" r="5" fill="${e.color}" fill-opacity="0.7" stroke="${e.color}" stroke-width="2"/></svg>`;
    }
    const watch = e.kind === "Watch";
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
      if (!layer || !layer.getBounds().isValid() || !view.intersects(layer.getBounds())) continue;
      const storm = a.source === "nhc" ? ((a.params || {}).storm || a.event) : "";
      const emergency = (a.tags || []).includes("emergency");
      const key = storm ? "nhc:" + storm : a.event + (emergency ? ":emergency" : "");
      const row = rows.get(key);
      if (row) { row.n += 1; continue; }
      rows.set(key, {
        storm, kind: a.kind, color: colorFor(a), n: 1,
        label: storm || (emergency && !/emergency/i.test(a.event) ? `${a.event} (emergency)` : a.event),
        rank: emergency ? -1 : (rank[a.kind] ?? 2),
      });
    }
    if (!rows.size) { el.hidden = true; return; }
    el.hidden = false;
    const items = [...rows.values()].sort((x, y) => (x.storm ? 0 : 1) - (y.storm ? 0 : 1) || x.rank - y.rank || x.label.localeCompare(y.label));
    const hasStorm = items.some((e) => e.storm);
    const now = new Date().toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: meta.timezone, timeZoneName: "short" });
    el.innerHTML = `<div class="legend-head"><span>Legend</span><span class="legend-toggle" aria-hidden="true"></span></div>
      <div class="legend-body">
        ${items.map((e) => `<div class="legend-row">${swatch(e)}<span>${esc(e.label)}${e.n > 1 ? ` <span class="muted">×${e.n}</span>` : ""}</span></div>`).join("")}
        ${hasStorm ? `<div class="legend-note">Dot: storm center · line: forecast track</div>` : ""}
        <div class="legend-note">Stormify · ${esc(now)}</div>
      </div>`;
  }

  function select(id, toggle = true) {
    const was = selected === id;
    selected = id;
    document.querySelectorAll(".card").forEach((el) => {
      const on = el.dataset.id === id;
      el.classList.toggle("sel", on);
      if (on) el.classList.toggle("open", toggle ? !el.classList.contains("open") || !was : true);
      if (on && !toggle) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    const layer = layers.get(id);
    if (layer) map.fitBounds(layer.getBounds(), { maxZoom: 9, padding: [30, 30] });
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
    load();
  });
  let debounce;
  ["f-hours", "f-action", "f-active"].forEach((id) => $(id).addEventListener("change", load));
  ["f-office", "f-event", "f-q"].forEach((id) => $(id).addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(load, 350); }));
  $("list").addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (card && !window.getSelection().toString()) select(card.dataset.id);
  });
  map.on("moveend", renderLegend);
  // Stop auto-fitting once you've panned or zoomed the map yourself.
  ["mousedown", "wheel", "touchstart"].forEach((ev) => $("map").addEventListener(ev, () => { map._userMoved = true; }, { passive: true }));
  $("test-btn").addEventListener("click", async () => {
    const r = await getJSON("/api/test-notification", { method: "POST" });
    const j = await r.json();
    toast(r.ok ? "Test notification sent." : (j.error || "Test failed."));
  });

  loadMeta().then(load);
  loadHealth();
  setInterval(load, 60000);
  setInterval(loadHealth, 30000);
})();
