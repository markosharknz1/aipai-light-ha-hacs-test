/*
 * AIPAI Reef Light card
 * ---------------------
 * A native Lovelace card for the AIPAI aquarium-light integration. It reads
 * each light's `Schedule` sensor (curves, labels, moon, mode, temperature) and
 * writes through the integration's services, so there is no long-lived token
 * and nothing to wire up by hand.
 *
 * The schedule is a list of TIME POINTS. Each point is an hour plus a level for
 * every channel, and the light fades between points - so a 0% point next to a
 * 100% point is the sunrise. There is no separate ramp setting. Editing a point
 * previews on the fixtures immediately; nothing is permanent until "Save
 * settings" (roll back with "Discard").
 *
 * Config:
 *   type: custom:aipai-reef-card
 *   name: Display tank          # optional heading
 *   serials: ["12345678", ...]  # optional; omit = every light HA knows about
 *
 * The heavy interaction logic (drag-to-retime, per-channel point editing,
 * import/export) mirrors the standalone mockup that was tested behaviourally in
 * a browser; see tests/frontend.
 */

const CFG_KIND = "aipai_light_config";
const CFG_VERSION = 1;
const SLOT_COUNT = 3;
const CHART_H = 80;
const CHART_TALL = 150;
const CHART_W = 320;
const PAD = 3;

// Fallback channel colours by label (the sensor tells us the labels).
const COLOURS = {
  white: "#e9c46a", "cool white": "#dbe7ff", warm: "#ffb021", blue: "#2e6bff",
  blue1: "#2e6bff", blue2: "#23c6e6", blue3: "#1f4dd6", red: "#ff4d4d",
  green: "#46c93a", purple: "#a64dff", uv: "#6c4dff", "orange light": "#ff8a3d",
  olive: "#8fb13a",
};
const colourFor = (label) => COLOURS[(label || "").toLowerCase()] || "#8aa";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const clamp100 = (v) => clamp(Math.round(Number(v) || 0), 0, 100);
const clampHour = (v) => clamp(Math.round(Number(v) || 0), 0, 23);
const hhmm = (h) => String(h).padStart(2, "0") + ":00";

// --- curve maths (shared with the device's 24-hourly model) ----------------
function channelCurve(points, ci) {
  const k = [...points].sort((a, b) => a.h - b.h);
  const out = [];
  for (let h = 0; h < 24; h++) {
    let v = 0;
    if (k.length && h >= k[0].h && h <= k[k.length - 1].h) {
      for (let i = 0; i < k.length - 1; i++) {
        const a = k[i], b = k[i + 1];
        if (h >= a.h && h <= b.h) {
          const av = a.ch[ci] ?? 0, bv = b.ch[ci] ?? 0;
          v = b.h === a.h ? Math.max(av, bv) : av + (bv - av) * (h - a.h) / (b.h - a.h);
          break;
        }
      }
      if (h === k[k.length - 1].h) v = k[k.length - 1].ch[ci] ?? 0;
    }
    out.push(clamp100(v));
  }
  return out;
}

// Curves (24 values per channel) -> time points, taking every hour. Lossless.
function curvesToPoints(curves) {
  const n = curves.length;
  if (!n) return [];
  return Array.from({ length: 24 }, (_, h) => ({
    h, ch: curves.map((c) => clamp100((c || [])[h] ?? 0)),
  }));
}

// Collapse 24 hourly points into a handful of keyframes for the editor. First
// trim to the lit span (plus one dark anchor each side, so sunrise/sunset stay
// defined), then keep only points where some channel's slope changes - the rest
// are redundant because the light interpolates through them anyway.
function pointsToKeyframes(points) {
  if (points.length <= 2) return points.map((p) => ({ h: p.h, ch: [...p.ch] }));
  const lit = (p) => p.ch.some((v) => v > 0);
  const first = points.findIndex(lit);
  if (first === -1) {  // all dark: two ends is enough
    return [points[0], points[points.length - 1]].map((p) => ({ h: p.h, ch: [...p.ch] }));
  }
  let last = points.length - 1;
  while (last > 0 && !lit(points[last])) last--;
  const lo = Math.max(0, first - 1);
  const hi = Math.min(points.length - 1, last + 1);
  const span = points.slice(lo, hi + 1);
  if (span.length <= 2) return span.map((p) => ({ h: p.h, ch: [...p.ch] }));
  const n = span[0].ch.length;
  const keep = [0, span.length - 1];
  for (let i = 1; i < span.length - 1; i++) {
    for (let c = 0; c < n; c++) {
      const a = span[i - 1].ch[c], b = span[i].ch[c], d = span[i + 1].ch[c];
      if (Math.abs((a + d) / 2 - b) > 0.5) { keep.push(i); break; }
    }
  }
  const idx = [...new Set(keep)].sort((a, b) => a - b);
  return idx.map((i) => ({ h: span[i].h, ch: [...span[i].ch] }));
}

class AipaiReefCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._ui = {
      sheet: null,          // "sched" | "moon" | "share" | "save" | "preset" | null
      sel: 0,               // selected time point
      focusCh: null,
      dragging: false,      // suppress re-render mid-drag
      shareText: "",
      shareMsg: "",
      editing: false,       // has THIS session pushed a live preview? (drives the banner)
    };
    this._draft = null;     // {points, moon} being edited, else null = live
    // Snapshot of the schedule as it was when this editing session started, so
    // Discard reverts to *that* - never a persisted baseline that could be stale
    // (a stale baseline once wrote all-channels-10% back to a healthy tank).
    this._session = null;
    this._built = false;
  }

  setConfig(config) {
    this._config = config || {};
    this._ui.sheet = null;
    this._draft = null;
  }

  getCardSize() { return 8; }

  static getConfigElement() {
    return document.createElement("aipai-reef-card-editor");
  }

  static getStubConfig() { return { name: "Reef tank" }; }

  set hass(hass) {
    this._hass = hass;
    if (this._ui.dragging) return;   // never yank the DOM out mid-gesture
    this._render();
  }

  // -- reading HA state --------------------------------------------------

  _lights() {
    if (!this._hass) return [];
    const want = this._config.serials
      ? new Set(this._config.serials.map(String))
      : null;
    const out = [];
    for (const [id, st] of Object.entries(this._hass.states)) {
      const a = st.attributes || {};
      if (a.aipai_kind !== "schedule") continue;
      if (want && !want.has(String(a.aipai_serial))) continue;
      out.push({
        entity: id,
        serial: String(a.aipai_serial),
        labels: a.labels || [],
        roads: a.roads || (a.labels || []).length,
        curves: a.curves || [],
        moon: a.moon || {},
        mode: a.mode,
        temperature: a.temperature,
      });
    }
    out.sort((x, y) => x.serial.localeCompare(y.serial));
    return out;
  }

  _slots() {
    if (!this._hass) return new Array(SLOT_COUNT).fill(null);
    for (const st of Object.values(this._hass.states)) {
      const a = st.attributes || {};
      if (a.aipai_kind === "presets" && Array.isArray(a.slots)) {
        return (a.slots.concat(new Array(SLOT_COUNT).fill(null))).slice(0, SLOT_COUNT);
      }
    }
    return new Array(SLOT_COUNT).fill(null);
  }

  // Each slot's stored levels, so a preset can be *viewed* before applying.
  _slotDetails() {
    if (this._hass) {
      for (const st of Object.values(this._hass.states)) {
        const a = st.attributes || {};
        if (a.aipai_kind === "presets" && Array.isArray(a.slot_details)) {
          return (a.slot_details.concat(new Array(SLOT_COUNT).fill(null))).slice(0, SLOT_COUNT);
        }
      }
    }
    return new Array(SLOT_COUNT).fill(null);
  }

  // A slot's levels as one value per channel in this fixture's label order.
  _slotLevels(i, labels) {
    const d = this._slotDetails()[i];
    const wanted = {};
    Object.entries((d && d.levels) || {}).forEach(([k, v]) => { wanted[k.toLowerCase()] = v; });
    return labels.map((lab) => clamp100(wanted[(lab || "").toLowerCase()] ?? 0));
  }

  // The tank's schedule as editable points. When editing we use the draft;
  // otherwise we derive keyframes from the first light's live curves.
  _points(lights) {
    if (this._draft) return this._draft.points;
    const base = lights[0];
    if (!base || !base.curves.length) return [];
    return pointsToKeyframes(curvesToPoints(base.curves));
  }

  _labels(lights) {
    return (lights[0] && lights[0].labels) || [];
  }

  _serials(lights) { return lights.map((l) => l.serial); }

  // -- calling services --------------------------------------------------

  _call(service, data) {
    if (!this._hass) return Promise.resolve();
    return this._hass.callService("aipai_light", service, data || {});
  }

  async _callResp(service, data) {
    const res = await this._hass.callWS({
      type: "call_service", domain: "aipai_light", service,
      service_data: data || {}, return_response: true,
    });
    return (res && res.response) || {};
  }

  _pointsPayload(points) {
    return points.map((p) => ({ hour: p.h, levels: p.ch.map((v) => clamp100(v)) }));
  }

  _previewDraft() {
    if (!this._draft) return;
    const lights = this._lights();
    this._ui.editing = true;          // this session has pushed a live change
    this._call("preview_schedule", {
      serial: this._serials(lights),
      points: this._pointsPayload(this._draft.points),
    });
  }

  // Revert to the schedule as it was when this session began - captured in the
  // card, never a persisted baseline that might be stale.
  _restoreSession() {
    const lights = this._lights();
    if (this._session && this._session.points && this._session.points.length) {
      this._call("preview_schedule", {
        serial: this._serials(lights),
        points: this._pointsPayload(this._session.points),
      });
    }
    this._ui.editing = false;
    this._session = null;
    this._draft = null;
  }

  // -- geometry ----------------------------------------------------------

  _chartH() { return this._ui.sheet === "sched" ? CHART_TALL : CHART_H; }
  _x(i) { return PAD + (i / 24) * (CHART_W - 2 * PAD); }
  _y(v, h) { return h - PAD - (v / 100) * (h - 2 * PAD - 6); }

  _svg(points, labels, moon, editing, sel, focusCh) {
    const h = this._chartH();
    let g = "";
    for (let i = 0; i <= 24; i += 6) {
      g += `<line x1="${this._x(i)}" y1="${PAD}" x2="${this._x(i)}" y2="${h - PAD}" stroke="currentColor" opacity=".1"/>`;
    }
    let p = "";
    labels.forEach((lab, ci) => {
      const dim = focusCh != null && focusCh !== ci;
      const d = channelCurve(points, ci).map((v, i) =>
        (i ? "L" : "M") + this._x(i).toFixed(1) + " " + this._y(v, h).toFixed(1)).join(" ");
      p += `<path data-ch="${ci}" d="${d}" fill="none" stroke="${colourFor(lab)}" stroke-width="${dim ? 1 : 2}" stroke-linejoin="round" opacity="${dim ? 0.28 : 0.95}"/>`;
    });
    if (moon && (moon.run || moon.enabled)) {
      const a = this._moonH(moon.start), b = this._moonH(moon.end);
      const my = this._y(clamp100(moon.level), h);
      const segs = b > a ? [[a, b]] : [[a, 24], [0, b]];
      segs.forEach(([s0, s1]) => {
        p += `<line data-moon x1="${this._x(s0)}" y1="${my}" x2="${this._x(s1)}" y2="${my}" stroke="#8fa6c8" stroke-width="1.6" stroke-dasharray="3 3" opacity=".9"/>`;
      });
    }
    if (editing) {
      [...points].sort((a, b) => a.h - b.h).forEach((k, i) => {
        const on = i === sel;
        p += `<g data-kfdot="${i}" transform="translate(${this._x(k.h).toFixed(1)},0)" style="cursor:grab">
          <rect x="-9" y="0" width="18" height="${h}" fill="transparent"/>
          <line x1="0" y1="${PAD}" x2="0" y2="${h - PAD}" stroke="var(--primary-color)" stroke-width="${on ? 1.6 : 1}" stroke-dasharray="${on ? "" : "2 3"}" opacity="${on ? 0.95 : 0.4}" pointer-events="none"/>
          <circle cx="0" cy="${PAD + 4}" r="${on ? 4.4 : 3.2}" fill="${on ? "var(--primary-color)" : "var(--card-background-color)"}" stroke="var(--primary-color)" stroke-width="1.8" pointer-events="none"/>
        </g>`;
      });
    }
    return `<svg viewBox="0 0 ${CHART_W} ${h}" preserveAspectRatio="none">${g}${p}</svg>`;
  }

  // moon start/end are literal HH.MM floats (5.30 == 05:30) on the device.
  _moonH(v) {
    const n = Number(v) || 0;
    const hrs = Math.floor(n);
    return hrs + (n - hrs) * 100 / 60;
  }

  // -- rendering ---------------------------------------------------------

  _render() {
    const lights = this._lights();
    const labels = this._labels(lights);
    const points = this._points(lights);
    const slots = this._slots();
    const sheeting = this._ui.sheet === "sched" || this._ui.sheet === "moon" || this._ui.sheet === "share";
    const sel = Math.min(this._ui.sel, Math.max(0, points.length - 1));
    const temps = lights.map((l) => l.temperature).filter((t) => typeof t === "number");
    const avg = temps.length ? (temps.reduce((a, b) => a + b, 0) / temps.length).toFixed(1) : "–";
    const name = this._config.name || "Reef tank";
    const moon = this._draft ? this._draft.moon : (lights[0] && lights[0].moon) || {};

    if (!lights.length) {
      this.shadowRoot.innerHTML = `${this._style()}<div class="card"><div class="empty">
        No AIPAI lights found.${this._config.serials ? " Check the serials in this card's config." : ""}
      </div></div>`;
      return;
    }

    this.shadowRoot.innerHTML = `${this._style()}
      <div class="card${sheeting ? " sheeting" : ""}">
        <div class="hdr">
          <div class="ttl"><div class="n">${name}</div>
            <div class="s">${lights.length} light${lights.length > 1 ? "s" : ""} · ${avg} °C avg</div></div>
        </div>
        <div class="row slots">
          ${slots.map((n2, i) => n2
            ? `<span class="chip" data-slot="${i}">${this._esc(n2)}</span>`
            : `<span class="chip empty" data-slot="${i}">Preset ${i + 1}</span>`).join("")}
          <span class="grow"></span>
          <span class="act" data-act="save">💾 Save preset</span>
        </div>
        ${this._sheet(points, labels, sel, moon, slots)}
        ${this._ui.editing && !this._ui.sheet ? this._unsavedBar() : ""}
        <div class="chart${this._ui.sheet === "sched" ? " editing" : ""}">
          ${this._svg(points, labels, moon, this._ui.sheet === "sched", sel, this._ui.focusCh)}
        </div>
        <div class="axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
        ${lights.map((l) => this._lightRow(l, sheeting)).join("")}
        <div class="ftr">
          <span class="btn" data-act="sched">EDIT SCHEDULE</span>
          <span class="btn" data-act="moon">MOONLIGHT</span>
          <span class="btn" data-act="share">IMPORT / EXPORT</span>
          <span class="btn" data-act="clock">SYNC CLOCK</span>
        </div>
      </div>`;
    this._wire();
  }

  _lightRow(l, sheeting) {
    const cls = sheeting ? " affected" : "";
    const tag = sheeting ? `<span class="rtag">will change</span>` : "";
    const t = typeof l.temperature === "number" ? ` · ${l.temperature} °C` : "";
    const modeTxt = l.mode === "1" ? "Scheduled" : "Manual";
    return `<div class="lite${cls}">
      <div class="literow">
        <div class="ttl"><div class="n sm">${l.serial}${tag}</div>
          <div class="s">${modeTxt}${t}</div></div>
      </div></div>`;
  }

  _unsavedBar() {
    return `<div class="unsaved">● <b>Previewing</b> · showing on the lights now, not saved
      <span class="sp">
        <span class="b discard" data-commit="discard">Discard</span>
        <span class="b save2" data-commit="save">Save settings</span>
      </span></div>`;
  }

  _sheet(points, labels, sel, moon, slots) {
    const s = this._ui.sheet;
    if (s === "sched") {
      const P = [...points].sort((a, b) => a.h - b.h);
      const k = P[sel] || P[0] || { h: 0, ch: labels.map(() => 0) };
      const edge = sel === 0 ? "first light" : sel === P.length - 1 ? "lights out" : "";
      return `<div class="sheet">
        <div class="q">Daily schedule — applies to ${this._lights().length} light(s)</div>
        <div class="row wrap">
          <span class="lbl">Time point</span>
          ${P.map((p, i) => `<span class="chip mini${i === sel ? " on" : ""}" data-ptsel="${i}">${hhmm(p.h)}</span>`).join("")}
          <span class="chip mini" data-ptadd>＋</span>
        </div>
        <div class="ptedit">
          <div class="row wrap">
            <span class="lbl">At</span>
            <input type="time" step="3600" class="timein" data-pth value="${hhmm(k.h)}">
            <span class="chip mini" data-ptcopy title="Give every other lit point these levels">Copy to all</span>
            <span class="chip mini" data-ptdel>Remove</span>
            <span class="lbl">${edge}</span>
          </div>
          ${labels.map((lab, ci) => {
            const v = clamp100(k.ch[ci]);
            return `<div class="ch"><span class="cn">${this._esc(lab)}</span>
              <span class="tr" data-pci="${ci}" tabindex="0" role="slider" aria-label="${this._esc(lab)}" aria-valuenow="${v}" aria-valuemin="0" aria-valuemax="100">
                <span class="fi" style="width:${v}%;background:${colourFor(lab)}"></span>
                <span class="kn" style="left:${v}%;background:${colourFor(lab)}"></span></span>
              <span class="vv">${v}%</span></div>`;
          }).join("")}
        </div>
        <div class="row"><span class="chip mini" data-sc="apply">Done</span>
          <span class="chip mini" data-sc="cancel">Cancel</span>
          <span class="lbl">${P.length} points · on the hour · fades between them</span></div>
      </div>`;
    }
    if (s === "moon") {
      const on = !!(moon.run || moon.enabled);
      return `<div class="sheet"><div class="q">Moonlight</div>
        <div class="row wrap">
          <span class="chip mini${on ? " on" : ""}" data-mn="toggle">${on ? "On" : "Off"}</span>
          <span class="lbl">from</span><input type="time" class="timein" data-mn2="start" value="${this._moonStr(moon.start)}">
          <span class="lbl">to</span><input type="time" class="timein" data-mn2="end" value="${this._moonStr(moon.end)}">
        </div>
        <div class="row wrap"><span class="lbl">Level</span>
          <span class="step"><button data-mlvl="-1">−</button><span class="qty">${clamp100(moon.level)} %</span><button data-mlvl="1">+</button></span></div>
        <div class="row"><span class="chip mini" data-mn="apply">Apply</span>
          <span class="chip mini" data-mn="cancel">Cancel</span></div>
      </div>`;
    }
    if (s === "share") {
      return `<div class="sheet"><div class="q">Share this tank's setup</div>
        <div class="row wrap">
          <span class="chip mini" data-share="export">⬆ Export</span>
          <span class="chip mini" data-share="import">⬇ Import</span>
          <span class="chip mini" data-share="close">Close</span>
          <span class="lbl">${this._esc(this._ui.shareMsg || "channels match by name, so this works across different lights")}</span>
        </div>
        <textarea class="sharebox" data-sharebox spellcheck="false" placeholder="Paste a config here, then press Import">${this._esc(this._ui.shareText || "")}</textarea>
      </div>`;
    }
    if (s === "save") {
      const slots2 = this._slots();
      return `<div class="sheet"><div class="q">Save the tank's current look</div>
        <div class="row wrap" style="padding-bottom:8px">
          <span class="lbl">Name</span>
          <input type="text" class="nameit" data-savename value="My look" placeholder="Preset name">
        </div>
        <div class="lbl" style="padding-bottom:6px">Into which slot?</div>
        <div class="row wrap">
          ${slots2.map((n, i) => `<span class="chip mini" data-saveslot="${i}">${i + 1} · ${n ? "replace “" + this._esc(n) + "”" : "empty"}</span>`).join("")}
          <span class="chip mini" data-saveslot="cancel">Cancel</span></div></div>`;
    }
    if (s === "preset") {
      const i = this._ui.presetSlot;
      const name = (this._slots()[i]) || `Preset ${i + 1}`;
      const editing = this._ui.presetEdit;
      // While editing we work on a local copy; viewing reads the stored levels.
      const vals = editing ? this._ui.presetDraft : this._slotLevels(i, labels);
      return `<div class="sheet">
        <div class="q">${editing ? "Editing" : "Preset"} — ${this._esc(name)}</div>
        <div class="lbl" style="padding-bottom:6px">${editing
          ? "Change the levels, then Save. The lights don't change until you Apply."
          : "This is what the preset holds. The lights haven't changed."}</div>
        ${labels.map((lab, ci) => {
          const v = clamp100(vals[ci]);
          const bar = editing
            ? `<span class="tr" data-slci="${ci}" tabindex="0" role="slider" aria-label="${this._esc(lab)}" aria-valuenow="${v}" aria-valuemin="0" aria-valuemax="100">
                 <span class="fi" style="width:${v}%;background:${colourFor(lab)}"></span>
                 <span class="kn" style="left:${v}%;background:${colourFor(lab)}"></span></span>`
            : `<span class="tr ro"><span class="fi" style="width:${v}%;background:${colourFor(lab)}"></span></span>`;
          return `<div class="ch"><span class="cn">${this._esc(lab)}</span>${bar}<span class="vv">${v}%</span></div>`;
        }).join("")}
        <div class="row wrap" style="padding-top:6px">
          ${editing
            ? `<span class="chip mini" data-preset="save">Save preset</span>
               <span class="chip mini" data-preset="cancel">Cancel</span>`
            : `<span class="chip mini" data-preset="apply">Apply to tank</span>
               <span class="chip mini" data-preset="edit">Edit</span>
               <span class="chip mini" data-preset="delete">Delete</span>
               <span class="chip mini" data-preset="close">Close</span>`}
        </div></div>`;
    }
    return "";
  }

  _moonStr(v) {
    const n = Number(v) || 0;
    const hrs = Math.floor(n);
    const mins = Math.round((n - hrs) * 100);
    return String(hrs).padStart(2, "0") + ":" + String(mins).padStart(2, "0");
  }

  // -- interaction -------------------------------------------------------

  _wire() {
    const root = this.shadowRoot;
    const $ = (s) => root.querySelector(s);
    const $$ = (s) => [...root.querySelectorAll(s)];

    $$("[data-act]").forEach((b) => b.onclick = () => this._onAct(b.dataset.act));
    $$("[data-commit]").forEach((b) => b.onclick = () => this._onCommit(b.dataset.commit));
    $$("[data-slot]").forEach((c) => c.onclick = () => this._onSlot(+c.dataset.slot));
    $$("[data-saveslot]").forEach((c) => c.onclick = () => this._onSaveSlot(c.dataset.saveslot));

    // preset view / edit
    $$("[data-preset]").forEach((b) => b.onclick = () => this._onPreset(b.dataset.preset));
    $$("[data-slci]").forEach((tr) => this._wirePresetSlider(tr));

    // schedule points
    $$("[data-ptsel]").forEach((c) => c.onclick = () => { this._ui.sel = +c.dataset.ptsel; this._ui.focusCh = null; this._render(); });
    const pth = $("[data-pth]");
    if (pth) pth.onchange = () => this._onPointTime(pth.value);
    const ptadd = $("[data-ptadd]"); if (ptadd) ptadd.onclick = () => this._onPointAdd();
    const ptdel = $("[data-ptdel]"); if (ptdel) ptdel.onclick = () => this._onPointDel();
    const ptcopy = $("[data-ptcopy]"); if (ptcopy) ptcopy.onclick = () => this._onCopyAll();
    $$("[data-sc]").forEach((c) => c.onclick = () => this._onSchedClose(c.dataset.sc));

    // per-channel sliders in the point editor
    $$("[data-pci]").forEach((tr) => this._wireChannel(tr));

    // chart drag (retime a point) + tap to add
    this._wireChart();

    // moon
    $$("[data-mn]").forEach((c) => c.onclick = () => this._onMoon(c.dataset.mn));
    $$("[data-mn2]").forEach((i) => i.onchange = () => this._onMoonTime(i.dataset.mn2, i.value));
    $$("[data-mlvl]").forEach((b) => b.onclick = () => this._onMoonLevel(+b.dataset.mlvl));

    // share
    $$("[data-share]").forEach((b) => b.onclick = () => this._onShare(b.dataset.share));
  }

  _ensureDraft() {
    if (this._draft) return;
    const lights = this._lights();
    const points = this._points(lights).map((p) => ({ h: p.h, ch: [...p.ch] }));
    this._draft = {
      points,
      moon: { ...((lights[0] && lights[0].moon) || {}) },
    };
    // Remember where the tank was before this session's edits, so Discard/Cancel
    // can put it back exactly - no persisted baseline involved.
    if (!this._session) {
      this._session = {
        points: points.map((p) => ({ h: p.h, ch: [...p.ch] })),
        moon: { ...((lights[0] && lights[0].moon) || {}) },
      };
    }
  }

  _onAct(act) {
    const toggle = (name) => { this._ui.sheet = this._ui.sheet === name ? null : name; };
    if (act === "sched") { this._ensureDraft(); toggle("sched"); this._ui.sel = 0; }
    else if (act === "moon") { this._ensureDraft(); toggle("moon"); }
    else if (act === "share") { toggle("share"); this._ui.shareMsg = ""; }
    else if (act === "save") { toggle("save"); }
    else if (act === "clock") { this._call("sync_clock", {}); }
    this._render();
  }

  _draftPointsSorted() { return [...this._draft.points].sort((a, b) => a.h - b.h); }

  _freeHour(want, self) {
    const taken = new Set(this._draft.points.filter((k) => k !== self).map((k) => k.h));
    const back = self.h > want ? 1 : -1;
    for (let d = 0; d < 24; d++) {
      for (const h of [want + back * d, want - back * d]) {
        if (h >= 0 && h <= 23 && !taken.has(h)) return h;
      }
    }
    return self.h;
  }

  _onPointTime(value) {
    if (!value) return;
    const p = this._draftPointsSorted()[this._ui.sel];
    p.h = this._freeHour(clampHour(this._hhToNum(value)), p);
    this._ui.sel = this._draftPointsSorted().indexOf(p);
    this._previewDraft(); this._render();
  }

  _hhToNum(v) { const [h] = String(v).split(":").map(Number); return h || 0; }

  _onPointAdd() {
    const P = this._draftPointsSorted();
    let gap = -1, at = 0;
    for (let i = 0; i < P.length - 1; i++) {
      const g = P[i + 1].h - P[i].h; if (g > gap) { gap = g; at = i; }
    }
    const h = Math.round((P[at].h + P[at + 1].h) / 2);
    if (P.some((k) => k.h === h)) return;
    const labels = this._labels(this._lights());
    this._draft.points.push({ h, ch: labels.map((_, ci) => channelCurve(this._draft.points, ci)[h]) });
    this._ui.sel = this._draftPointsSorted().findIndex((k) => k.h === h);
    this._previewDraft(); this._render();
  }

  _onPointDel() {
    if (this._draft.points.length <= 2) return;
    const p = this._draftPointsSorted()[this._ui.sel];
    this._draft.points = this._draft.points.filter((k) => k !== p);
    this._ui.sel = clamp(this._ui.sel, 0, this._draft.points.length - 1);
    this._previewDraft(); this._render();
  }

  _onCopyAll() {
    const src = this._draftPointsSorted()[this._ui.sel];
    this._draft.points.forEach((k) => {
      if (k !== src && !k.ch.every((v) => v === 0)) k.ch = [...src.ch];
    });
    this._previewDraft(); this._render();
  }

  _wireChannel(tr) {
    const ci = +tr.dataset.pci;
    const paint = (v) => {
      tr.querySelector(".fi").style.width = v + "%";
      tr.querySelector(".kn").style.left = v + "%";
      tr.parentElement.querySelector(".vv").textContent = v + "%";
      tr.setAttribute("aria-valuenow", v);
      this._redrawChart();
    };
    const at = (e) => {
      const b = tr.getBoundingClientRect();
      return clamp100(((e.clientX - b.left) / b.width) * 100);
    };
    tr.onpointerdown = (e) => {
      e.preventDefault(); e.stopPropagation();
      tr.setPointerCapture(e.pointerId);
      this._ui.dragging = true; this._ui.focusCh = ci;
      const k = this._draftPointsSorted()[this._ui.sel];
      k.ch[ci] = at(e); paint(k.ch[ci]);
      tr.onpointermove = (ev) => { k.ch[ci] = at(ev); paint(k.ch[ci]); };
    };
    tr.onpointerup = (e) => {
      tr.onpointermove = null;
      try { tr.releasePointerCapture(e.pointerId); } catch (_) {}
      this._ui.dragging = false; this._ui.focusCh = null;
      this._previewDraft(); this._render();
    };
    tr.onkeydown = (e) => {
      const d = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[e.key];
      if (d === undefined) return;
      e.preventDefault();
      const k = this._draftPointsSorted()[this._ui.sel];
      k.ch[ci] = clamp100(k.ch[ci] + d * (e.shiftKey ? 10 : 1));
      paint(k.ch[ci]);
      clearTimeout(this._kbTimer);
      this._kbTimer = setTimeout(() => this._previewDraft(), 400);
    };
  }

  _redrawChart() {
    const svg = this.shadowRoot.querySelector(".chart svg");
    if (!svg || !this._draft) return;
    const h = this._chartH();
    const labels = this._labels(this._lights());
    labels.forEach((lab, ci) => {
      const el = svg.querySelector(`[data-ch="${ci}"]`);
      if (el) {
        el.setAttribute("d", channelCurve(this._draft.points, ci).map((v, i) =>
          (i ? "L" : "M") + this._x(i).toFixed(1) + " " + this._y(v, h).toFixed(1)).join(" "));
      }
    });
    this._draftPointsSorted().forEach((k, i) => {
      const g = svg.querySelector(`[data-kfdot="${i}"]`);
      if (g) g.setAttribute("transform", `translate(${this._x(k.h).toFixed(1)},0)`);
    });
  }

  _wireChart() {
    const svg = this.shadowRoot.querySelector(".chart.editing svg");
    if (!svg || !this._draft) return;
    const toHour = (cx, b) => Math.round((((cx - b.left) / b.width * CHART_W) - PAD) / (CHART_W - 2 * PAD) * 24);
    svg.querySelectorAll("[data-kfdot]").forEach((dot) => {
      dot.onpointerdown = (e) => {
        e.preventDefault(); e.stopPropagation();
        const i = +dot.dataset.kfdot, p = this._draftPointsSorted()[i];
        this._ui.sel = i; this._ui.focusCh = null; this._ui.dragging = true;
        dot.setPointerCapture(e.pointerId);
        const b = svg.getBoundingClientRect();
        dot.onpointermove = (ev) => { p.h = this._freeHour(clampHour(toHour(ev.clientX, b)), p); this._redrawChart(); };
        dot.onpointerup = (ev) => {
          dot.onpointermove = null;
          try { dot.releasePointerCapture(ev.pointerId); } catch (_) {}
          this._ui.dragging = false; this._ui.sel = this._draftPointsSorted().indexOf(p);
          this._previewDraft(); this._render();
        };
      };
    });
    svg.onpointerdown = (e) => {
      const b = svg.getBoundingClientRect();
      const h = clampHour(toHour(e.clientX, b));
      if (this._draft.points.some((k) => k.h === h)) return;
      const labels = this._labels(this._lights());
      this._draft.points.push({ h, ch: labels.map((_, ci) => channelCurve(this._draft.points, ci)[h]) });
      this._ui.sel = this._draftPointsSorted().findIndex((k) => k.h === h);
      this._previewDraft(); this._render();
    };
  }

  _onSchedClose(which) {
    if (which === "cancel") {
      // Revert this session's edits to where the tank started - never a
      // persisted baseline. If nothing was previewed, this is a no-op.
      this._restoreSession();
    }
    this._draft = null; this._ui.sheet = null; this._render();
  }

  _onMoon(which) {
    this._ensureDraft();
    if (which === "toggle") {
      const on = !!(this._draft.moon.run || this._draft.moon.enabled);
      this._draft.moon.run = !on; this._draft.moon.enabled = !on;
      this._render(); return;
    }
    if (which === "apply") {
      const m = this._draft.moon;
      this._call("set_moon", {
        serial: this._serials(this._lights()),
        color: m.color || "#00A0E9", level: clamp100(m.level),
        start: this._moonStr(m.start), end: this._moonStr(m.end),
        enable: !!(m.run || m.enabled),
      });
    }
    // Moon is its own device timer; closing the sheet just drops the draft.
    this._draft = null; this._ui.sheet = null; this._render();
  }

  _onMoonTime(key, value) {
    this._ensureDraft();
    const [h, mm] = String(value).split(":").map(Number);
    this._draft.moon[key] = (h || 0) + (mm || 0) / 100;   // literal HH.MM
    this._render();
  }

  _onMoonLevel(d) {
    this._ensureDraft();
    this._draft.moon.level = clamp(clamp100(this._draft.moon.level) + d, 0, 100);
    this._render();
  }

  _onCommit(which) {
    if (which === "save") {
      // The current live state is what we want; just persist it as the baseline
      // and end the session. The lights already show it.
      this._call("save_settings", { serial: this._serials(this._lights()) });
      this._ui.editing = false;
      this._session = null;
    } else {
      // Discard = put the tank back to where this session started.
      this._restoreSession();
    }
    this._render();
  }

  _onSlot(i) {
    const slots = this._slots();
    if (slots[i] == null) { this._ui.sheet = "save"; this._render(); return; }
    // Tapping a preset VIEWS it - it does not touch the lights. Applying is a
    // deliberate button inside the sheet.
    this._ui.sheet = "preset";
    this._ui.presetSlot = i;
    this._ui.presetEdit = false;
    this._render();
  }

  _onPreset(which) {
    const i = this._ui.presetSlot;
    const labels = this._labels(this._lights());
    if (which === "apply") {
      this._call("apply_slot", { serial: this._serials(this._lights()), slot: i + 1 });
      this._ui.sheet = null;
    } else if (which === "edit") {
      this._ui.presetEdit = true;
      this._ui.presetDraft = this._slotLevels(i, labels);   // local copy
    } else if (which === "cancel") {
      this._ui.presetEdit = false;
    } else if (which === "save") {
      const levels = {};
      labels.forEach((lab, ci) => { levels[lab] = clamp100(this._ui.presetDraft[ci]); });
      const name = (this._slots()[i]) || `Preset ${i + 1}`;
      this._call("set_slot", { slot: i + 1, name, levels });   // pure data, lights untouched
      this._ui.presetEdit = false;
    } else if (which === "delete") {
      this._call("clear_slot", { slot: i + 1 });
      this._ui.sheet = null;
    } else if (which === "close") {
      this._ui.sheet = null;
    }
    this._render();
  }

  _wirePresetSlider(tr) {
    const ci = +tr.dataset.slci;
    const paint = (v) => {
      tr.querySelector(".fi").style.width = v + "%";
      tr.querySelector(".kn").style.left = v + "%";
      tr.parentElement.querySelector(".vv").textContent = v + "%";
      tr.setAttribute("aria-valuenow", v);
    };
    const at = (e) => {
      const b = tr.getBoundingClientRect();
      return clamp100(((e.clientX - b.left) / b.width) * 100);
    };
    tr.onpointerdown = (e) => {
      e.preventDefault(); e.stopPropagation();
      tr.setPointerCapture(e.pointerId);
      this._ui.dragging = true;
      this._ui.presetDraft[ci] = at(e); paint(this._ui.presetDraft[ci]);
      tr.onpointermove = (ev) => { this._ui.presetDraft[ci] = at(ev); paint(this._ui.presetDraft[ci]); };
    };
    tr.onpointerup = (e) => {
      tr.onpointermove = null;
      try { tr.releasePointerCapture(e.pointerId); } catch (_) {}
      this._ui.dragging = false;
      this._render();   // editing a preset never previews on the lights
    };
    tr.onkeydown = (e) => {
      const d = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[e.key];
      if (d === undefined) return;
      e.preventDefault();
      this._ui.presetDraft[ci] = clamp100(this._ui.presetDraft[ci] + d * (e.shiftKey ? 10 : 1));
      paint(this._ui.presetDraft[ci]);
    };
  }

  _onSaveSlot(v) {
    if (v !== "cancel") {
      const serials = this._serials(this._lights());
      // Read the name from the sheet's own input. window.prompt() is blocked in
      // the HA frontend, which silently ate the save before.
      const input = this.shadowRoot.querySelector("[data-savename]");
      const name = ((input && input.value) || "").trim() || `Preset ${+v + 1}`;
      this._call("save_slot", { serial: serials[0], slot: +v + 1, name });
    }
    this._ui.sheet = null; this._render();
  }

  // -- import / export ---------------------------------------------------

  _onShare(what) {
    const box = () => this.shadowRoot.querySelector("[data-sharebox]");
    if (what === "close") { this._ui.sheet = null; this._ui.shareMsg = ""; this._render(); return; }
    if (what === "export") {
      const lights = this._lights();
      const labels = this._labels(lights);
      const points = this._points(lights);
      const slots = this._slots();
      const doc = {
        kind: CFG_KIND, version: CFG_VERSION, channels: labels,
        name: this._config.name || "Reef tank",
        points: [...points].sort((a, b) => a.h - b.h).map((p) => ({
          hour: p.h, levels: Object.fromEntries(labels.map((l, i) => [l, clamp100(p.ch[i])])),
        })),
        slots: slots.map((n) => n ? { name: n, levels: {} } : null),
      };
      this._ui.shareText = JSON.stringify(doc, null, 1);
      this._ui.shareMsg = "Copied below — paste it to share";
      this._render();
      const t = box(); if (t) { t.focus(); t.select(); }
      return;
    }
    // import
    const text = (box() && box().value || "").trim();
    this._ui.shareText = text;
    let doc;
    try { doc = JSON.parse(text); }
    catch { this._ui.shareMsg = "⚠ That isn't valid JSON"; this._render(); return; }
    if (!doc || doc.kind !== CFG_KIND) { this._ui.shareMsg = "⚠ Not an AIPAI light config"; this._render(); return; }
    if (!(doc.version <= CFG_VERSION)) { this._ui.shareMsg = "⚠ Config is newer than this card understands"; this._render(); return; }
    if (!Array.isArray(doc.points) || !doc.points.length) { this._ui.shareMsg = "⚠ No time points in that config"; this._render(); return; }
    // Snapshot where the tank is now, so Discard can undo the import.
    if (!this._session) {
      const lights = this._lights();
      this._session = {
        points: this._points(lights).map((p) => ({ h: p.h, ch: [...p.ch] })),
        moon: { ...((lights[0] && lights[0].moon) || {}) },
      };
    }
    // Hand the raw config to the backend, which does the authoritative,
    // label-keyed import and reports channel mismatches.
    this._callResp("import_config", {
      serial: this._serials(this._lights()), config: text, apply: true,
    }).then((resp) => {
      const warns = [];
      (resp.lights || []).forEach((l) => (l.warnings || []).forEach((w) => warns.push(w)));
      this._ui.shareMsg = resp.ok
        ? "Imported" + (warns.length ? " — " + warns[0] : "")
        : "⚠ " + ((resp.lights && resp.lights[0] && resp.lights[0].error) || "Import failed");
      // An import is a live change to the tank - treat it like a preview session
      // so Save/Discard can keep or revert it.
      if (resp.ok) this._ui.editing = true;
      this._draft = null;
      this._render();
    }).catch(() => { this._ui.shareMsg = "⚠ Import failed"; this._render(); });
  }

  _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _style() {
    return `<style>
      :host { --primary: var(--primary-color, #03a9f4); }
      .card { background: var(--ha-card-background, var(--card-background-color, #fff));
        border-radius: var(--ha-card-border-radius, 12px); padding: 12px 14px 8px;
        box-shadow: var(--ha-card-box-shadow, none); color: var(--primary-text-color); }
      .empty { padding: 24px 8px; color: var(--secondary-text-color); text-align: center; }
      .hdr { display: flex; align-items: center; margin-bottom: 8px; }
      .ttl .n { font-weight: 600; font-size: 1.05rem; }
      .ttl .n.sm { font-size: .9rem; font-weight: 500; }
      .ttl .s { color: var(--secondary-text-color); font-size: .8rem; }
      .row { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
      .row.wrap { flex-wrap: wrap; }
      .grow { flex: 1; }
      .lbl { font-size: .78rem; color: var(--secondary-text-color); }
      .chip { font-size: .8rem; padding: 6px 12px; border-radius: 999px; cursor: pointer;
        background: var(--secondary-background-color); color: var(--primary-text-color); }
      .chip.on { background: var(--primary); color: #fff; }
      .chip.empty { border: 1px dashed var(--divider-color); background: transparent;
        color: var(--secondary-text-color); font-style: italic; }
      .chip.mini { padding: 5px 10px; font-size: .75rem; }
      .act, .btn { cursor: pointer; color: var(--primary); font-size: .8rem; }
      .btn { font-size: .72rem; letter-spacing: .03em; }
      .ftr { display: flex; gap: 16px; flex-wrap: wrap; padding: 8px 0 4px;
        border-top: 1px solid var(--divider-color); margin-top: 6px; }
      .chart { padding: 2px 0; } .chart svg { display: block; width: 100%; height: auto; }
      .chart.editing { touch-action: none; cursor: crosshair; }
      .chart.editing svg { background: var(--secondary-background-color); border-radius: 8px; }
      .axis { display: flex; justify-content: space-between; font-size: .65rem;
        color: var(--secondary-text-color); font-family: monospace; padding: 0 0 6px; }
      .sheet { background: var(--secondary-background-color); border-radius: 10px;
        padding: 10px 12px; margin: 6px 0; }
      .sheet .q { font-size: .82rem; font-weight: 600; margin-bottom: 8px; }
      .timein { font-family: monospace; font-size: .8rem; padding: 5px 8px; border-radius: 8px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); }
      .step { display: inline-flex; align-items: center; border: 1px solid var(--divider-color);
        border-radius: 999px; overflow: hidden; }
      .step button { border: none; background: transparent; color: var(--primary);
        width: 28px; height: 28px; cursor: pointer; font-size: .95rem; }
      .step .qty { font-family: monospace; font-size: .8rem; min-width: 48px; text-align: center; }
      .ptedit { border-top: 1px solid var(--divider-color); padding-top: 8px; margin-top: 4px; }
      .ch { display: grid; grid-template-columns: 62px 1fr 38px; align-items: center; gap: 10px; padding: 6px 0; }
      .ch .cn { font-size: .77rem; color: var(--secondary-text-color); white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis; }
      .ch .tr { height: 4px; border-radius: 999px; background: var(--divider-color);
        position: relative; cursor: pointer; touch-action: none; outline: none; }
      .ch .tr::after { content: ""; position: absolute; left: 0; right: 0; top: -8px; bottom: -8px; }
      .ch .tr:focus-visible .kn { box-shadow: 0 0 0 3px var(--primary); }
      .ch .tr.ro { cursor: default; } .ch .tr.ro::after { display: none; }
      .ch .fi { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; }
      .ch .kn { position: absolute; top: 50%; width: 13px; height: 13px; border-radius: 50%;
        transform: translate(-50%, -50%); box-shadow: 0 1px 3px rgba(0,0,0,.35); }
      .ch .vv { font-family: monospace; font-size: .72rem; text-align: right; color: var(--secondary-text-color); }
      .nameit { flex: 1; min-width: 140px; font-size: .85rem; padding: 6px 9px;
        border-radius: 8px; border: 1px solid var(--divider-color);
        background: var(--card-background-color); color: var(--primary-text-color); }
      .sharebox { width: 100%; min-height: 110px; font-family: monospace; font-size: .72rem;
        border: 1px solid var(--divider-color); border-radius: 8px;
        background: var(--card-background-color); color: var(--primary-text-color);
        padding: 8px; resize: vertical; box-sizing: border-box; }
      .lite { border-top: 1px solid var(--divider-color); }
      .card.sheeting .lite.affected { background: rgba(3,169,244,.06); }
      .literow { display: flex; align-items: center; padding: 8px 2px; }
      .rtag { font-size: .66rem; color: var(--primary); border: 1px solid var(--primary);
        border-radius: 999px; padding: 1px 7px; margin-left: 8px; }
      .unsaved { display: flex; align-items: center; gap: 10px; font-size: .8rem;
        background: rgba(255,170,0,.15); border-radius: 8px; padding: 8px 12px; margin: 6px 0; }
      .unsaved .sp { margin-left: auto; display: flex; gap: 8px; }
      .unsaved .b { cursor: pointer; border-radius: 999px; padding: 4px 12px; font-size: .76rem; font-weight: 600; }
      .unsaved .b.save2 { background: var(--primary); color: #fff; }
      .unsaved .b.discard { border: 1px solid var(--secondary-text-color); }
    </style>`;
  }
}

