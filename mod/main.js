// MCP Bridge (dev). Runs in the main menu and in game (every page load re-runs it).
// Publishes window.__sandustryMcp for the MCP server, which reaches it through the DevTools port
// (Runtime.evaluate). No port of its own, no network calls, nothing written to the save.
const VERSION = "0.2.0";
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
