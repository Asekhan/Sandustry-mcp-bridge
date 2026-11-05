// MCP Bridge (dev). Runs in the main menu and in game (every page load re-runs it).
// Publishes window.__sandustryMcp for the MCP server, which reaches it through the DevTools port
// (Runtime.evaluate). No port of its own, no network calls, nothing written to the save.
const VERSION = "0.4.0";
const MAX_LOGS = 2000;

const prev = window.__sandustryMcp;
const logs = prev && Array.isArray(prev.logs) ? prev.logs : [];
let seq = prev && prev.seq ? prev.seq : 0;

function text(v) {
  if (typeof v === "string") return v;
  if (v instanceof Error) return v.stack || v.message;
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}
function push(level, source, args) {
  const msg = args.map(text).join(" ").slice(0, 4000);
  // A message repeated within the last 50 entries (a mod failing every frame) bumps a counter instead.
  for (let i = logs.length - 1; i >= 0 && i >= logs.length - 50; i--) {
    const l = logs[i];
    if (l.msg === msg && l.level === level) { l.n = (l.n || 1) + 1; l.last = Date.now(); return; }
  }
  logs.push({ seq: ++seq, t: Date.now(), level, source, msg });
  if (logs.length > MAX_LOGS) logs.splice(0, logs.length - MAX_LOGS);
  bridge.seq = seq;
}

// Console capture: wrap once per page (a reload gives a fresh console).
if (!console.__mcpWrapped) {
  for (const level of ["log", "info", "warn", "error", "debug"]) {
    const orig = console[level].bind(console);
    console[level] = (...args) => { try { push(level, "console", args); } catch (e) {} return orig(...args); };
  }
  console.__mcpWrapped = true;
  window.addEventListener("error", (ev) => push("error", "window", [ev.message, ev.filename + ":" + ev.lineno, ev.error || ""]));
  window.addEventListener("unhandledrejection", (ev) => push("error", "promise", [ev.reason]));
}

const bridge = {
  version: VERSION,
  loadedAt: Date.now(),
  logs,
  seq,
  sandkit,
  get state() { return sandkit.state; },
  get engine() { return sandkit.engine; },
  scene() {
    try { return sandkit.state.store.scene.active; } catch (e) { return null; }
  },
  log(level, ...args) { push(level, "mcp", args); },
  events: [],
};
window.__sandustryMcp = bridge;

// Fresh mod code on every page load (mod_reload). The game reads each mod's code once, at game
// start, and re-runs that copy on every page load. The bridge loads before every other mod and
// swaps in the files as they are on disk now, for every mod (local and Workshop): main and worldgen
// entries when the game compiles them (new Function, tagged sourceURL=sandkit-workshop://<id>/<entry>),
// worker entries when the game hands them to the simulation workers. Mods listed in this mod's
// disabled.json (written by mod_reload) get an empty entry instead (their code doesn't run). A file
// that can't be read keeps the game's copy. Manifests, assets, patches.json and new mods stay as
// read at game start.
const reload = { readMs: 0, mods: {}, disabled: [] };
bridge.reload = reload;
try { localStorage.removeItem("mcpBridge.liveMods"); localStorage.removeItem("mcpBridge.disabledMods"); } catch (e) {}  // earlier builds, no longer used
const roots = {};
try {
  const r = await window.electron.platform.workshop.getSandkitMods();
  for (const m of (r && r.ok && r.data && r.data.mods) || []) if (m && m.manifest && m.rootUrl) roots[m.manifest.id] = m.rootUrl;
} catch (e) { push("warn", "mcp", ["fresh mod code off: mod list unavailable (" + e.message + ")"]); }
const readFile = (url) => {
  const x = new XMLHttpRequest();
  x.open("GET", url, false);  // sync: the game compiles right after
  x.send();
  if (x.status !== 200 && x.status !== 0) throw new Error("HTTP " + x.status);
  return x.responseText;
};
try {
  const own = roots["asekhan.mcpbridge"];
  if (own) reload.disabled = JSON.parse(readFile(own.replace(/\/?$/, "/") + "disabled.json")).filter(x => typeof x === "string");
} catch (e) { reload.disabled = []; }  // no file: nothing disabled

function fresh(id, entry, old) {
  if (reload.disabled.includes(id)) { note(id, entry, "disabled"); return ""; }
  const t0 = performance.now();
  try {
    const text = readFile(roots[id].replace(/\/?$/, "/") + entry);
    note(id, entry, text === old ? "same" : "changed");
    return text;
  } catch (e) {
    note(id, entry, "error: " + e.message + "; ran the game-start copy");
    return old;
  } finally { reload.readMs += performance.now() - t0; }
}
function note(id, entry, what) {
  (reload.mods[id] = reload.mods[id] || {})[entry] = what;
  if (what !== "same") push(what.startsWith("error") ? "error" : "info", "mcp", ["mod code " + id + "/" + entry + ": " + what]);
}

if (Object.keys(roots).length && !window.Function.__mcpFresh) {
  const NativeFunction = window.Function;
  const HEAD = "return (async () => {\n", TAIL = "\n})();\n//# sourceURL=";
  const rewrite = (args) => {
    if (args.length !== 2 || args[0] !== "__sandkit" || typeof args[1] !== "string") return args;
    const body = args[1], m = body.match(/\/\/# sourceURL=sandkit-workshop:\/\/([^/\s]+)\/(\S+)\s*$/);
    if (!m || !roots[m[1]]) return args;
    const a = body.indexOf(HEAD), b = body.lastIndexOf(TAIL);
    if (a < 0 || b < a) { note(m[1], m[2], "error: unknown wrapper; ran the game-start copy"); return args; }
    const old = body.slice(a + HEAD.length, b);
    return [args[0], body.slice(0, a + HEAD.length) + fresh(m[1], m[2], old) + body.slice(b)];
  };
  window.Function = new Proxy(NativeFunction, {
    construct: (target, args) => Reflect.construct(target, rewrite(args)),
    apply: (target, self, args) => Reflect.apply(target, self, rewrite(args)),
    get: (target, key) => key === "__mcpFresh" ? true : Reflect.get(target, key),
  });
  try {
    const sim = sandkit.state.environment.multithreading.simulation, load = sim.loadExternalModEntries;
    sim.loadExternalModEntries = function (state, entries) {
      const out = (entries || []).map(e => e && roots[e.id] ? Object.assign({}, e, { source: fresh(e.id, e.entry, e.source) }) : e);
      return load.call(this, state, out);
    };
  } catch (e) { push("warn", "mcp", ["fresh mod code: worker entries not hooked (" + e.message + ")"]); }
}

// Record a few lifecycle events so the server can wait on them.
for (const name of ["game:ready", "game:started", "tutorial:completed"]) {
  try {
    sandkit.api.events.on(name, () => {
      bridge.events.push({ name, t: Date.now() });
      push("info", "event", [name]);
    });
  } catch (e) {}
}
// mod_reload reports once the game has run every mod (main and worker entries) for this world.
try { sandkit.api.events.on("mods:initialized", () => { reload.done = Date.now(); }); } catch (e) {}
push("info", "mcp", ["bridge " + VERSION + " loaded, scene=" + bridge.scene() + ", url=" + location.search]);