// --- visual config editor -------------------------------------------------
// A point-and-click editor so you never hand-type serials: tick which lights a
// card shows. Ticking all (or none explicitly) means "all lights, including any
// added later"; a subset pins the card to those serials.
class AipaiReefCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._built = false;
    this._lightKey = "";
  }

  setConfig(config) {
    this._config = Object.assign({}, config || {});
    if (!this._built) this._build();
    else this._sync();
  }

  set hass(hass) {
    this._hass = hass;
    const key = this._lightsKey();
    // Only rebuild when the set of available lights changes - never on every
    // hass tick, or a rebuild would steal focus while you're typing the name.
    if (!this._built || key !== this._lightKey) {
      this._lightKey = key;
      this._build();
    }
  }

  _discovered() {
    const out = [];
    if (this._hass) {
      for (const st of Object.values(this._hass.states)) {
        const a = st.attributes || {};
        if (a.aipai_kind === "schedule") {
          out.push({
            serial: String(a.aipai_serial),
            model: a.model || "",
            roads: a.roads || (a.labels || []).length,
          });
        }
      }
    }
    out.sort((x, y) => x.serial.localeCompare(y.serial));
    return out;
  }

  _lightsKey() {
    return this._discovered().map((l) => l.serial).join(",");
  }

  _rows() {
    const discovered = this._discovered();
    const known = new Set(discovered.map((l) => l.serial));
    const configured = Array.isArray(this._config.serials)
      ? this._config.serials.map(String) : null;
    // Show any configured-but-currently-missing serials too, so editing a card
    // for an offline light doesn't silently drop it.
    const extra = (configured || [])
      .filter((s) => !known.has(s))
      .map((s) => ({ serial: s, model: "", roads: 0, missing: true }));
    return [...discovered, ...extra];
  }

  _build() {
    const cfg = this._config;
    const configured = Array.isArray(cfg.serials) ? cfg.serials.map(String) : null;
    const rows = this._rows();
    const checked = (s) => (configured === null ? true : configured.includes(s));
    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

    this.shadowRoot.innerHTML = `
      <style>
        .ed { display: flex; flex-direction: column; gap: 12px; padding: 4px 2px; }
        .fld { display: flex; flex-direction: column; gap: 4px; }
        .fld > span { font-size: .8rem; color: var(--secondary-text-color); }
        input[type=text] { font-size: .95rem; padding: 8px 10px; border-radius: 8px;
          border: 1px solid var(--divider-color); background: var(--card-background-color);
          color: var(--primary-text-color); }
        .lbl { font-size: .8rem; color: var(--secondary-text-color); }
        .hint { font-size: .75rem; color: var(--secondary-text-color); }
        .row { display: flex; align-items: center; gap: 10px; padding: 6px 0;
          border-top: 1px solid var(--divider-color); cursor: pointer; }
        .row input { width: 18px; height: 18px; }
        .mono { font-family: monospace; }
        .muted { color: var(--secondary-text-color); font-size: .8rem; margin-left: auto; }
      </style>
      <div class="ed">
        <label class="fld"><span>Card name</span>
          <input type="text" data-name value="${esc(cfg.name || "")}" placeholder="e.g. Frag tank"></label>
        <div>
          <div class="lbl">Which lights?</div>
          <div class="hint" data-hint>${
            configured === null
              ? "All lights (new ones appear automatically)."
              : "Only the ticked lights."}</div>
        </div>
        ${rows.length
          ? rows.map((l) => `
          <label class="row">
            <input type="checkbox" data-serial="${esc(l.serial)}" ${checked(l.serial) ? "checked" : ""}>
            <span class="mono">${esc(l.serial)}</span>
            <span class="muted">${l.missing ? "(not found)" : esc(l.model) + (l.roads ? ` · ${l.roads} ch` : "")}</span>
          </label>`).join("")
          : `<div class="hint">No AIPAI lights found yet - add a light first.</div>`}
      </div>`;

    const name = this.shadowRoot.querySelector("[data-name]");
    name.addEventListener("input", () => {
      const v = name.value.trim();
      if (v) this._config.name = v; else delete this._config.name;
      this._emit();
    });
    this.shadowRoot.querySelectorAll("[data-serial]").forEach((cb) =>
      cb.addEventListener("change", () => this._onToggle()));
    this._built = true;
  }

  // Update values in place (no rebuild) so config-changed round-trips don't
  // steal focus from the name field.
  _sync() {
    const name = this.shadowRoot.querySelector("[data-name]");
    if (name && document.activeElement !== name && this.shadowRoot.activeElement !== name) {
      name.value = this._config.name || "";
    }
    const configured = Array.isArray(this._config.serials)
      ? this._config.serials.map(String) : null;
    this.shadowRoot.querySelectorAll("[data-serial]").forEach((cb) => {
      cb.checked = configured === null ? true : configured.includes(cb.dataset.serial);
    });
    const hint = this.shadowRoot.querySelector("[data-hint]");
    if (hint) {
      hint.textContent = configured === null
        ? "All lights (new ones appear automatically)."
        : "Only the ticked lights.";
    }
  }

  _onToggle() {
    const boxes = [...this.shadowRoot.querySelectorAll("[data-serial]")];
    const selected = boxes.filter((b) => b.checked).map((b) => b.dataset.serial);
    const discovered = this._discovered().map((l) => l.serial);
    const allSelected = discovered.length > 0 &&
      selected.length === discovered.length &&
      discovered.every((s) => selected.includes(s));
    if (allSelected) delete this._config.serials;   // "all", incl. future lights
    else this._config.serials = selected;
    this._sync();
    this._emit();
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: this._config }, bubbles: true, composed: true,
    }));
  }
}

if (!customElements.get("aipai-reef-card-editor")) {
  customElements.define("aipai-reef-card-editor", AipaiReefCardEditor);
}

if (!customElements.get("aipai-reef-card")) {
  customElements.define("aipai-reef-card", AipaiReefCard);
}

// Register in the card picker.
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "aipai-reef-card")) {
  window.customCards.push({
    type: "aipai-reef-card",
    name: "AIPAI Reef Light",
    description: "Schedule designer, presets and moonlight for AIPAI aquarium lights.",
    preview: false,
  });
}

console.info("%c AIPAI-REEF-CARD %c loaded ", "background:#03a9f4;color:#fff", "");
