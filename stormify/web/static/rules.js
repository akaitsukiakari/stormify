// Stormify rule editor: rules, bulk scope changes, per-office view, saved office groups.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const OFFICES = window.STORMIFY_OFFICES || {};
  const KINDS = ["Emergency", "Warning", "Watch", "Advisory", "Statement", "Outlook", "Discussion", "Product", "Other"];
  // Common products, so a rule can name one before it has ever shown up in your feed.
  const EVENT_CATALOG = [
    "Tornado Warning", "Tornado Watch", "Severe Thunderstorm Warning", "Severe Thunderstorm Watch",
    "Flash Flood Warning", "Flash Flood Watch", "Flash Flood Statement", "Flood Warning", "Flood Watch", "Flood Advisory",
    "Flood Statement", "Special Weather Statement", "Severe Weather Statement", "Extreme Wind Warning",
    "Winter Storm Warning", "Winter Storm Watch", "Winter Weather Advisory", "Blizzard Warning", "Ice Storm Warning",
    "Lake Effect Snow Warning", "Snow Squall Warning", "Wind Chill Warning", "Wind Chill Advisory", "Extreme Cold Warning",
    "Cold Weather Advisory", "Freeze Warning", "Frost Advisory", "High Wind Warning", "High Wind Watch", "Wind Advisory",
    "Red Flag Warning", "Fire Weather Watch", "Fire Warning", "Dust Storm Warning", "Blowing Dust Advisory",
    "Excessive Heat Warning", "Extreme Heat Warning", "Heat Advisory", "Dense Fog Advisory", "Air Quality Alert",
    "Hurricane Warning", "Hurricane Watch", "Tropical Storm Warning", "Tropical Storm Watch", "Storm Surge Warning",
    "Storm Surge Watch", "Hurricane Local Statement", "Tsunami Warning", "Tsunami Watch", "Tsunami Advisory",
    "Coastal Flood Warning", "Coastal Flood Advisory", "High Surf Warning", "Rip Current Statement", "Gale Warning",
    "Small Craft Advisory", "Marine Weather Statement", "Hazardous Weather Outlook", "Short Term Forecast",
    "Child Abduction Emergency", "Civil Emergency Message", "Law Enforcement Warning", "Shelter In Place Warning",
    "Tropical Cyclone Public Advisory", "Tropical Cyclone Update", "Tropical Cyclone Forecast Advisory",
    "Tropical Cyclone Discussion", "Tropical Weather Outlook",
    "Day 1 Convective Outlook", "Day 2 Convective Outlook", "Day 3 Convective Outlook", "Mesoscale Discussion",
    "Geomagnetic Storm Watch", "Geomagnetic Storm Warning", "Geomagnetic Storm Alert", "Geomagnetic Warning",
    "Solar Radiation Storm Warning", "Solar Radiation Storm Alert", "Radio Blackout Alert", "Radio Blackout Summary",
    "Area Forecast Discussion", "Record Event Report", "Public Information Statement", "Local Storm Report",
  ];
  const TAGS = [
    ["emergency", "Emergency wording"], ["pds", "Particularly dangerous situation"], ["considerable", "Considerable damage threat"],
    ["destructive", "Destructive thunderstorm"], ["observed", "Observed / confirmed"], ["tornado-possible", "Tornado possible"],
    ["risk-mrgl", "SPC Marginal risk"], ["risk-slgt", "SPC Slight risk"], ["risk-enh", "SPC Enhanced risk"],
    ["risk-mdt", "SPC Moderate risk"], ["risk-high", "SPC High risk"],
    ["watch-likely", "MD: watch likely"], ["watch-possible", "MD: watch possible"], ["tornado-watch", "MD: about a tornado watch"],
    ["svr-watch", "MD: about a severe thunderstorm watch"], ["winter", "MD: winter weather"], ["heavy-rain", "MD: heavy rain"],
    ["geomagnetic", "Space weather: geomagnetic"], ["radiation", "Space weather: radiation"], ["radio", "Space weather: radio blackout"],
    ["g1", "G1"], ["g2", "G2"], ["g3", "G3"], ["g4", "G4"], ["g5", "G5"], ["s1", "S1"], ["s2", "S2"], ["s3", "S3"],
    ["s4", "S4"], ["s5", "S5"], ["r1", "R1"], ["r2", "R2"], ["r3", "R3"], ["r4", "R4"], ["r5", "R5"],
    ["hurricane-warning", "NHC: hurricane warning in effect"], ["hurricane-watch", "NHC: hurricane watch in effect"],
    ["ts-warning", "NHC: TS warning in effect"], ["ts-watch", "NHC: TS watch in effect"], ["surge-warning", "NHC: surge warning"],
    ["surge-watch", "NHC: surge watch"], ["major-hurricane", "NHC: major hurricane"], ["ww-changes", "NHC: watches/warnings changed"],
    ["atlantic", "NHC: Atlantic"], ["east-pacific", "NHC: Eastern Pacific"], ["central-pacific", "NHC: Central Pacific"],
  ];
  const PRIORITY = { 5: "5 · urgent", 4: "4 · high", 3: "3 · default", 2: "2 · low", 1: "1 · min" };
  const ON_UPDATE = { new_only: "New alerts only", significant: "Also significant updates", any: "Every update" };

  let rules = [];
  let groups = {};
  let meta = { events: [], offices: [] };
  let selected = new Set();
  let openUid = null;
  let previews = new Map();
  let dirty = false;
  let uidSeq = 0;
  const withUid = (r) => Object.assign(r, { _uid: ++uidSeq });

  // ---- helpers --------------------------------------------------------------------
  const officeLabel = (c) => OFFICES[c] ? `${c} · ${OFFICES[c]}` : c;
  const normOffice = (o) => { o = String(o).trim().toUpperCase(); return o.length === 4 && "KP".includes(o[0]) ? o.slice(1) : o; };
  const uniq = (xs) => [...new Set(xs)];
  const scopeOf = (r) => (r.scope ||= {});

  function setDirty(on = true) {
    dirty = on;
    $("save").disabled = !on;
    $("dirty").textContent = on ? "Unsaved changes" : "";
  }
  function banner(msg, kind = "error") {
    const b = $("banner");
    if (!msg) { b.hidden = true; return; }
    b.className = "banner " + kind; b.textContent = msg; b.hidden = false;
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3000);
  }
  async function api(url, opts = {}) {
    const r = await fetch(url, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
    if (r.status === 401) { location.href = "/login?next=/rules"; throw new Error("login"); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  // What a saved rule looks like: no empty lists, no UI bookkeeping.
  function cleanRule(r) {
    const out = {};
    for (const [k, v] of Object.entries(r)) {
      if (k.startsWith("_") || k === "id") continue;
      if (Array.isArray(v) && !v.length) continue;
      if (v === null || v === "" || v === undefined) continue;
      out[k] = v;
    }
    const s = {};
    for (const [k, v] of Object.entries(r.scope || {})) {
      if (k === "nationwide") { if (v) s.nationwide = true; continue; }
      if (Array.isArray(v) && v.length) s[k] = v;
    }
    out.scope = s;
    const f = {};
    for (const [k, v] of Object.entries(r.filters || {})) {
      if (k === "case_sensitive") { if (v) f.case_sensitive = true; continue; }
      if (Array.isArray(v) && v.length) f[k] = v;
    }
    if (Object.keys(f).length) out.filters = f; else delete out.filters;
    out.name = (r.name || "").trim() || "Untitled rule";
    return out;
  }

  // ---- suggestions (office codes by code or city, event names, tags) ------------------
  function officeSuggest(q) {
    q = q.trim().toLowerCase();
    if (!q) return [];
    const codes = uniq([...Object.keys(OFFICES), ...(meta.offices || [])]);
    const hits = codes.filter((c) => c.toLowerCase().startsWith(q) || (OFFICES[c] || "").toLowerCase().includes(q));
    hits.sort((a, b) => (a.toLowerCase().startsWith(q) ? 0 : 1) - (b.toLowerCase().startsWith(q) ? 0 : 1) || a.localeCompare(b));
    return hits.slice(0, 10).map((c) => ({ value: c, label: officeLabel(c) }));
  }
  function eventSuggest(q) {
    q = q.trim().toLowerCase();
    const all = uniq([...EVENT_CATALOG, ...(meta.events || [])]).sort();
    return all.filter((e) => !q || e.toLowerCase().includes(q)).slice(0, 12).map((e) => ({ value: e, label: e }));
  }
  function tagSuggest(q) {
    q = q.trim().toLowerCase();
    return TAGS.filter(([t, d]) => !q || t.includes(q) || d.toLowerCase().includes(q)).slice(0, 12)
      .map(([t, d]) => ({ value: t, label: `${t} · ${d}` }));
  }

  // A list of chips with a text box that suggests as you type. Enter or comma adds what's typed.
  function chipbox(el, values, { suggest, onChange, normalize = (v) => v.trim(), placeholder = "", labelOf = (v) => v } = {}) {
    let items = [...(values || [])];
    el.classList.add("chipbox");
    el.innerHTML = `<span class="chips-in"></span><span class="suggest-wrap"><input autocomplete="off" placeholder="${esc(placeholder)}"><div class="suggest" hidden></div></span>`;
    const chips = el.querySelector(".chips-in");
    const input = el.querySelector("input");
    const box = el.querySelector(".suggest");
    let hits = [], cursor = -1;
    const draw = () => {
      chips.innerHTML = items.map((v, i) => `<span class="pill" title="${esc(labelOf(v))}">${esc(v)}<button type="button" data-i="${i}" aria-label="Remove ${esc(v)}">×</button></span>`).join("");
    };
    const change = () => { draw(); onChange && onChange([...items]); };
    const add = (raw) => {
      const vals = String(raw).split(",").map(normalize).filter(Boolean);
      let changed = false;
      for (const v of vals) if (!items.includes(v)) { items.push(v); changed = true; }
      input.value = ""; close();
      if (changed) change();
    };
    const close = () => { box.hidden = true; hits = []; cursor = -1; };
    const show = () => {
      hits = suggest ? suggest(input.value).filter((h) => !items.includes(h.value)) : [];
      cursor = -1;
      if (!hits.length || document.activeElement !== input) { close(); return; }
      box.innerHTML = hits.map((h, i) => `<div class="opt" data-i="${i}">${esc(h.label)}</div>`).join("");
      box.hidden = false;
    };
    chips.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-i]");
      if (b) { items.splice(+b.dataset.i, 1); change(); }
    });
    input.addEventListener("input", show);
    input.addEventListener("focus", show);
    input.addEventListener("blur", () => setTimeout(() => { if (input.value.trim()) add(input.value); close(); }, 150));
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!hits.length) return;
        e.preventDefault();
        cursor = (cursor + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length;
        box.querySelectorAll(".opt").forEach((o, i) => o.classList.toggle("on", i === cursor));
      } else if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        if (cursor >= 0) add(hits[cursor].value);
        else if (input.value.trim()) add(hits.length === 1 && suggest === officeSuggest ? hits[0].value : input.value);
      } else if (e.key === "Backspace" && !input.value && items.length) {
        items.pop(); change();
      } else if (e.key === "Escape") close();
    });
    box.addEventListener("mousedown", (e) => {
      const o = e.target.closest(".opt");
      if (o) { e.preventDefault(); add(hits[+o.dataset.i].value); input.focus(); }
    });
    draw();
    return { get: () => [...items], set: (v) => { items = [...v]; draw(); } };
  }

  // ---- summaries ----------------------------------------------------------------------
  function listShort(xs, n = 3) {
    return xs.length <= n ? xs.join(", ") : `${xs.slice(0, n).join(", ")} +${xs.length - n}`;
  }
  function whatOf(r) {
    const parts = [];
    if ((r.events || []).length) parts.push(listShort(r.events, 2));
    if ((r.kinds || []).length) parts.push("any " + r.kinds.join("/").toLowerCase());
    if ((r.tags_any || []).length) parts.push("tagged " + listShort(r.tags_any, 3));
    return parts.join(" · ") || "Any product";
  }
  function whereOf(r) {
    const s = r.scope || {};
    if (s.nationwide) return { text: "Nationwide", warn: false };
    const parts = [];
    if ((s.groups || []).length) parts.push(s.groups.map((g) => `group “${g}”`).join(", "));
    if ((s.offices || []).length) parts.push(listShort(s.offices, 6));
    if ((s.states || []).length) parts.push("states " + listShort(s.states, 6));
    if ((s.zones || []).length) parts.push(`${s.zones.length} zone${s.zones.length > 1 ? "s" : ""}`);
    if ((s.same || []).length) parts.push(`${s.same.length} county code${s.same.length > 1 ? "s" : ""}`);
    return parts.length ? { text: parts.join(" · "), warn: false } : { text: "No locations, so it matches nothing", warn: true };
  }
  function hasFilters(r) {
    const f = r.filters || {};
    return ["include_any", "include_all", "exclude_any", "include_regex", "exclude_regex"].some((k) => (f[k] || []).length)
      || r.min_hail_in != null || r.min_wind_mph != null;
  }

  // ---- rule list ----------------------------------------------------------------------
  function renderRules() {
    const list = $("rule-list");
    if (!rules.length) {
      list.innerHTML = `<div class="empty">No rules yet. Everything still lands on the dashboard; rules decide what buzzes your phone.</div>`;
    } else {
      list.innerHTML = rules.map((r) => {
        const where = whereOf(r);
        const p = previews.get(r._uid);
        const pv = p ? `<div class="preview">Last ${p.hours} h: ${r.action === "log" ? `${p.log} logged` : `would push ${p.push}`}${p.events.length ? ` · ${p.events.map(([e, n]) => `${esc(e)} ×${n}`).join(", ")}` : ""}</div>` : "";
        return `<div class="rule${r.enabled === false ? " off" : ""}${selected.has(r._uid) ? " sel" : ""}" data-uid="${r._uid}">
          <div class="rule-head">
            <span class="handle" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
            <input type="checkbox" class="pick" ${selected.has(r._uid) ? "checked" : ""} aria-label="Select ${esc(r.name)}">
            <label class="switch" title="On/off"><input type="checkbox" class="enabled" ${r.enabled === false ? "" : "checked"}><span></span></label>
            <div class="rule-main">
              <div class="rule-name">${esc(r.name || "Untitled rule")}
                <span class="badge ${r.action === "log" ? "" : "push"}">${r.action === "log" ? "DASHBOARD" : "PUSH " + (r.priority || 3)}</span>
                ${hasFilters(r) ? `<span class="badge" title="Has text filters or thresholds">FILTERED</span>` : ""}
              </div>
              <div class="rule-sub"><span>${esc(whatOf(r))}</span> <span class="muted">in</span> <span class="${where.warn ? "warn" : ""}">${esc(where.text)}</span></div>
              ${pv}
            </div>
            <button class="edit">${openUid === r._uid ? "Close" : "Edit"}</button>
          </div>
          ${openUid === r._uid ? `<div class="editor" id="editor"></div>` : ""}
        </div>`;
      }).join("");
      if (openUid !== null && rules.some((r) => r._uid === openUid)) buildEditor(rules.find((r) => r._uid === openUid), $("editor"));
    }
    renderBulk();
  }

  function renderBulk() {
    const n = selected.size;
    $("sel-count").textContent = n ? `${n} selected` : "Select all";
    $("select-all").checked = n > 0 && n === rules.length;
    $("select-all").indeterminate = n > 0 && n < rules.length;
    $("bulk-actions").classList.toggle("disabled", !n);
    document.querySelectorAll("#bulk-actions [data-bulk]").forEach((b) => { b.disabled = !n; });
    const g = document.querySelector('[data-bulk="group"]');
    g.innerHTML = `<option value="">Apply group…</option>` + Object.keys(groups).sort().map((k) => `<option value="${esc(k)}">${esc(k)} (${groups[k].length})</option>`).join("");
    const c = document.querySelector('[data-bulk="copy"]');
    c.innerHTML = `<option value="">Copy scope from…</option>` + rules.map((r) => `<option value="${r._uid}">${esc(r.name)}</option>`).join("");
  }

  // ---- the editor for one rule ---------------------------------------------------------
  function buildEditor(r, el) {
    const s = scopeOf(r);
    r.filters ||= {};
    const f = r.filters;
    const lines = (k) => esc((f[k] || []).join("\n"));
    el.innerHTML = `
      <div class="grid">
        <label class="wide">Name<input data-k="name" value="${esc(r.name || "")}"></label>
        <fieldset><legend>When it matches</legend>
          <label class="radio"><input type="radio" name="act" value="push" ${r.action !== "log" ? "checked" : ""}> Push to my phone</label>
          <label class="radio"><input type="radio" name="act" value="log" ${r.action === "log" ? "checked" : ""}> Dashboard only</label>
          <label>Loudness<select data-k="priority">${Object.entries(PRIORITY).map(([v, l]) => `<option value="${v}" ${+v === +(r.priority || 3) ? "selected" : ""}>${l}</option>`).reverse().join("")}</select></label>
        </fieldset>
        <fieldset><legend>Updates and cancellations</legend>
          <label>On updates<select data-k="on_update">${Object.entries(ON_UPDATE).map(([v, l]) => `<option value="${v}" ${v === (r.on_update || "significant") ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          <label class="check"><input type="checkbox" data-k="on_cancel" ${r.on_cancel ? "checked" : ""}> Tell me when it's cancelled</label>
        </fieldset>

        <fieldset class="wide"><legend>Which products</legend>
          <label>Event names <span class="muted small">(empty = any)</span><div data-chip="events"></div></label>
          <div class="kinds">Or any of these kinds:
            ${KINDS.map((k) => `<button type="button" class="chip ${(r.kinds || []).includes(k) ? "on" : ""}" data-kind="${k}">${k}</button>`).join("")}
          </div>
          <label>Only if tagged with any of <div data-chip="tags_any"></div></label>
        </fieldset>

        <fieldset class="wide"><legend>Where</legend>
          <label class="check"><input type="checkbox" data-scope="nationwide" ${s.nationwide ? "checked" : ""}> Nationwide</label>
          <div class="where ${s.nationwide ? "dim" : ""}">
            <label>Offices <span class="muted small">(type a code or a city)</span><div data-chip="offices"></div></label>
            ${Object.keys(groups).length ? `<div class="groups-pick">Office groups: ${Object.keys(groups).sort().map((g) => `<label class="check"><input type="checkbox" data-group="${esc(g)}" ${(s.groups || []).includes(g) ? "checked" : ""}> ${esc(g)} <span class="muted small">(${groups[g].length})</span></label>`).join("")}</div>` : ""}
            <label>States <span class="muted small">(two letters)</span><div data-chip="states"></div></label>
            <details ${(s.zones || []).length || (s.same || []).length ? "open" : ""}><summary class="muted">Zones and county codes</summary>
              <label>UGC zones <span class="muted small">(COZ039, KSC173)</span><div data-chip="zones"></div></label>
              <label>SAME / FIPS county codes <span class="muted small">(008005)</span><div data-chip="same"></div></label>
            </details>
          </div>
        </fieldset>

        <fieldset class="wide"><legend>Text filters <span class="muted small">(one per line; a rule that fails these logs instead of pushing)</span></legend>
          <div class="grid3">
            <label>Contains any of<textarea data-f="include_any" rows="3">${lines("include_any")}</textarea></label>
            <label>Contains all of<textarea data-f="include_all" rows="3">${lines("include_all")}</textarea></label>
            <label>Skip if it contains<textarea data-f="exclude_any" rows="3">${lines("exclude_any")}</textarea></label>
            <label>Matches a regex<textarea data-f="include_regex" rows="2" spellcheck="false">${lines("include_regex")}</textarea></label>
            <label>Skip if a regex matches<textarea data-f="exclude_regex" rows="2" spellcheck="false">${lines("exclude_regex")}</textarea></label>
            <div>
              <label class="check"><input type="checkbox" data-f="case_sensitive" ${f.case_sensitive ? "checked" : ""}> Case sensitive</label>
              <label>Hail at least (in)<input type="number" step="0.25" min="0" data-k="min_hail_in" value="${r.min_hail_in ?? ""}"></label>
              <label>Wind at least (mph)<input type="number" step="5" min="0" data-k="min_wind_mph" value="${r.min_wind_mph ?? ""}"></label>
            </div>
          </div>
        </fieldset>

        <details class="wide"><summary class="muted">Notification text</summary>
          <p class="muted small">Leave empty for the default. Variables like {event}, {office}, {tags_prefix}, {hail_short}, {area_short}, {expires}. See docs/rules.md for the full list.</p>
          <label>Title<input data-k="title_template" value="${esc(r.title_template || "")}" placeholder="{status_prefix}{tags_prefix}{event} · {office}" spellcheck="false"></label>
          <label>Body<textarea data-k="body_template" rows="2" placeholder="{threat}{area_short}&#10;Until {expires}" spellcheck="false">${esc(r.body_template || "")}</textarea></label>
        </details>
      </div>
      <div class="editor-actions">
        <button type="button" data-act="up">Move up</button>
        <button type="button" data-act="down">Move down</button>
        <button type="button" data-act="dup">Duplicate</button>
        <span class="spacer"></span>
        <button type="button" data-act="del" class="danger">Delete rule</button>
      </div>`;

    const regexErr = (k) => {
      for (const p of f[k] || []) {
        try { new RegExp(p); } catch (e) { return `${p}: ${e.message}`; }
      }
      return "";
    };
    const refreshHead = () => {
      // Update the row's summary in place rather than redrawing (which would drop the focused field).
      const row = el.closest(".rule");
      row.querySelector(".rule-name").firstChild.textContent = (r.name || "Untitled rule") + " ";
      row.querySelector(".rule-sub").innerHTML = `<span>${esc(whatOf(r))}</span> <span class="muted">in</span> <span class="${whereOf(r).warn ? "warn" : ""}">${esc(whereOf(r).text)}</span>`;
      const badge = row.querySelector(".rule-name .badge");
      badge.className = "badge " + (r.action === "log" ? "" : "push");
      badge.textContent = r.action === "log" ? "DASHBOARD" : "PUSH " + (r.priority || 3);
      previews.delete(r._uid);
      row.querySelector(".preview")?.remove();
    };
    const touched = () => { setDirty(); refreshHead(); };

    el.addEventListener("input", (e) => {
      const t = e.target;
      if (t.dataset.k) {
        const k = t.dataset.k;
        if (t.type === "checkbox") r[k] = t.checked;
        else if (t.type === "number") r[k] = t.value === "" ? null : (k === "min_wind_mph" ? parseInt(t.value, 10) : parseFloat(t.value));
        else if (k === "priority") r[k] = parseInt(t.value, 10);
        else r[k] = t.value;
      } else if (t.dataset.f) {
        const k = t.dataset.f;
        if (t.type === "checkbox") f[k] = t.checked;
        else f[k] = t.value.split("\n").map((x) => x.trim()).filter(Boolean);
        if (k.endsWith("regex")) t.setCustomValidity(regexErr(k)), t.reportValidity();
      } else if (t.dataset.scope === "nationwide") {
        s.nationwide = t.checked;
        el.querySelector(".where").classList.toggle("dim", t.checked);
      } else if (t.dataset.group) {
        const g = new Set(s.groups || []);
        t.checked ? g.add(t.dataset.group) : g.delete(t.dataset.group);
        s.groups = [...g];
      } else if (t.name === "act") {
        r.action = t.value;
      } else return;
      touched();
    });
    el.querySelector(".kinds").addEventListener("click", (e) => {
      const k = e.target.dataset.kind;
      if (!k) return;
      const ks = new Set(r.kinds || []);
      ks.has(k) ? ks.delete(k) : ks.add(k);
      r.kinds = KINDS.filter((x) => ks.has(x));
      e.target.classList.toggle("on");
      touched();
    });
    const chip = (name, target, key, opts) => chipbox(el.querySelector(`[data-chip="${name}"]`), target[key], {
      ...opts, onChange: (v) => { target[key] = v; touched(); },
    });
    chip("events", r, "events", { suggest: eventSuggest, placeholder: "Tornado Warning…" });
    chip("tags_any", r, "tags_any", { suggest: tagSuggest, normalize: (v) => v.trim().toLowerCase(), placeholder: "pds, g3…" });
    chip("offices", s, "offices", { suggest: officeSuggest, normalize: normOffice, placeholder: "BOU, Denver…", labelOf: officeLabel });
    chip("states", s, "states", { normalize: (v) => v.trim().toUpperCase().slice(0, 2), placeholder: "CO" });
    chip("zones", s, "zones", { normalize: (v) => v.trim().toUpperCase(), placeholder: "COZ039" });
    chip("same", s, "same", { normalize: (v) => v.trim().replace(/\D/g, "").padStart(6, "0"), placeholder: "008005" });

    el.querySelector(".editor-actions").addEventListener("click", (e) => {
      const act = e.target.dataset.act;
      const i = rules.indexOf(r);
      if (act === "up" && i > 0) { rules.splice(i - 1, 0, rules.splice(i, 1)[0]); setDirty(); renderRules(); }
      if (act === "down" && i < rules.length - 1) { rules.splice(i + 1, 0, rules.splice(i, 1)[0]); setDirty(); renderRules(); }
      if (act === "dup") { const c = withUid({ ...clone(cleanRule(r)), name: r.name + " (copy)" }); rules.splice(i + 1, 0, c); openUid = c._uid; setDirty(); renderRules(); }
      if (act === "del" && confirm(`Delete “${r.name}”?`)) { rules.splice(i, 1); selected.delete(r._uid); openUid = null; setDirty(); renderRules(); }
    });
  }

  // ---- rule list interactions --------------------------------------------------------------
  $("rule-list").addEventListener("click", (e) => {
    const row = e.target.closest(".rule");
    if (!row || e.target.closest(".editor")) return;
    const uid = +row.dataset.uid;
    const r = rules.find((x) => x._uid === uid);
    if (e.target.closest(".edit") || (e.target.closest(".rule-main") && !e.target.closest("input"))) {
      openUid = openUid === uid ? null : uid;
      renderRules();
      if (openUid) row.scrollIntoView({ block: "nearest" });
    } else if (e.target.classList.contains("pick")) {
      e.target.checked ? selected.add(uid) : selected.delete(uid);
      row.classList.toggle("sel", e.target.checked);
      renderBulk();
    } else if (e.target.classList.contains("enabled")) {
      r.enabled = e.target.checked;
      row.classList.toggle("off", !r.enabled);
      setDirty();
    }
  });
  $("select-all").addEventListener("change", (e) => {
    selected = e.target.checked ? new Set(rules.map((r) => r._uid)) : new Set();
    renderRules();
  });

  // Drag to reorder (desktop). On a phone, Move up / Move down in the editor does the same.
  let dragUid = null;
  // Only the handle starts a drag, so text in the editor can still be selected.
  $("rule-list").addEventListener("mousedown", (e) => {
    const row = e.target.closest(".rule");
    if (row) row.draggable = !!e.target.closest(".handle");
  });
  $("rule-list").addEventListener("dragstart", (e) => {
    const row = e.target.closest?.(".rule");
    if (!row || e.target.closest(".editor")) { e.preventDefault(); return; }
    dragUid = +row.dataset.uid;
    e.dataTransfer.effectAllowed = "move";
    row.classList.add("dragging");
  });
  $("rule-list").addEventListener("dragover", (e) => {
    if (dragUid === null) return;
    e.preventDefault();
    document.querySelectorAll(".rule.drop").forEach((x) => x.classList.remove("drop"));
    e.target.closest(".rule")?.classList.add("drop");
  });
  $("rule-list").addEventListener("drop", (e) => {
    e.preventDefault();
    const row = e.target.closest(".rule");
    if (row && dragUid !== null && +row.dataset.uid !== dragUid) {
      const from = rules.findIndex((r) => r._uid === dragUid);
      const moved = rules.splice(from, 1)[0];
      const to = rules.findIndex((r) => r._uid === +row.dataset.uid);
      rules.splice(from <= to ? to + 1 : to, 0, moved);
      setDirty();
    }
    dragUid = null;
    renderRules();
  });
  $("rule-list").addEventListener("dragend", () => { dragUid = null; document.querySelectorAll(".rule.drop, .rule.dragging").forEach((x) => x.classList.remove("drop", "dragging")); });

  $("add-rule").addEventListener("click", () => {
    const r = withUid({ name: "New rule", enabled: true, events: [], kinds: [], scope: { offices: [] }, action: "push", priority: 3, on_update: "significant" });
    rules.unshift(r);
    openUid = r._uid;
    setDirty();
    renderRules();
    document.querySelector("#editor [data-k=name]")?.select();
  });

  // ---- bulk actions ---------------------------------------------------------------------------
  const picked = () => rules.filter((r) => selected.has(r._uid));
  let officeBar = null, officeBarMode = null;
  function openOfficeBar(mode) {
    officeBarMode = mode;
    $("office-bar-label").textContent = { "set-offices": "Set offices on the selected rules to:", "add-offices": "Add these offices to the selected rules:", "remove-offices": "Remove these offices from the selected rules:" }[mode];
    $("office-bar").hidden = false;
    officeBar = chipbox($("office-bar-input"), [], { suggest: officeSuggest, normalize: normOffice, placeholder: "BOU, Denver…", labelOf: officeLabel });
    $("office-bar-input").querySelector("input").focus();
  }
  $("office-bar-cancel").addEventListener("click", () => { $("office-bar").hidden = true; });
  $("office-bar-ok").addEventListener("click", () => {
    const pending = $("office-bar-input").querySelector("input").value.trim();
    const offices = uniq([...officeBar.get(), ...(pending ? pending.split(",").map(normOffice).filter(Boolean) : [])]);
    for (const r of picked()) {
      const s = scopeOf(r);
      if (officeBarMode === "set-offices") { s.offices = offices; s.nationwide = false; }
      if (officeBarMode === "add-offices") { s.offices = uniq([...(s.offices || []), ...offices]); s.nationwide = false; }
      if (officeBarMode === "remove-offices") s.offices = (s.offices || []).filter((o) => !offices.includes(o));
    }
    $("office-bar").hidden = true;
    setDirty(); previews.clear(); renderRules();
    toast(`Updated ${selected.size} rule${selected.size > 1 ? "s" : ""}.`);
  });

  document.getElementById("bulk-actions").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-bulk]");
    if (!b || !selected.size) return;
    const act = b.dataset.bulk;
    const rs = picked();
    if (act.endsWith("offices")) { openOfficeBar(act); return; }
    if (act === "nationwide") rs.forEach((r) => { r.scope = { nationwide: true }; });
    if (act === "clear-scope") {
      if (!confirm(`Clear every location from ${rs.length} rule${rs.length > 1 ? "s" : ""}? They'll match nothing until you give them a scope.`)) return;
      rs.forEach((r) => { r.scope = {}; });
    }
    if (act === "enable" || act === "disable") rs.forEach((r) => { r.enabled = act === "enable"; });
    if (act === "push" || act === "log") rs.forEach((r) => { r.action = act; });
    if (act === "duplicate") {
      for (const r of rs) {
        const c = withUid({ ...clone(cleanRule(r)), name: r.name + " (copy)" });
        rules.splice(rules.indexOf(r) + 1, 0, c);
      }
    }
    if (act === "delete") {
      if (!confirm(`Delete ${rs.length} rule${rs.length > 1 ? "s" : ""}?`)) return;
      rules = rules.filter((r) => !selected.has(r._uid));
      selected.clear();
    }
    setDirty(); previews.clear(); renderRules();
  });
  document.getElementById("bulk-actions").addEventListener("change", (e) => {
    const sel = e.target.closest("select[data-bulk]");
    if (!sel || !sel.value) return;
    const rs = picked();
    if (sel.dataset.bulk === "group") rs.forEach((r) => { const s = scopeOf(r); s.groups = uniq([...(s.groups || []), sel.value]); s.nationwide = false; });
    if (sel.dataset.bulk === "copy") {
      const src = rules.find((r) => r._uid === +sel.value);
      rs.forEach((r) => { if (r !== src) r.scope = clone(src.scope || {}); });
    }
    if (sel.dataset.bulk === "priority") rs.forEach((r) => { r.priority = +sel.value; });
    sel.value = "";
    setDirty(); previews.clear(); renderRules();
  });

  // ---- preview: run the draft over recent alerts -------------------------------------------------
  $("preview").addEventListener("click", async () => {
    $("preview").disabled = true;
    $("preview-note").textContent = "Checking…";
    try {
      const j = await api("/api/rules/preview", { method: "POST", body: JSON.stringify({ rules: rules.map(cleanRule), office_groups: groups, hours: 48 }) });
      previews = new Map(rules.map((r, i) => [r._uid, { ...j.rules[i], hours: j.hours }]));
      $("preview-note").textContent = `${j.alerts.toLocaleString()} alerts in the last ${j.hours} h · ${j.pushes} would have pushed${j.truncated ? " (newest only)" : ""}. Doesn't count updates or the per-poll cap.`;
      banner("");
      renderRules();
    } catch (e) {
      $("preview-note").textContent = "";
      if (e.message !== "login") banner("Couldn't check: " + e.message);
    } finally { $("preview").disabled = false; }
  });

  // ---- import ---------------------------------------------------------------------------------------
  $("import").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const incoming = Array.isArray(data) ? data : data.rules;
      if (!Array.isArray(incoming)) throw new Error("expected a list of rules or {\"rules\": [...]}");
      const replace = rules.length && confirm(`Replace your ${rules.length} rules with the ${incoming.length} in this file?\n\nCancel adds them to the end instead.`);
      const added = incoming.map((r) => withUid(clone(r)));
      rules = replace ? added : [...rules, ...added];
      setDirty(); previews.clear(); renderRules();
      toast(`${replace ? "Loaded" : "Added"} ${incoming.length} rules. Save to keep them.`);
    } catch (err) { banner("Couldn't import: " + err.message); }
  });

  // ---- by office ----------------------------------------------------------------------------------
  let pickedOffice = "";
  function coverage(r, code) {
    const s = r.scope || {};
    const via = (s.groups || []).filter((g) => (groups[g] || []).includes(code));
    return { nationwide: !!s.nationwide, direct: (s.offices || []).includes(code), via };
  }
  function renderOffice() {
    const list = $("office-list");
    $("office-actions").hidden = !pickedOffice;
    if (!pickedOffice) { list.innerHTML = `<div class="empty">Pick an office to see and change which rules cover it.</div>`; return; }
    $("office-name").textContent = OFFICES[pickedOffice] || "";
    const covered = rules.filter((r) => { const c = coverage(r, pickedOffice); return c.nationwide || c.direct || c.via.length; }).length;
    list.innerHTML = `<div class="muted small office-sum">${covered} of ${rules.length} rules cover ${esc(pickedOffice)}.</div>` + rules.map((r) => {
      const c = coverage(r, pickedOffice);
      const note = c.nationwide ? "Nationwide: covers every office" : c.via.length ? `Through group ${c.via.map((g) => `“${esc(g)}”`).join(", ")}` : "";
      return `<label class="office-row${r.enabled === false ? " off" : ""}">
        <input type="checkbox" data-uid="${r._uid}" ${c.direct || c.nationwide || c.via.length ? "checked" : ""} ${c.nationwide || (c.via.length && !c.direct) ? "disabled" : ""}>
        <span class="rule-name">${esc(r.name)}</span>
        <span class="badge ${r.action === "log" ? "" : "push"}">${r.action === "log" ? "DASHBOARD" : "PUSH " + (r.priority || 3)}</span>
        <span class="muted small">${esc(whatOf(r))}${note ? " · " + note : ""}</span>
      </label>`;
    }).join("");
  }
  $("office-list").addEventListener("change", (e) => {
    const uid = +e.target.dataset.uid;
    const r = rules.find((x) => x._uid === uid);
    if (!r) return;
    const s = scopeOf(r);
    s.offices = e.target.checked ? uniq([...(s.offices || []), pickedOffice]) : (s.offices || []).filter((o) => o !== pickedOffice);
    setDirty(); previews.clear(); renderOffice();
  });
  $("office-add-all").addEventListener("click", () => {
    rules.forEach((r) => { const s = scopeOf(r); if (!s.nationwide) s.offices = uniq([...(s.offices || []), pickedOffice]); });
    setDirty(); previews.clear(); renderOffice();
  });
  $("office-remove-all").addEventListener("click", () => {
    rules.forEach((r) => { const s = scopeOf(r); s.offices = (s.offices || []).filter((o) => o !== pickedOffice); });
    setDirty(); previews.clear(); renderOffice();
  });
  (() => {
    const input = $("office-pick");
    const box = input.parentElement.querySelector(".suggest");
    let hits = [];
    const choose = (code) => { pickedOffice = normOffice(code); input.value = pickedOffice; box.hidden = true; renderOffice(); };
    input.addEventListener("input", () => {
      hits = officeSuggest(input.value);
      box.innerHTML = hits.map((h, i) => `<div class="opt" data-i="${i}">${esc(h.label)}</div>`).join("");
      box.hidden = !hits.length;
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); choose(hits[0]?.value || input.value); } });
    input.addEventListener("blur", () => setTimeout(() => { box.hidden = true; }, 150));
    box.addEventListener("mousedown", (e) => { const o = e.target.closest(".opt"); if (o) { e.preventDefault(); choose(hits[+o.dataset.i].value); } });
  })();

  // ---- office groups -------------------------------------------------------------------------------
  function usedBy(name) { return rules.filter((r) => ((r.scope || {}).groups || []).includes(name)); }
  function renderGroups() {
    const list = $("group-list");
    const names = Object.keys(groups).sort();
    if (!names.length) { list.innerHTML = `<div class="empty">No groups yet.</div>`; return; }
    list.innerHTML = names.map((g) => `<div class="group" data-name="${esc(g)}">
        <div class="group-head"><input class="gname" value="${esc(g)}" aria-label="Group name"><span class="muted small">Used by ${usedBy(g).length} rule${usedBy(g).length === 1 ? "" : "s"}</span><span class="spacer"></span><button class="danger gdel">Delete</button></div>
        <div class="gchips"></div>
      </div>`).join("");
    list.querySelectorAll(".group").forEach((el) => {
      const name = el.dataset.name;
      chipbox(el.querySelector(".gchips"), groups[name], {
        suggest: officeSuggest, normalize: normOffice, placeholder: "Add offices…", labelOf: officeLabel,
        onChange: (v) => { groups[name] = v; setDirty(); previews.clear(); },
      });
    });
  }
  $("group-list").addEventListener("change", (e) => {
    if (!e.target.classList.contains("gname")) return;
    const old = e.target.closest(".group").dataset.name;
    const name = e.target.value.trim();
    if (!name || name === old) { e.target.value = old; return; }
    if (groups[name]) { banner(`There's already a group called “${name}”.`); e.target.value = old; return; }
    groups[name] = groups[old];
    delete groups[old];
    for (const r of usedBy(old)) r.scope.groups = r.scope.groups.map((g) => (g === old ? name : g));
    setDirty(); renderGroups();
  });
  $("group-list").addEventListener("click", (e) => {
    if (!e.target.classList.contains("gdel")) return;
    const name = e.target.closest(".group").dataset.name;
    const users = usedBy(name);
    if (!confirm(users.length ? `Delete “${name}”? ${users.length} rule(s) use it and will lose those offices.` : `Delete “${name}”?`)) return;
    delete groups[name];
    for (const r of users) r.scope.groups = r.scope.groups.filter((g) => g !== name);
    setDirty(); renderGroups();
  });
  $("add-group").addEventListener("click", () => {
    let n = 1, name = "My offices";
    while (groups[name]) name = `My offices ${++n}`;
    groups[name] = [];
    setDirty(); renderGroups();
    $("group-list").querySelector(`.group[data-name="${CSS.escape(name)}"] .gchips input`)?.focus();
  });

  // ---- tabs, save, load -------------------------------------------------------------------------------
  document.querySelector(".tabs").addEventListener("click", (e) => {
    const t = e.target.dataset.tab;
    if (!t) return;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    for (const p of ["rules", "office", "groups"]) $("pane-" + p).hidden = p !== t;
    if (t === "rules") renderRules();
    if (t === "office") renderOffice();
    if (t === "groups") renderGroups();
    try { localStorage.setItem("rulesTab", t); } catch { /* ignore */ }
  });

  $("save").addEventListener("click", async () => {
    const names = rules.map((r) => (r.name || "").trim());
    const dup = names.find((n, i) => names.indexOf(n) !== i);
    if (dup && !confirm(`Two rules are named “${dup}”. Previews and the feed's "matched" list can't tell them apart. Save anyway?`)) return;
    $("save").disabled = true;
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ office_groups: groups }) });
      await api("/api/rules", { method: "PUT", body: JSON.stringify({ rules: rules.map(cleanRule) }) });
      setDirty(false);
      banner("");
      toast("Saved. The poller uses them from its next check.");
    } catch (e) {
      $("save").disabled = false;
      if (e.message !== "login") banner("Not saved: " + e.message);
    }
  });
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  async function load() {
    try {
      const [r, s, m] = await Promise.all([api("/api/rules"), api("/api/settings"), api("/api/meta").catch(() => ({}))]);
      rules = (r.rules || []).map(withUid);
      groups = (s.settings || {}).office_groups || {};
      meta = { ...meta, ...m };
      renderRules();
      let tab = null;
      try { tab = localStorage.getItem("rulesTab"); } catch { /* ignore */ }
      if (tab && tab !== "rules") document.querySelector(`.tab[data-tab="${tab}"]`)?.click();
    } catch (e) {
      if (e.message !== "login") banner("Couldn't load your rules: " + e.message);
    }
  }
  load();
})();
