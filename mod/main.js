// MCP Bridge (dev). Runs in the main menu and in game (every page load re-runs it).
// Publishes window.__sandustryMcp for the MCP server, which reaches it through the DevTools port
// (Runtime.evaluate). No port of its own, no network calls, nothing written to the save.
const VERSION = "0.3.0";
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

// Live mods (mod_reload). The game reads every mod's code once, at game start, and re-runs that
// copy on each page load. For the mods listed in localStorage[LIVE_KEY] ({id: {root, disabled}}),
// this swaps in the files as they are on disk now (or nothing, when disabled): main and worldgen
// entries when the game compiles them (new Function, tagged sourceURL=sandkit-workshop://<id>/<entry>;
// the bridge loads first, so every later mod goes through here), worker entries when the game hands
// them to the simulation workers. Manifests, assets and patches.json stay as read at game start.
const LIVE_KEY = "mcpBridge.liveMods";
const live = { mods: {}, status: {} };
bridge.live = live;
try { live.mods = JSON.parse(localStorage.getItem(LIVE_KEY) || "{}") || {}; } catch (e) { live.mods = {}; }

function readFresh(id, entry) {
  const mod = live.mods[id];
  if (mod.disabled) return "";
  const x = new XMLHttpRequest();
  x.open("GET", mod.root.replace(/\/?$/, "/") + entry, false);  // sync: the game compiles right after
  x.send();
  if (x.status !== 200 && x.status !== 0) throw new Error("HTTP " + x.status);
  return x.responseText;
}
function note(id, entry, what) {
  (live.status[id] = live.status[id] || {})[entry] = what;
  push(what.startsWith("error") ? "error" : "info", "mcp", ["live mod " + id + "/" + entry + ": " + what]);
}

if (Object.keys(live.mods).length && !window.Function.__mcpLive) {
  const NativeFunction = window.Function;
  const HEAD = "return (async () => {\n", TAIL = "\n})();\n//# sourceURL=";
  const rewrite = (args) => {
    if (args.length !== 2 || args[0] !== "__sandkit" || typeof args[1] !== "string") return args;
    const body = args[1], m = body.match(/\/\/# sourceURL=sandkit-workshop:\/\/([^/\s]+)\/(\S+)\s*$/);
    if (!m || !live.mods[m[1]]) return args;
    const a = body.indexOf(HEAD), b = body.lastIndexOf(TAIL);
    if (a < 0 || b < a) { note(m[1], m[2], "error: unknown wrapper, kept the game's copy"); return args; }
    try {
      const fresh = readFresh(m[1], m[2]);
      note(m[1], m[2], live.mods[m[1]].disabled ? "disabled" : "fresh from disk");
      return [args[0], body.slice(0, a + HEAD.length) + fresh + body.slice(b)];
    } catch (e) { note(m[1], m[2], "error: " + e.message + ", kept the game's copy"); return args; }
  };
  const Live = new Proxy(NativeFunction, {
    construct: (target, args) => Reflect.construct(target, rewrite(args)),
    apply: (target, self, args) => Reflect.apply(target, self, rewrite(args)),
    get: (target, key) => key === "__mcpLive" ? true : Reflect.get(target, key),
  });
  window.Function = Live;

  try {
    const sim = sandkit.state.environment.multithreading.simulation, load = sim.loadExternalModEntries;
    sim.loadExternalModEntries = function (state, entries) {
      const out = (entries || []).map(e => {
        if (!e || !live.mods[e.id]) return e;
        try {
          const source = readFresh(e.id, e.entry);
          note(e.id, e.entry, live.mods[e.id].disabled ? "disabled" : "fresh from disk");
          return Object.assign({}, e, { source });
        } catch (err) { note(e.id, e.entry, "error: " + err.message + ", kept the game's copy"); return e; }
      });
      return load.call(this, state, out);
    };
  } catch (e) { push("warn", "mcp", ["live mods: worker entries not hooked (" + e.message + ")"]); }
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
push("info", "mcp", ["bridge " + VERSION + " loaded, scene=" + bridge.scene() + ", url=" + location.search]);
