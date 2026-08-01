/* Injects a "Clip" button on YouTube watch pages and a settings panel that
   drives the local clipper server. */

const DEFAULTS = {
  resolution: "1080",
  aspect: "16:9",
  fit: "bars",
  position: "top-left",
  margin_x: 4.5,
  margin_y: 4.5,
  transition: 0.6,
  credit_duration: 10,
  outline_width: 5,
  outline_color: "#ffffff",
  box_color: "#000000",
  box_opacity: 0.55,
  text_color: "#ffffff",
  card_scale: 0.9,
};

let settings = { ...DEFAULTS };
let host = null;      // shadow host element
let ui = null;        // shadow root
let serverBase = "";
let busy = false;

/* ------------------------------------------------------------ time helpers */

/* Digits are read right-to-left: 121 -> 1:21, 10245 -> 1:02:45. */
function maskTime(raw) {
  const [d, frac] = String(raw).split(".");
  const digits = (d || "").replace(/\D/g, "").slice(-6);
  if (!digits) return "";
  let out;
  if (digits.length <= 2) out = "0:" + digits.padStart(2, "0");
  else if (digits.length <= 4)
    out = digits.slice(0, -2).replace(/^0+(?=\d)/, "") + ":" + digits.slice(-2);
  else
    out =
      digits.slice(0, -4).replace(/^0+(?=\d)/, "") + ":" +
      digits.slice(-4, -2) + ":" + digits.slice(-2);
  return frac === undefined ? out : out + "." + frac.replace(/\D/g, "");
}

function toSeconds(text) {
  if (!text) return null;
  const parts = String(text).split(":").map(Number);
  if (parts.some(isNaN)) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

function fromSeconds(total) {
  const t = Math.max(0, total);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const ss = (s < 10 ? "0" : "") + s.toFixed(2).replace(/\.?0+$/, "");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

function playerTime() {
  const v = document.querySelector("video.html5-main-video") ||
            document.querySelector("#movie_player video") ||
            document.querySelector("video");
  return v && isFinite(v.currentTime) ? v.currentTime : null;
}

function watchUrl() {
  const id = new URLSearchParams(location.search).get("v");
  return id ? `https://www.youtube.com/watch?v=${id}` : location.href;
}

function isWatchPage() {
  return location.pathname === "/watch" &&
         new URLSearchParams(location.search).has("v");
}

/* ------------------------------------------------------------ the button */

const SCISSORS = `<svg viewBox="0 0 24 24"><path d="M9.64 7.64a3 3 0 1 0-1.41 1.41L10.59 11.4 8.23 13.76a3 3 0 1 0 1.41 1.41L12 12.81l6.5 6.5 1.41-1.41L9.64 7.64zM6.5 7a1 1 0 1 1 0-2 1 1 0 0 1 0 2zm0 12a1 1 0 1 1 0-2 1 1 0 0 1 0 2zM18.5 5l1.41 1.41-5.5 5.5-1.41-1.42L18.5 5z"/></svg>`;

function makeButton() {
  const b = document.createElement("button");
  b.className = "ytclip-btn";
  b.innerHTML = SCISSORS + "<span>Clip</span>";
  b.title = "Clip this section with a channel credit";
  b.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    togglePanel();
  });
  return b;
}

function mountButton() {
  if (!isWatchPage()) {
    document.querySelectorAll(".ytclip-btn").forEach((n) => n.remove());
    closePanel();
    return;
  }
  if (document.querySelector(".ytclip-btn")) return;

  const row =
    document.querySelector("#top-level-buttons-computed") ||
    document.querySelector("#actions #menu") ||
    document.querySelector("ytd-watch-metadata #actions");
  if (!row) return;
  row.appendChild(makeButton());
}

/* YouTube is a single-page app, so re-check after every navigation and
   whenever it re-renders the action row. */
document.addEventListener("yt-navigate-finish", () => setTimeout(mountButton, 300));
setInterval(mountButton, 1500);
mountButton();

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "toggle") togglePanel();
});

/* ------------------------------------------------------------ the panel */

function togglePanel() {
  if (host) return closePanel();
  openPanel();
}

function closePanel() {
  if (host) host.remove();
  host = null;
  ui = null;
  document.querySelectorAll(".ytclip-btn").forEach((b) =>
    b.removeAttribute("data-open")
  );
}

async function openPanel() {
  const stored = await chrome.storage.local.get("settings");
  settings = { ...DEFAULTS, ...(stored.settings || {}) };

  host = document.createElement("div");
  host.id = "ytclip-host";
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  document.body.appendChild(host);
  ui = host.attachShadow({ mode: "open" });
  ui.innerHTML = PANEL_HTML;
  document
    .querySelectorAll(".ytclip-btn")
    .forEach((b) => b.setAttribute("data-open", "1"));

  wirePanel();
  loadVideo();
}

const $ = (sel) => ui.querySelector(sel);

function wirePanel() {
  $("#close").addEventListener("click", closePanel);

  // start / end, with the player's current position one click away
  ["start", "end"].forEach((id) => {
    const el = $("#" + id);
    el.addEventListener("input", () => {
      if ((el.value.replace(/\D/g, "") || "").length >= 3)
        el.value = maskTime(el.value);
      updateLength();
    });
    el.addEventListener("blur", () => {
      el.value = maskTime(el.value);
      updateLength();
    });
    $("#now-" + id).addEventListener("click", () => {
      const t = playerTime();
      if (t === null) return;
      el.value = fromSeconds(t);
      updateLength();
      schedulePreview();
    });
  });

  // simple bound controls
  const bind = (sel, key, parse, fmt, label) => {
    const el = $(sel);
    el.value = settings[key];
    const sync = () => {
      settings[key] = parse(el.value);
      if (label) $(label).textContent = fmt(el.value);
      save();
      schedulePreview();
    };
    el.addEventListener("input", sync);
    el.addEventListener("change", sync);
    if (label) $(label).textContent = fmt(el.value);
  };

  const num = (v) => +v;
  bind("#transition", "transition", num, (v) => (+v).toFixed(2) + " s", "#transitionV");
  bind("#credit", "credit_duration", num, (v) => (+v).toFixed(1) + " s", "#creditV");
  bind("#marginX", "margin_x", num, (v) => +v + " %", "#marginXV");
  bind("#marginY", "margin_y", num, (v) => +v + " %", "#marginYV");
  bind("#ow", "outline_width", num, (v) => +v + " px", "#owV");
  bind("#op", "box_opacity", num, (v) => Math.round(v * 100) + " %", "#opV");
  bind("#cs", "card_scale", num, (v) => Math.round(v * 100) + " %", "#csV");
  bind("#oc", "outline_color", String, String);
  bind("#bc", "box_color", String, String);
  bind("#tc", "text_color", String, String);

  // resolution / aspect, each with a custom escape hatch
  ["resolution", "aspect"].forEach((key) => {
    const sel = $("#" + key);
    const custom = $("#" + key + "Custom");
    const preset = [...sel.options].some((o) => o.value === String(settings[key]));
    sel.value = preset ? String(settings[key]) : "custom";
    custom.value = settings[key];
    custom.style.display = preset ? "none" : "block";
    const sync = () => {
      const isCustom = sel.value === "custom";
      custom.style.display = isCustom ? "block" : "none";
      if (!isCustom) custom.value = sel.value;
      settings[key] = isCustom ? custom.value.trim() : sel.value;
      save();
      updateDims();
      schedulePreview();
    };
    sel.addEventListener("change", sync);
    custom.addEventListener("input", sync);
  });

  // segmented / corner pickers
  [["fit", "#fit"], ["position", "#position"]].forEach(([key, sel]) => {
    const group = $(sel);
    [...group.children].forEach((b) =>
      b.classList.toggle("on", b.dataset.v === String(settings[key]))
    );
    group.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      settings[key] = b.dataset.v;
      [...group.children].forEach((x) => x.classList.toggle("on", x === b));
      save();
      updateDims();
      schedulePreview();
    });
  });

  $("#go").addEventListener("click", render);
  updateDims();
}

function save() {
  chrome.storage.local.set({ settings });
}

function aspectRatio() {
  const t = String(settings.aspect).replace("/", ":").replace(",", ".");
  const r = t.includes(":")
    ? parseFloat(t.split(":")[0]) / parseFloat(t.split(":")[1])
    : parseFloat(t);
  return isFinite(r) && r >= 0.1 && r <= 10 ? r : null;
}

function updateDims() {
  const r = aspectRatio();
  const base = Math.round(+settings.resolution);
  const el = $("#dims");
  if (!r) return (el.textContent = "Aspect ratio must look like 16:9.");
  if (!(base >= 144 && base <= 4320))
    return (el.textContent = "Resolution must be between 144 and 4320.");
  let w, h;
  if (r >= 1) { h = base; w = Math.round(h * r); }
  else        { w = base; h = Math.round(w / r); }
  el.textContent = `${w - (w % 2)} × ${h - (h % 2)}`;
}

function updateLength() {
  const a = toSeconds($("#start").value);
  const b = toSeconds($("#end").value);
  const el = $("#len");
  if (a === null || b === null || !$("#start").value || !$("#end").value)
    el.textContent = "";
  else if (b <= a) el.textContent = "End must be after start";
  else el.textContent = `${(b - a).toFixed(2).replace(/\.?0+$/, "")} s clip`;
}

/* ------------------------------------------------------------ server calls */

function send(message) {
  return new Promise((resolve) =>
    chrome.runtime.sendMessage(message, (r) =>
      resolve(r || { ok: false, error: "Extension not reachable" })
    )
  );
}

let video = null;

async function loadVideo() {
  status("Reading video info…", "wait");
  const r = await send({ type: "info", url: watchUrl() });
  if (!r.ok) return status(r.error, "err");
  video = r.data;
  serverBase = r.base || serverBase;
  $("#vtitle").textContent = video.title;
  $("#vchannel").textContent = video.channel;
  if (video.avatar || video.thumbnail)
    $("#vavatar").src = video.avatar || video.thumbnail;
  status("");
  schedulePreview();
}

let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(renderPreview, 250);
}

async function renderPreview() {
  if (!ui) return;
  const body = {
    ...settings,
    title: video ? video.title : "Video title",
    channel: video ? video.channel : "Channel",
    avatar: video ? video.avatar || video.thumbnail : null,
  };
  const r = await send({ type: "preview", body });
  if (!ui || !r.ok || !r.data) return;
  const d = r.data;
  const img = $("#card");
  const stage = $("#stage");
  const ratio = aspectRatio() || 16 / 9;
  stage.style.aspectRatio = ratio + " / 1";
  img.src = d.png;
  img.style.display = "block";
  img.style.width = (d.width / d.video_width) * 100 + "%";
  img.style.left = (d.x / d.video_width) * 100 + "%";
  img.style.top = (d.y / d.video_height) * 100 + "%";
  playAnimation(d);
}

function playAnimation(d) {
  const img = $("#card");
  img.getAnimations().forEach((a) => a.cancel());
  const t = +settings.transition;
  const hold = +settings.credit_duration;
  const off = `translateX(-${100 + (d.x / d.width) * 100}%)`;
  if (t <= 0) return;
  const total = (t * 2 + hold) * 1000;
  img.animate(
    [
      { transform: off, offset: 0 },
      { transform: "translateX(0)", offset: t / (t * 2 + hold) },
      { transform: "translateX(0)", offset: (t + hold) / (t * 2 + hold) },
      { transform: off, offset: 1 },
    ],
    { duration: total, easing: "linear" }
  );
}

function status(text, kind) {
  const el = $("#status");
  el.className = kind || "";
  el.innerHTML = kind === "wait" ? `<span class="spin"></span>${text}` : text;
}

async function render() {
  if (busy) return;
  const a = toSeconds(maskTime($("#start").value));
  const b = toSeconds(maskTime($("#end").value));
  if (a === null || b === null) return status("Set a start and an end time.", "err");
  if (b <= a) return status("End time must be after start time.", "err");
  if (!aspectRatio()) return status("Aspect ratio must look like 16:9.", "err");

  busy = true;
  $("#go").disabled = true;
  const began = Date.now();
  const tick = setInterval(() => {
    status(`Rendering… ${((Date.now() - began) / 1000).toFixed(0)} s`, "wait");
  }, 500);
  status("Rendering…", "wait");

  const body = {
    ...settings,
    url: watchUrl(),
    range: maskTime($("#start").value) + "-" + maskTime($("#end").value),
  };
  const r = await send({ type: "render", body });
  clearInterval(tick);
  busy = false;
  if (!ui) return;
  $("#go").disabled = false;

  if (!r.ok) return status(r.error, "err");
  serverBase = r.base || serverBase;
  const url = `${serverBase}/clips/${r.data.file}`;
  status(`Done — ${r.data.width}×${r.data.height}, ${r.data.duration}s.`);
  const out = $("#result");
  out.style.display = "block";
  out.querySelector("video").src = url;
  const dl = out.querySelector("a");
  dl.href = url + "?download=1";
  dl.setAttribute("download", r.data.file);
}

/* ------------------------------------------------------------ markup */

const PANEL_HTML = `
<style>
  :host { all: initial; }
  * { box-sizing: border-box; }
  .panel {
    pointer-events: auto;
    position: fixed; top: 76px; right: 20px; width: 356px;
    max-height: calc(100vh - 100px); overflow-y: auto;
    background: #0f1216; color: #e9edf4; border: 1px solid #2a303b;
    border-radius: 14px; padding: 16px;
    font: 13px/1.45 "Roboto", -apple-system, Arial, sans-serif;
    box-shadow: 0 16px 48px rgba(0,0,0,.55);
  }
  h1 { margin: 0; font-size: 15px; font-weight: 600; letter-spacing: -.2px; }
  h1 span { color: #ff3b30; }
  .head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .x { background: none; border: 0; color: #8d97a8; font-size: 20px; cursor: pointer; line-height: 1; padding: 0 2px; }
  .x:hover { color: #e9edf4; }
  label { display: block; font-size: 11px; color: #8d97a8; margin-bottom: 5px; }
  input[type=text], input[type=number], select {
    width: 100%; background: #1d222b; border: 1px solid #2a303b; color: #e9edf4;
    border-radius: 8px; padding: 8px 10px; font-size: 13px; font-family: inherit; outline: none;
  }
  input:focus, select:focus { border-color: #ff3b30; }
  .field { margin-bottom: 11px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
  .row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 9px; }
  .timerow { display: flex; gap: 6px; }
  .timerow input { flex: 1; }
  .now {
    background: #1d222b; border: 1px solid #2a303b; color: #e9edf4; border-radius: 8px;
    padding: 0 9px; cursor: pointer; font-size: 12px; font-family: inherit; white-space: nowrap;
  }
  .now:hover { border-color: #ff3b30; color: #ff3b30; }
  .seg, .corners { background: #1d222b; border: 1px solid #2a303b; border-radius: 9px; padding: 3px; }
  .seg { display: flex; gap: 4px; }
  .corners { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
  .seg button, .corners button {
    background: none; border: 0; color: #8d97a8; padding: 7px 3px; border-radius: 6px;
    font-size: 11.5px; cursor: pointer; font-family: inherit; flex: 1;
  }
  .seg button.on, .corners button.on { background: #ff3b30; color: #fff; font-weight: 600; }
  .slider { display: flex; align-items: center; gap: 8px; }
  input[type=range] {
    flex: 1; -webkit-appearance: none; appearance: none; height: 4px;
    background: #2a303b; border-radius: 2px; outline: none; margin: 0;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%;
    background: #ff3b30; cursor: pointer; border: 0;
  }
  input[type=range]::-moz-range-thumb {
    width: 14px; height: 14px; border-radius: 50%; background: #ff3b30;
    cursor: pointer; border: 0;
  }
  input[type=range]::-moz-range-track { background: #2a303b; height: 4px; border-radius: 2px; }
  .val { min-width: 50px; text-align: right; font-variant-numeric: tabular-nums; font-size: 12px; }
  input[type=color] { width: 100%; height: 32px; padding: 2px; background: #1d222b; border: 1px solid #2a303b; border-radius: 8px; cursor: pointer; }
  .meta { display: flex; gap: 9px; align-items: center; margin-bottom: 11px; }
  .meta img { width: 34px; height: 34px; border-radius: 50%; background: #1d222b; object-fit: cover; }
  .meta b { display: block; font-size: 12.5px; font-weight: 600; }
  .meta small { color: #8d97a8; font-size: 11.5px; }
  .stage { position: relative; width: 100%; background: #000; border: 1px solid #2a303b; border-radius: 9px; overflow: hidden; margin-bottom: 11px; }
  #card { position: absolute; left: 0; top: 0; transform-origin: left center; display: none; }
  .go { width: 100%; background: #ff3b30; color: #fff; border: 0; border-radius: 9px; padding: 11px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; }
  .go:hover { background: #ff6b60; }
  .go:disabled { opacity: .5; cursor: default; }
  #status { min-height: 18px; margin-top: 9px; font-size: 12px; color: #8d97a8; text-align: center; }
  #status.err { color: #ff6b60; }
  .hint { font-size: 11px; color: #8d97a8; margin-top: 4px; }
  details { margin: 11px 0; border-top: 1px solid #2a303b; padding-top: 11px; }
  summary { cursor: pointer; font-size: 11px; color: #8d97a8; text-transform: uppercase; letter-spacing: .1em; outline: none; }
  #result { display: none; margin-top: 11px; }
  #result video { width: 100%; border-radius: 8px; background: #000; display: block; }
  #result a { display: block; text-align: center; margin-top: 8px; background: #1d222b; border: 1px solid #2a303b;
    color: #e9edf4; border-radius: 9px; padding: 9px; text-decoration: none; font-weight: 500; }
  #result a:hover { border-color: #ff3b30; }
  .spin { display: inline-block; width: 10px; height: 10px; border: 2px solid #2a303b; border-top-color: #ff3b30;
    border-radius: 50%; animation: sp .7s linear infinite; vertical-align: -1px; margin-right: 6px; }
  @keyframes sp { to { transform: rotate(360deg) } }
</style>
<div class="panel">
  <div class="head">
    <h1>YouTube <span>Clipper</span></h1>
    <button class="x" id="close" title="Close">&times;</button>
  </div>

  <div class="meta">
    <img id="vavatar" alt="">
    <div><b id="vtitle">…</b><small id="vchannel"></small></div>
  </div>

  <div class="row">
    <div class="field">
      <label>Start</label>
      <div class="timerow">
        <input type="text" id="start" placeholder="1:51" inputmode="numeric">
        <button class="now" id="now-start" title="Use the player's current time">Now</button>
      </div>
    </div>
    <div class="field">
      <label>End</label>
      <div class="timerow">
        <input type="text" id="end" placeholder="2:04" inputmode="numeric">
        <button class="now" id="now-end" title="Use the player's current time">Now</button>
      </div>
    </div>
  </div>
  <div class="hint" id="len"></div>

  <div class="stage" id="stage" style="aspect-ratio:16/9"><img id="card" alt=""></div>

  <div class="field">
    <label>Position</label>
    <div class="corners" id="position">
      <button data-v="top-left">Top left</button>
      <button data-v="top-right">Top right</button>
      <button data-v="bottom-left">Bottom left</button>
      <button data-v="bottom-right">Bottom right</button>
    </div>
  </div>

  <div class="field">
    <label>Transition duration</label>
    <div class="slider"><input type="range" id="transition" min="0" max="3" step="0.05"><span class="val" id="transitionV"></span></div>
  </div>
  <div class="field">
    <label>Credit on-screen duration</label>
    <div class="slider"><input type="range" id="credit" min="0.5" max="30" step="0.1"><span class="val" id="creditV"></span></div>
  </div>

  <details>
    <summary>Output &amp; style</summary>
    <div class="row" style="margin-top:11px">
      <div class="field">
        <label>Resolution</label>
        <select id="resolution">
          <option value="2160">2160p</option><option value="1440">1440p</option>
          <option value="1080">1080p</option><option value="720">720p</option>
          <option value="480">480p</option><option value="360">360p</option>
          <option value="custom">Custom…</option>
        </select>
        <input type="number" id="resolutionCustom" min="144" max="4320" step="2" style="display:none;margin-top:6px">
      </div>
      <div class="field">
        <label>Aspect ratio</label>
        <select id="aspect">
          <option value="16:9">16:9</option><option value="9:16">9:16</option>
          <option value="1:1">1:1</option><option value="4:5">4:5</option>
          <option value="4:3">4:3</option><option value="21:9">21:9</option>
          <option value="custom">Custom…</option>
        </select>
        <input type="text" id="aspectCustom" placeholder="2.39:1" style="display:none;margin-top:6px">
      </div>
    </div>
    <div class="field">
      <label>Aspect ratio fit</label>
      <div class="seg" id="fit">
        <button data-v="bars">Bars</button>
        <button data-v="zoom">Zoom</button>
        <button data-v="stretch">Stretch</button>
      </div>
      <div class="hint" id="dims"></div>
    </div>
    <div class="row">
      <div class="field">
        <label>Horizontal margin</label>
        <div class="slider"><input type="range" id="marginX" min="0" max="20" step="0.25"><span class="val" id="marginXV"></span></div>
      </div>
      <div class="field">
        <label>Vertical margin</label>
        <div class="slider"><input type="range" id="marginY" min="0" max="20" step="0.25"><span class="val" id="marginYV"></span></div>
      </div>
    </div>
    <div class="field">
      <label>Outline thickness</label>
      <div class="slider"><input type="range" id="ow" min="0" max="12" step="1"><span class="val" id="owV"></span></div>
    </div>
    <div class="row3">
      <div class="field"><label>Outline</label><input type="color" id="oc"></div>
      <div class="field"><label>Box</label><input type="color" id="bc"></div>
      <div class="field"><label>Text</label><input type="color" id="tc"></div>
    </div>
    <div class="field">
      <label>Box opacity</label>
      <div class="slider"><input type="range" id="op" min="0" max="1" step="0.01"><span class="val" id="opV"></span></div>
    </div>
    <div class="field">
      <label>Box size</label>
      <div class="slider"><input type="range" id="cs" min="0.5" max="1.8" step="0.05"><span class="val" id="csV"></span></div>
    </div>
  </details>

  <button class="go" id="go">Create clip</button>
  <div id="status"></div>

  <div id="result">
    <video controls></video>
    <a download>Download clip</a>
  </div>
</div>
`;
