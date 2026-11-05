"""Sandustry MCP server: drives the running game through its DevTools port. Standard library only.

The game is an Electron app; started with --remote-debugging-port it accepts Chrome DevTools
Protocol connections on 127.0.0.1. Every tool here is a Runtime.evaluate / Input / Page call on
the game page. The companion mod (../mod, id asekhan.mcpbridge) publishes the Sandkit object and
a log buffer on window.__sandustryMcp; without it only the window.electron / DOM tools work.

Run:   python -I sandustry_mcp.py            (MCP over stdio)
       python -I sandustry_mcp.py --install-mod
       python -I sandustry_mcp.py --call <tool> '<json args>'   (one-shot, for testing)
Env:   SANDUSTRY_EXE, SANDUSTRY_APPDATA, SANDUSTRY_DEVTOOLS_PORT
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MOD_SRC = os.path.join(HERE, "..", "mod")
MOD_ID = "asekhan.mcpbridge"
LEGACY_MOD_IDS = ("guillaume.mcpbridge",)  # 0.1.0 id; two copies would both load
PORT = int(os.environ.get("SANDUSTRY_DEVTOOLS_PORT", "9222"))


def find_game_exe():
    """SANDUSTRY_EXE, else Sandustry in any Steam library (Steam path from the registry, libraries from libraryfolders.vdf)."""
    if os.environ.get("SANDUSTRY_EXE"):
        return os.environ["SANDUSTRY_EXE"]
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.append(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        pass
    roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    libraries = []
    for root in roots:
        libraries.append(root)
        try:
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as fh:
                for line in fh:
                    parts = line.strip().split('"')
                    if len(parts) >= 4 and parts[1] == "path":
                        libraries.append(parts[3].replace("\\\\", "\\"))
        except OSError:
            pass
    for lib in libraries:
        exe = os.path.join(lib, "steamapps", "common", "Sandustry", "Sandustry.exe")
        if os.path.isfile(exe):
            return os.path.normpath(exe)
    return None


GAME_EXE = find_game_exe()
APPDATA = os.environ.get("SANDUSTRY_APPDATA", os.path.join(os.environ.get("APPDATA", ""), "sandustry"))
SAVES = os.path.join(APPDATA, "saves")
TRASH = os.path.join(SAVES, ".mcp-trash")
MAIN_LOG = os.path.join(APPDATA, "logs", "main.log")
SCENES = {1: "MainMenu", 2: "Intro", 3: "Deploy", 4: "Game"}
SERVER_VERSION = "0.4.1"


def log(*a):
    print("[sandustry-mcp]", *a, file=sys.stderr, flush=True)


class PageCrashed(Exception):
    """The game's page process is gone (Inspector.targetCrashed): nothing on it will ever answer."""


def page_crash_note():
    """The game's own record of its page dying: the last render-process-gone line in main.log, unless a page started
    after it (the game was restarted)."""
    try:
        with open(MAIN_LOG, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 200_000))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if "render-process-gone" in line:
            return line.strip()[:240]
        if "[renderer] startup" in line:
            return None
    return None


def crash_message(reason=""):
    note = page_crash_note()
    return ("The game page crashed" + (f" ({reason})" if reason else "") + ": nothing on it will answer. "
            + (f"main.log: {note}. " if note else "")
            + "An 'oom' reason means it ran out of memory. Restart it with game_launch restart:true (unsaved progress is lost).")


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- WebSocket + CDP

class WebSocket:
    """Minimal RFC 6455 client: text frames, fragmentation, ping/pong. No Origin header (CDP accepts that)."""

    def __init__(self, url, timeout=10):
        assert url.startswith("ws://")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("websocket handshake failed: " + head.decode(errors="replace")[:200])

    def _exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def send(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self, timeout):
        self.sock.settimeout(timeout)
        message = b""
        while True:
            b0, b1 = self._exact(2)
            fin, opcode, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            mask = self._exact(4) if b1 & 0x80 else None
            data = self._exact(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket closed by game")
            message += data
            if fin:
                return message.decode("utf-8", errors="replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    def __init__(self):
        self.ws = None
        self.next_id = 0
        self.target = None

    def targets(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
            return json.load(r)

    def available(self):
        try:
            return any(t.get("type") == "page" for t in self.targets())
        except OSError:
            return False

    def connect(self):
        if self.ws:
            return
        try:
            pages = [t for t in self.targets() if t.get("type") == "page" and not t.get("url", "").startswith("devtools:")]
        except OSError:
            raise ToolError(f"Game not reachable on DevTools port {PORT}. Start it with game_launch "
                            f"(it must run with --remote-debugging-port={PORT}).")
        if not pages:
            raise ToolError("DevTools port is open but the game has no page yet; retry in a moment.")
        self.target = pages[0]
        self.ws = WebSocket(self.target["webSocketDebuggerUrl"])
        # Inspector events tell us when the page process dies (the browser answers Inspector.enable even then, and
        # reports a page that is already dead). The game keeps running and accepts input while its window is unfocused.
        for method, params in (("Inspector.enable", {}), ("Emulation.setFocusEmulationEnabled", {"enabled": True})):
            try:
                self.call(method, params, timeout=5, retry=False)
            except ToolError:
                if not self.ws:
                    raise

    def drop(self):
        if self.ws:
            self.ws.close()
        self.ws = None

    def why_silent(self):
        note = page_crash_note()
        if note:
            return " " + crash_message()
        return " The page may be busy in a long script, or stuck; game_status says whether it still answers."

    def call(self, method, params=None, timeout=30, retry=True):
        """Every request has a hard deadline. A lost connection is retried once; no answer in time never is (a retry
        would only wait again, and a dead page never answers)."""
        for attempt in ((0, 1) if retry else (1,)):
            try:
                self.connect()
                self.next_id += 1
                mid = self.next_id
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                end = time.time() + timeout
                while True:
                    left = end - time.time()
                    if left <= 0:
                        raise socket.timeout()
                    msg = json.loads(self.ws.recv(left))
                    if msg.get("method") == "Inspector.targetCrashed":
                        raise PageCrashed()
                    if msg.get("method") == "Inspector.detached":
                        raise ConnectionError("detached: " + str(msg.get("params", {}).get("reason")))
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise ToolError(f"{method}: {msg['error'].get('message')}")
                        return msg.get("result", {})
            except PageCrashed:
                self.drop()
                raise ToolError(crash_message())
            except socket.timeout:
                self.drop()
                raise ToolError(f"{method}: no answer after {timeout:g}s." + self.why_silent())
            except (ConnectionError, OSError) as e:
                self.drop()
                if attempt:
                    raise ToolError(f"Lost the game connection ({e})." + self.why_silent())

    def evaluate(self, expression, timeout=30):
        r = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                           "returnByValue": True, "userGesture": True}, timeout=timeout + 2)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            exc = d.get("exception", {})
            raise ToolError("JS error: " + (exc.get("description") or exc.get("value") or d.get("text") or "unknown"))
        return r.get("result", {}).get("value")


cdp = CDP()

# Prelude for every game-side call: helpers and a cycle-safe, size-limited serializer.
PRELUDE = r"""
const mcp = window.__sandustryMcp || null;
const sk = mcp && mcp.sandkit, S = sk && sk.state, api = sk && sk.engine && sk.engine.api;
const need = () => { if (!mcp) throw new Error("MCP Bridge mod (asekhan.mcpbridge) is not loaded on this page"); };
const SCENES = {1: "MainMenu", 2: "Intro", 3: "Deploy", 4: "Game"};
const ser = (v, depth = 4, seen = new WeakSet()) => {
  if (v === null || v === undefined) return v === undefined ? null : v;
  const t = typeof v;
  if (t === "number") return Number.isFinite(v) ? v : String(v);
  if (t === "string") return v.length > 4000 ? v.slice(0, 4000) + "…" : v;
  if (t === "boolean") return v;
  if (t === "function") return "[function " + (v.name || "anonymous") + "]";
  if (t === "bigint" || t === "symbol") return String(v);
  if (v instanceof Error) return {error: v.message, stack: v.stack};
  if (typeof Element !== "undefined" && v instanceof Element) return "<" + v.tagName.toLowerCase() + (v.id ? "#" + v.id : "") + ">";
  if (seen.has(v)) return "[circular]";
  seen.add(v);
  if (ArrayBuffer.isView(v)) return "[" + v.constructor.name + " length=" + v.length + "]";
  if (depth <= 0) return Array.isArray(v) ? "[array " + v.length + "]" : "[object " + Object.keys(v).length + " keys]";
  if (Array.isArray(v) || v instanceof Set) {
    const a = Array.from(v), out = a.slice(0, 100).map(x => ser(x, depth - 1, seen));
    if (a.length > 100) out.push("… " + (a.length - 100) + " more");
    return out;
  }
  const src = v instanceof Map ? Object.fromEntries(v) : v, keys = Object.keys(src), out = {};
  for (const k of keys.slice(0, 80)) { try { out[k] = ser(src[k], depth - 1, seen); } catch (e) { out[k] = "[throws]"; } }
  if (keys.length > 80) out["…"] = (keys.length - 80) + " more keys";
  return out;
};
"""


def js(body, timeout=30):
    """Runs an async JS function body in the game page; `return` a value (serialized with ser())."""
    expr = "(async () => {" + PRELUDE + "\nconst __r = await (async () => {" + body + "\n})();\nreturn ser(__r, 6);\n})()"
    return cdp.evaluate(expr, timeout=timeout)


# The main menu is ready once its New/Continue buttons are on screen (the scene already says MainMenu
# during the splash). Labels come from the game's own translations, so any language works; the
# English words are the fallback when the bridge mod (and with it the i18n API) isn't loaded.
MENU_SHOWN = r"""
const menuShown = () => {
  const labels = new Set(["new", "continue"]);
  try { for (const k of ["ui|menu|new", "ui|menu|continue"]) labels.add(String(api.i18n.t(k)).trim().toLowerCase()); } catch (e) {}
  return Array.from(document.querySelectorAll(".cursor-pointer")).some(e =>
    e.getBoundingClientRect().width > 0 && labels.has((e.innerText || "").trim().toLowerCase()));
};
"""


def page_state():
    try:
        return js(MENU_SHOWN + """
          let scene = null; try { scene = SCENES[S.store.scene.active] || S.store.scene.active; } catch (e) {}
          return {url: location.search, bridge: mcp ? mcp.version : null, loadedAt: mcp ? mcp.loadedAt : null, scene,
                  readyState: document.readyState, menu: scene === "MainMenu" && menuShown()};
        """, timeout=5)
    except ToolError:
        return None


def wait_until(pred, timeout, what):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = page_state()
        except ToolError:
            last = None
        if last and pred(last):
            return last
        time.sleep(0.5)
    raise ToolError(f"Timed out after {timeout}s waiting for {what}. Last page state: {json.dumps(last)}")


def navigate(search, timeout):
    """Changes location.search (the game's own way of switching scenes), then waits for the new page's bridge."""
    before = page_state()
    t0 = time.time() * 1000
    cdp.evaluate("(() => { const u = new URL(location.href); u.search = " + json.dumps(search) +
                 "; setTimeout(() => { location.href = u.toString(); }, 50); return true; })()")
    time.sleep(1.0)
    has_bridge = bool(before and before.get("bridge"))
    return wait_until(lambda s: (s.get("loadedAt") or 0) > t0 if has_bridge else s.get("readyState") == "complete",
                      timeout, "the page to reload")


# ---------------------------------------------------------------- saves (disk side, no game needed)

def read_saves():
    out = []
    if not os.path.isdir(SAVES):
        return out
    for f in os.listdir(SAVES):
        if not f.endswith(".save"):
            continue
        p = os.path.join(SAVES, f)
        meta = {"id": f[:-5]}
        try:
            with open(p, "rb") as fh:
                line = fh.read(4096).split(b"\n", 1)[0]
            meta.update(json.loads(line.decode("utf-8")))
        except (OSError, ValueError):
            meta["unreadable"] = True
        meta["mtime"] = os.path.getmtime(p)
        meta["sizeMB"] = round(os.path.getsize(p) / 1e6, 1)
        out.append(meta)
    out.sort(key=lambda m: m["mtime"], reverse=True)
    return out


def resolve_save(ref):
    saves = read_saves()
    for m in saves:
        if m["id"] == ref:
            return m
    hits = [m for m in saves if ref.lower() in str(m.get("name", "")).lower() or ref.lower() == str(m.get("worldName", "")).lower()]
    if not hits:
        raise ToolError(f"No save matches {ref!r}. Use saves_list.")
    if len({m['id'] for m in hits}) > 1 and not any(str(m.get("name", "")).lower() == ref.lower() for m in hits):
        return hits[0]  # most recent match
    exact = [m for m in hits if str(m.get("name", "")).lower() == ref.lower()]
    return (exact or hits)[0]


# ---------------------------------------------------------------- process

def game_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Sandustry.exe", "/NH"], capture_output=True, text=True, timeout=10).stdout
        return "Sandustry.exe" in out
    except (OSError, subprocess.SubprocessError):
        return False


def kill_game():
    subprocess.run(["taskkill", "/IM", "Sandustry.exe", "/F"], capture_output=True, timeout=15)
    for _ in range(20):
        if not game_running():
            return
        time.sleep(0.5)


# ---------------------------------------------------------------- input

KEYS = {
    "space": (" ", "Space", 32), "enter": ("Enter", "Enter", 13), "escape": ("Escape", "Escape", 27), "esc": ("Escape", "Escape", 27),
    "tab": ("Tab", "Tab", 9), "backspace": ("Backspace", "Backspace", 8), "delete": ("Delete", "Delete", 46),
    "shift": ("Shift", "ShiftLeft", 16), "control": ("Control", "ControlLeft", 17), "ctrl": ("Control", "ControlLeft", 17),
    "alt": ("Alt", "AltLeft", 18), "left": ("ArrowLeft", "ArrowLeft", 37), "up": ("ArrowUp", "ArrowUp", 38),
    "right": ("ArrowRight", "ArrowRight", 39), "down": ("ArrowDown", "ArrowDown", 40),
}
MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}


def key_spec(name):
    n = name.strip()
    low = n.lower()
    if low in KEYS:
        return KEYS[low]
    if low.startswith("arrow") and low[5:] in KEYS:
        return KEYS[low[5:]]
    if len(n) == 4 and n.startswith("Key"):
        n = n[3]
    if len(n) == 6 and n.startswith("Digit"):
        n = n[5]
    if len(n) == 1 and n.isalpha():
        return (n.lower(), "Key" + n.upper(), ord(n.upper()))
    if len(n) == 1 and n.isdigit():
        return (n, "Digit" + n, ord(n))
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 12:
        return (n.upper(), n.upper(), 111 + int(low[1:]))
    raise ToolError(f"Unknown key {name!r}. Use a letter, digit, F1-F12, or one of: {', '.join(sorted(KEYS))}.")


def key_event(kind, spec, modifiers=0):
    key, code, vk = spec
    cdp.call("Input.dispatchKeyEvent", {"type": kind, "key": key, "code": code, "windowsVirtualKeyCode": vk,
                                        "nativeVirtualKeyCode": vk, "modifiers": modifiers})


def press_combo(combo, hold_ms):
    """combo like "w", "ctrl+s", "shift+KeyD". Holds all keys for hold_ms, releases in reverse."""
    specs = [key_spec(p) for p in combo.split("+")]
    mods = 0
    for s in specs:
        key_event("rawKeyDown", s, mods)
        mods |= MODIFIER_BITS.get(s[0], 0)
    if hold_ms:
        time.sleep(hold_ms / 1000)
    for s in reversed(specs):
        mods &= ~MODIFIER_BITS.get(s[0], 0)
        key_event("keyUp", s, mods)


# ---------------------------------------------------------------- tools

TOOLS = {}


def tool(name, description, props=None, required=()):
    def deco(fn):
        TOOLS[name] = {"fn": fn, "schema": {"name": name, "description": description, "inputSchema": {
            "type": "object", "properties": props or {}, "required": list(required)}}}
        return fn
    return deco


@tool("game_status", "Is the game running and reachable; current scene (MainMenu/Intro/Deploy/Game), URL, bridge mod version, world, player position.")
def t_status(a):
    if not cdp.available():
        return {"running": game_running(), "devtools": False,
                "hint": "Not reachable. game_launch starts it with the DevTools port (restart:true if it is already running without it)."}
    try:
        page = js("""
      const out = {url: location.search || "(main menu)", bridge: mcp ? mcp.version : null};
      try { out.scene = SCENES[S.store.scene.active] || S.store.scene.active; } catch (e) {}
      try { out.world = {id: S.store.meta.worldId, name: S.store.meta.worldName || S.store.meta.name, seed: S.store.meta.seed}; } catch (e) {}
      try { const p = S.store.player; out.player = {x: Math.round(p.x), y: Math.round(p.y), cellX: Math.floor(p.x / 4), cellY: Math.floor(p.y / 4)}; } catch (e) {}
      try { out.tutorial = {active: S.store.tutorial.active, step: S.store.tutorial.currentStep}; } catch (e) {}
      out.logSeq = mcp ? mcp.seq : null;
      return out;
    """, timeout=5) or {}
    except ToolError as e:
        # Answer anyway, fast: the page is listed but does not respond (crashed, or busy in a long script)
        return {"running": True, "devtools": True, "responding": False, "crashed": page_crash_note() is not None, "error": str(e)}
    return {"running": True, "devtools": True, **page}


@tool("game_launch", "Start Sandustry with the DevTools port open (direct exe launch; Steam must be running). Waits for the main menu. restart:true kills a running game first (unsaved progress is lost).",
      {"restart": {"type": "boolean", "default": False}, "timeout_s": {"type": "number", "default": 120}})
def t_launch(a):
    if cdp.available() and not a.get("restart"):
        return {"already": True, **t_status({})}
    if game_running():
        if not a.get("restart"):
            raise ToolError("Sandustry is running without the DevTools port. Call game_launch with restart:true (unsaved progress is lost) or quit it first.")
        cdp.drop()
        kill_game()
    if not GAME_EXE or not os.path.isfile(GAME_EXE):
        raise ToolError(f"Sandustry.exe not found (looked in the Steam libraries; got {GAME_EXE}). Set SANDUSTRY_EXE to its full path.")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([GAME_EXE, f"--remote-debugging-port={PORT}"], cwd=os.path.dirname(GAME_EXE), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    end = time.time() + float(a.get("timeout_s", 120))
    while time.time() < end and not cdp.available():
        time.sleep(1)
    if not cdp.available():
        raise ToolError("The game started but the DevTools port never opened (did Steam relaunch it without the flag?).")
    st = wait_until(lambda s: s.get("menu"), max(5, end - time.time()), "the main menu buttons")
    return {"launched": True, **st}


@tool("game_quit", "Quit the game (no save). force:true kills the process.", {"force": {"type": "boolean", "default": False}})
def t_quit(a):
    if not a.get("force") and cdp.available():
        try:
            cdp.evaluate("window.electron.appQuit(), true", timeout=5)
        except ToolError:
            pass
        cdp.drop()
        for _ in range(20):
            if not game_running():
                return {"quit": True}
            time.sleep(0.5)
    kill_game()
    cdp.drop()
    return {"quit": True, "forced": True}


@tool("saves_list", "List save files (read from disk; works without the game). Newest first.",
      {"filter": {"type": "string", "description": "substring of name/world/id"}, "include_autosaves": {"type": "boolean", "default": True},
       "limit": {"type": "integer", "default": 30}})
def t_saves(a):
    f = (a.get("filter") or "").lower()
    rows = []
    for m in read_saves():
        if not a.get("include_autosaves", True) and "autosave" in m["id"]:
            continue
        if f and f not in (m["id"] + " " + str(m.get("name", "")) + " " + str(m.get("worldName", ""))).lower():
            continue
        rows.append({"id": m["id"], "name": m.get("name"), "world": m.get("worldName"), "saved": m.get("timestamp"),
                     "playH": round(m.get("playTime", 0) / 3.6e6, 1), "factoryLevel": m.get("factoryLevel"), "sizeMB": m["sizeMB"]})
    return {"folder": SAVES, "count": len(rows), "saves": rows[: int(a.get("limit", 30))]}


@tool("save_load", "Load a save by id or name (navigates the game like the Load window does) and wait until the world runs.",
      {"save": {"type": "string"}, "timeout_s": {"type": "number", "default": 120}}, ["save"])
def t_load(a):
    m = resolve_save(a["save"])
    navigate("?db_load=" + m["id"], float(a.get("timeout_s", 120)))
    st = wait_until(lambda s: s.get("scene") in ("Game", "Intro", "Deploy") or not s.get("bridge"), float(a.get("timeout_s", 120)), "the world")
    return {"loaded": m["id"], "name": m.get("name"), **st}


@tool("save_game", "Save the running world. Without id: a new save named `name`. With id: overwrite that save.",
      {"name": {"type": "string"}, "id": {"type": "string"}})
def t_save(a):
    name = a.get("name") or "MCP save " + time.strftime("%H:%M:%S")
    return js("need(); const id = api.game.save(S, " + json.dumps(name) + ", " + json.dumps(a.get("id")) + " || undefined);"
              "await new Promise(r => setTimeout(r, 1500)); return {id, name: " + json.dumps(name) + ", saving: !!S.session.saving};")


@tool("save_trash", "Move a save (and its .backup) to saves/.mcp-trash (reversible; nothing is permanently deleted). Needs confirm = the exact save id.",
      {"save": {"type": "string"}, "confirm": {"type": "string", "description": "repeat the exact save id"}}, ["save", "confirm"])
def t_trash(a):
    m = resolve_save(a["save"])
    if a.get("confirm") != m["id"]:
        raise ToolError(f"'{a['save']}' resolves to id {m['id']} ({m.get('name')}). Call again with confirm={m['id']!r}.")
    os.makedirs(TRASH, exist_ok=True)
    moved = []
    for suffix in (".save", ".save.backup"):
        src = os.path.join(SAVES, m["id"] + suffix)
        if os.path.exists(src):
            dst = os.path.join(TRASH, m["id"] + suffix)
            if os.path.exists(dst):
                dst += "." + time.strftime("%Y%m%d-%H%M%S")
            shutil.move(src, dst)
            moved.append(dst)
    return {"trashed": m["id"], "name": m.get("name"), "moved_to": moved,
            "note": "Steam Cloud may still hold a copy; restore by moving the file back into saves/."}


@tool("main_menu", "Go back to the main menu WITHOUT saving (reloads the page with no world).", {"timeout_s": {"type": "number", "default": 60}})
def t_menu(a):
    navigate("", float(a.get("timeout_s", 60)))
    return wait_until(lambda s: s.get("menu"), float(a.get("timeout_s", 60)), "the main menu buttons")


@tool("new_world", "Start a new world. By default skips the story intro, the opening story dialogue and the tutorial (normal starting unlocks are granted). Other mods' popups are left alone (ui_click them).",
      {"seed": {"type": "string"}, "name": {"type": "string"}, "skip_intro": {"type": "boolean", "default": True},
       "skip_tutorial": {"type": "boolean", "default": True}, "timeout_s": {"type": "number", "default": 180}})
def t_new(a):
    seed = a.get("seed") or str(int(time.time()))
    name = a.get("name") or "MCP " + time.strftime("%m-%d %H%M")
    timeout = float(a.get("timeout_s", 180))
    from urllib.parse import quote
    navigate(f"?new_game=true&seed={quote(seed)}&name={quote(name)}", timeout)
    st = wait_until(lambda s: s.get("scene") in ("Intro", "Deploy", "Game") or not s.get("bridge"), timeout, "the new world")
    out = {"seed": seed, "name": name, "scene": st.get("scene")}
    if a.get("skip_intro", True) and st.get("scene") == "Intro":
        out["intro_skip"] = t_skip_intro({})
        st = wait_until(lambda s: s.get("scene") == "Game", 60, "the game after skipping the intro")
    if a.get("skip_tutorial", True):
        if st.get("scene") != "Game":
            st = wait_until(lambda s: s.get("scene") == "Game", 120, "the game scene")
        out["tutorial_skip"] = t_skip_tutorial({})
    if a.get("skip_intro", True):
        out["story_skip"] = t_skip_story({"wait_s": 4})
    out["status"] = t_status({})
    return out


@tool("skip_intro", "Skip the story intro (mission brief cinematic and its dialogue) by pressing the game's own Skip Intro button; falls back to the API when the button never shows.",
      {"wait_s": {"type": "number", "default": 15}})
def t_skip_intro(a):
    # The button also stops the intro music and clears queued story notifications; game.start(skip) alone leaves those.
    return js("""need(); const before = SCENES[S.store.scene.active], end = Date.now() + """ + str(int(float(a.get("wait_s", 15)) * 1000)) + """;
      const btn = () => Array.from(document.querySelectorAll("button")).find(b => /^skip intro$/i.test((b.innerText || "").trim()));
      let how = null;
      while (Date.now() < end) {
        const b = btn();
        if (b) { b.click(); how = "button"; await new Promise(r => setTimeout(r, 800)); if (!btn()) break; }
        else if (how) break;
        await new Promise(r => setTimeout(r, 300));
      }
      if (!how) { api.game.start(S, {skip: true}); try { S.session.notifications.story = []; } catch (e) {} how = "api"; }
      await new Promise(r => setTimeout(r, 300));
      return {how, before, after: SCENES[S.store.scene.active]};""", timeout=float(a.get("wait_s", 15)) + 10)


@tool("skip_story", "Dismiss story dialogue boxes (ZOE messages). Watches for wait_s seconds and clears any that arrive; story objectives keep tracking.",
      {"wait_s": {"type": "number", "default": 5}})
def t_skip_story(a):
    wait = float(a.get("wait_s", 5))
    return js("""need(); const end = Date.now() + """ + str(int(wait * 1000)) + """, cleared = [];
      while (true) {
        const q = S.session.notifications.story;
        if (q && q.length) { cleared.push(...q.map(n => n.stepId || n.id)); S.session.notifications.story = []; api.ui.update(S, 21); }
        if (Date.now() >= end) break;
        await new Promise(r => setTimeout(r, 250));
      }
      return {cleared};""", timeout=wait + 10)


@tool("skip_tutorial", "End the tutorial the way its Skip Tutorial button does (grants the normal starting unlocks).",
      {"grant_unlocks": {"type": "boolean", "default": True}})
def t_skip_tutorial(a):
    grant = "true" if a.get("grant_unlocks", True) else "false"
    return js("need(); const was = S.store.tutorial.active;"
              "const ok = api.progression.complete(S, {domain: 'tutorial', grantNormalUnlocks: " + grant + "});"
              "return {wasActive: was, completed: ok, active: S.store.tutorial.active};")


UI_COLLECT = r"""
const want = ARGS;
const vis = (el) => { const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return null;
  const cs = getComputedStyle(el); if (cs.visibility === "hidden" || cs.display === "none" || +cs.opacity === 0) return null;
  if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) return null; return r; };
const label = (el) => (el.innerText || el.value || el.getAttribute("aria-label") || el.title || el.getAttribute("alt") || el.placeholder || "").replace(/\s+/g, " ").trim();
const sel = 'button,a,input,select,textarea,[role=button],[role=tab],[role=menuitem],[id^="main-menu"],[onclick],[tabindex]';
// Document order: native controls, plus the outermost element of each pointer-cursor region (the game's menu buttons are divs).
const cands = [];
for (const el of document.querySelectorAll("body *")) {
  if (el.matches(sel)) { cands.push(el); continue; }
  if (!/^(DIV|SPAN|LI|IMG)$/.test(el.tagName)) continue;
  if (getComputedStyle(el).cursor === "pointer" && !(el.parentElement && getComputedStyle(el.parentElement).cursor === "pointer")) cands.push(el);
}
const items = [];
for (const el of cands) {
  const r = vis(el); if (!r) continue;
  const text = label(el).slice(0, 80);
  if (!text && !el.id) continue;
  items.push({el, text, id: el.id || undefined, tag: el.tagName.toLowerCase(), disabled: !!el.disabled || undefined,
              x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)});
}
const norm = (s) => (s || "").toLowerCase();
const match = (it) => (!want.id || it.id === want.id) && (!want.text || (want.exact ? norm(it.text) === norm(want.text) : norm(it.text).includes(norm(want.text))))
  && (!want.filter || norm(it.text + " " + (it.id || "")).includes(norm(want.filter)));
let found = want.selector ? Array.from(document.querySelectorAll(want.selector)).filter(vis).map(el => ({el, text: label(el).slice(0, 80), id: el.id || undefined, tag: el.tagName.toLowerCase(),
  x: Math.round(vis(el).left + vis(el).width / 2), y: Math.round(vis(el).top + vis(el).height / 2)})) : items.filter(match);
// Prefer the innermost element when a parent and child carry the same text.
found = found.filter(a => !found.some(b => b !== a && a.el.contains(b.el) && b.text === a.text));
"""


@tool("ui_list", "List visible clickable UI elements (buttons, links, inputs, pointer-cursor items) with text, id and centre position.",
      {"filter": {"type": "string"}, "limit": {"type": "integer", "default": 80}})
def t_ui_list(a):
    args = json.dumps({"filter": a.get("filter")})
    return js(UI_COLLECT.replace("ARGS", args) + f"""
      return {{count: found.length, viewport: [innerWidth, innerHeight],
        items: found.slice(0, {int(a.get('limit', 80))}).map(({{el, ...rest}}) => rest)}};""")


@tool("ui_click", "Click a UI element by visible text (substring, case-insensitive), element id, or CSS selector. Uses a real mouse click at its centre unless dom:true.",
      {"text": {"type": "string"}, "id": {"type": "string"}, "selector": {"type": "string"}, "exact": {"type": "boolean", "default": False},
       "index": {"type": "integer", "default": 0, "description": "which match when several"}, "dom": {"type": "boolean", "default": False}})
def t_ui_click(a):
    if not (a.get("text") or a.get("id") or a.get("selector")):
        raise ToolError("Give text, id or selector.")
    args = json.dumps({k: a.get(k) for k in ("text", "id", "selector", "exact")})
    idx = int(a.get("index", 0))
    res = js(UI_COLLECT.replace("ARGS", args) + f"""
      const all = found.map(({{el, ...rest}}) => rest);
      if (!found.length) return {{clicked: false, candidates: items.slice(0, 40).map(({{el, ...rest}}) => rest.text || rest.id)}};
      const it = found[Math.min({idx}, found.length - 1)];
      if ({'true' if a.get('dom') else 'false'}) it.el.click();
      return {{clicked: true, target: {{text: it.text, id: it.id, tag: it.tag, x: it.x, y: it.y}}, matches: all.length}};""")
    if not res.get("clicked"):
        raise ToolError("No visible element matches. Visible: " + json.dumps(res.get("candidates")))
    if not a.get("dom"):
        x, y = res["target"]["x"], res["target"]["y"]
        mouse(x, y, "left", "click", 60)
    time.sleep(0.3)
    return res


@tool("ui_type", "Type text into the focused field, or into the field matching selector (clears it first when clear:true).",
      {"text": {"type": "string"}, "selector": {"type": "string"}, "clear": {"type": "boolean", "default": False}}, ["text"])
def t_ui_type(a):
    if a.get("selector"):
        js("const el = document.querySelector(" + json.dumps(a["selector"]) + "); if (!el) throw new Error('no element'); el.focus();"
           + ("el.select && el.select();" if a.get("clear") else "") + "return true;")
    cdp.call("Input.insertText", {"text": a["text"]})
    return {"typed": len(a["text"])}


def mouse(x, y, button, action, hold_ms):
    base = {"x": x, "y": y, "button": button, "clickCount": 1}
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    if action in ("click", "down"):
        cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
    if action == "click":
        time.sleep(hold_ms / 1000)
    if action in ("click", "up"):
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})


@tool("mouse", "Mouse event at viewport CSS pixel coordinates (see ui_list / screenshot for positions).",
      {"x": {"type": "number"}, "y": {"type": "number"}, "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
       "action": {"type": "string", "enum": ["click", "down", "up", "move"], "default": "click"}, "hold_ms": {"type": "integer", "default": 60}},
      ["x", "y"])
def t_mouse(a):
    mouse(a["x"], a["y"], a.get("button", "left"), a.get("action", "click"), int(a.get("hold_ms", 60)))
    return {"ok": True}


@tool("key", "Press keys in the game: a key or combo (\"e\", \"Escape\", \"ctrl+s\", \"F5\"), optionally held for hold_ms, repeated `times`.",
      {"keys": {"type": "string"}, "hold_ms": {"type": "integer", "default": 50}, "times": {"type": "integer", "default": 1}}, ["keys"])
def t_key(a):
    for _ in range(max(1, int(a.get("times", 1)))):
        press_combo(a["keys"], int(a.get("hold_ms", 50)))
        time.sleep(0.05)
    return {"pressed": a["keys"], **(player_pos() or {})}


MOVES = {"left": "a", "right": "d", "up": "w", "down": "s", "jump": "space", "jetpack": "w"}


@tool("move", "Walk/fly the player by holding a direction key (default binds: left=A, right=D, up/jetpack=W, down=S, jump=Space). Returns the new position.",
      {"direction": {"type": "string", "enum": list(MOVES)}, "ms": {"type": "integer", "default": 500}}, ["direction"])
def t_move(a):
    before = player_pos()
    press_combo(MOVES[a["direction"]], int(a.get("ms", 500)))
    time.sleep(0.2)
    return {"before": before, "after": player_pos()}


def player_pos():
    try:
        return js("""const p = S && S.store && S.store.player; if (!p) return null;
          return {x: Math.round(p.x), y: Math.round(p.y), cellX: Math.floor(p.x / 4), cellY: Math.floor(p.y / 4)};""", timeout=5)
    except ToolError:
        return None


@tool("player", "Player position (world px and cells; 1 cell = 4 px), velocity, inventory summary, world size.")
def t_player(a):
    return js("""need(); const p = S.store.player;
      let world = null; try { world = {cellsW: S.store.world.size.width, cellsH: S.store.world.size.height}; } catch (e) {}
      let inv = null; try { inv = (p.inventory || []).slice(0, 30).map(i => i && (i.id || i.type || i.name)); } catch (e) {}
      return {x: p.x, y: p.y, cellX: Math.floor(p.x / 4), cellY: Math.floor(p.y / 4), velocity: p.velocity || {x: p.vx, y: p.vy}, world, inventory: inv};""")


@tool("teleport", "Move the player to (x, y). unit 'cell' (default) or 'px'. Stops velocity and snaps the camera.",
      {"x": {"type": "number"}, "y": {"type": "number"}, "unit": {"type": "string", "enum": ["cell", "px"], "default": "cell"}}, ["x", "y"])
def t_teleport(a):
    k = 4 if a.get("unit", "cell") == "cell" else 1
    x, y = a["x"] * k, a["y"] * k
    return js(f"""need(); api.player.setPosition(S, {x}, {y}); try {{ api.player.setVelocity(S, 0, 0); }} catch (e) {{}}
      api.camera.snapToPlayer(S); await new Promise(r => setTimeout(r, 300));
      const p = S.store.player; return {{x: p.x, y: p.y, cellX: Math.floor(p.x / 4), cellY: Math.floor(p.y / 4)}};""")


@tool("logs", "Recent game logs. source 'game' = console/errors/events captured by the bridge mod this page load (use since_seq to page); 'main' = tail of main.log on disk (works without the game).",
      {"source": {"type": "string", "enum": ["game", "main"], "default": "game"}, "since_seq": {"type": "integer", "default": 0},
       "level": {"type": "string", "description": "comma list: log,info,warn,error,debug"}, "grep": {"type": "string"},
       "limit": {"type": "integer", "default": 80}, "full": {"type": "boolean", "default": False, "description": "include stacks / all lines"}})
def t_logs(a):
    limit, grep = int(a.get("limit", 80)), (a.get("grep") or "").lower()
    if a.get("source") == "main":
        with open(MAIN_LOG, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400_000))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        if grep:
            lines = [l for l in lines if grep in l.lower()]
        return {"file": MAIN_LOG, "lines": lines[-limit:]}
    levels = [s.strip() for s in (a.get("level") or "").split(",") if s.strip()]
    body = "m" if a.get("full") else "m.split(String.fromCharCode(10))[0]"
    return js(f"""need(); const since = {int(a.get('since_seq', 0))}, levels = {json.dumps(levels)}, g = {json.dumps(grep)};
      const rows = mcp.logs.filter(l => l.seq > since && (!levels.length || levels.includes(l.level)) && (!g || l.msg.toLowerCase().includes(g)));
      const body = (m) => {body};
      return {{seq: mcp.seq, total: rows.length, logs: rows.slice(-{limit}).map(l => `#${{l.seq}} ${{new Date(l.t).toISOString().slice(11, 23)}} ${{l.level}} [${{l.source}}]${{l.n ? " (x" + l.n + ")" : ""}} ${{body(l.msg)}}`)}};""")


@tool("screenshot", "Capture the game view as an image (JPEG, scaled; 0.5 of a 1080p window is ~960x540). Prefer ui_list/game_status when text is enough.",
      {"scale": {"type": "number", "default": 0.5}, "quality": {"type": "integer", "default": 70},
       "save_to": {"type": "string", "description": "optional file path to also write the image"}})
def t_shot(a):
    scale = max(0.1, min(1.0, float(a.get("scale", 0.5))))
    vp = cdp.evaluate("[innerWidth, innerHeight]")
    r = cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": int(a.get("quality", 70)),
                                            "clip": {"x": 0, "y": 0, "width": vp[0], "height": vp[1], "scale": scale}}, timeout=30)
    if a.get("save_to"):
        with open(a["save_to"], "wb") as fh:
            fh.write(base64.b64decode(r["data"]))
    return {"__image__": r["data"], "mime": "image/jpeg",
            "text": f"viewport {vp[0]}x{vp[1]} css px, image scale {scale} (multiply image coords by {round(1 / scale, 3)} for mouse)"}


@tool("eval", "Run JavaScript in the game page (async function body; use `return`). In scope: mcp (bridge), sk (sandkit), S (game state = sk.state), api (engine api: api.player.setPosition(S,x,y), api.game.save(S,name) …), need(). Result is JSON-serialized (depth 6, capped).",
      {"code": {"type": "string"}, "timeout_s": {"type": "number", "default": 30}}, ["code"])
def t_eval(a):
    code = a["code"]
    if "return" not in code and "\n" not in code.strip() and not code.strip().endswith(";"):
        code = "return (" + code + ");"
    return js(code, timeout=float(a.get("timeout_s", 30)))


@tool("wait_for", "Wait until the scene is one of `scene` (MainMenu, Intro, Deploy, Game), or for a JS condition (`until`, an expression) to be truthy.",
      {"scene": {"type": "string"}, "until": {"type": "string"}, "timeout_s": {"type": "number", "default": 60}})
def t_wait(a):
    timeout = float(a.get("timeout_s", 60))
    if a.get("until"):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = js("return !!(" + a["until"] + ");", timeout=5)
                if v:
                    return {"ok": True}
            except ToolError:
                pass
            time.sleep(0.5)
        raise ToolError(f"Timed out: {a['until']}")
    scenes = [s.strip() for s in (a.get("scene") or "Game").split(",")]
    return wait_until(lambda s: s.get("scene") in scenes, timeout, "scene " + "/".join(scenes))


RESOLVE = r"""
const resolve = (path) => {
  // "store.player" (game state), "api.player" (engine api), "sandkit.api.x" (mod api), "window.x".
  const parts = (path || "").split(".").filter(Boolean);
  const roots = {api, sandkit: sk, window, mcp, S};
  let cur = roots[parts[0]] !== undefined ? roots[parts.shift()] : S, parent = null;
  for (const p of parts) { if (cur == null) throw new Error("nothing at " + p + " in " + path); parent = cur; cur = cur instanceof Map ? cur.get(p) : cur[p]; }
  return {cur, parent};
};
"""


@tool("state", "Browse game state (or api./sandkit./window. paths) without writing JS. Arrays/Maps are paged (offset/limit); `find` keeps items whose JSON contains the text or matches key=value.",
      {"path": {"type": "string", "default": "store", "description": "e.g. store.player, store.structures, session.camera, api.player"},
       "depth": {"type": "integer", "default": 2}, "offset": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 20},
       "find": {"type": "string"}})
def t_state(a):
    return js("need();" + RESOLVE + f"""
      const {{cur}} = resolve({json.dumps(a.get('path', 'store'))}), depth = {int(a.get('depth', 2))};
      const find = {json.dumps(a.get('find') or '')}, off = {int(a.get('offset', 0))}, lim = {int(a.get('limit', 20))};
      const listy = Array.isArray(cur) || cur instanceof Map || cur instanceof Set || ArrayBuffer.isView(cur);
      if (!listy) {{
        if (cur && typeof cur === "object" && !find) return {{type: cur.constructor && cur.constructor.name, keys: Object.keys(cur).length, value: ser(cur, depth)}};
        if (!find) return {{value: ser(cur, depth)}};
      }}
      let entries = cur instanceof Map ? Array.from(cur.entries()) : listy ? Array.from(cur).map((v, i) => [i, v]) : Object.entries(cur);
      if (find) {{
        const kv = find.match(/^([\\w.]+)=(.*)$/);
        entries = entries.filter(([k, v]) => {{
          if (kv) {{ let x = v; for (const p of kv[1].split(".")) x = x == null ? x : x[p]; return String(x) === kv[2]; }}
          try {{ return JSON.stringify(ser(v, 3)).toLowerCase().includes(find.toLowerCase()); }} catch (e) {{ return false; }}
        }});
      }}
      return {{total: entries.length, offset: off, items: entries.slice(off, off + lim).map(([k, v]) => ({{key: k, value: ser(v, depth)}}))}};""")


@tool("api_list", "Discover the engine API (sandkit.engine.api; every function takes the game state first). No path: namespaces with member counts. Namespace path: members with parameter lists. Function path: its source head.",
      {"path": {"type": "string", "description": "e.g. elements, api.player.setPosition, sandkit.api.ui"}})
def t_api_list(a):
    path = a.get("path") or ""
    if path and not path.split(".")[0] in ("api", "sandkit", "window", "mcp"):
        path = "api." + path
    return js("need();" + RESOLVE + f"""
      const path = {json.dumps(path)}, {{cur}} = path ? resolve(path) : {{cur: api}};
      const sig = (f) => {{ const s = String(f); const m = s.match(/^(?:async\\s*)?(?:function\\s*\\w*)?\\s*\\(([^)]*)\\)|^(?:async\\s*)?(\\w+)\\s*=>/); return m ? "(" + (m[1] !== undefined ? m[1] : m[2]) + ")" : "(?)"; }};
      if (typeof cur === "function") return {{path, source: String(cur).slice(0, 1500)}};
      if (!cur || typeof cur !== "object") return {{path, value: ser(cur, 1)}};
      const out = {{}};
      for (const k of Object.keys(cur)) {{
        const v = cur[k];
        out[k] = typeof v === "function" ? sig(v) : v && typeof v === "object" ? (path ? "{{" + Object.keys(v).length + " members}}" : Object.keys(v).length) : ser(v, 0);
      }}
      return {{path: path || "api", members: out}};""")


@tool("api_call", "Call an engine API function with the game state passed first (api.elements.getInfoAtCell(S, x, y) → path 'elements.getInfoAtCell', args [x, y]). Paths starting 'sandkit.' call the mod API without the state.",
      {"path": {"type": "string"}, "args": {"type": "array", "default": []}, "depth": {"type": "integer", "default": 3}}, ["path"])
def t_api_call(a):
    path = a["path"]
    with_state = not path.startswith(("sandkit.", "window.", "mcp."))
    if with_state and not path.startswith("api."):
        path = "api." + path
    return js("need();" + RESOLVE + f"""
      const {{cur, parent}} = resolve({json.dumps(path)});
      if (typeof cur !== "function") throw new Error({json.dumps(path)} + " is not a function");
      const args = {json.dumps(a.get('args') or [])};
      const r = await cur.apply(parent, {'[S, ...args]' if with_state else 'args'});
      return ser(r, {int(a.get('depth', 3))});""")


CELL_NAMES = r"""
// Cell ids: 0 empty, terrain ids are their terrain type, element ids pack the type with per-cell data.
const cellKey = (id) => {
  if (id === 0) return "empty";
  if (api.terrains.isCellIdTerrain(id)) return "t" + id;
  try { return "e" + api.elements.getResolvedTypeFromCellId(S, id); } catch (e) { return "?" + id; }
};
const nameOf = (key) => {
  if (key === "empty") return "empty";
  const n = +key.slice(1);
  if (key[0] === "e") { try { return "element " + n + " " + (api.elements.getName(S, n) || ""); } catch (e) { return "element " + n; } }
  try {
    const c = api.terrains.getConfig(n), s = c && String(c.name || c.id || c.nameKey || "");
    if (s) return "terrain " + n + " " + (/^terrains\|[^|]+\|name$/.test(s) ? s.split("|")[1] : s);  // i18n key "terrains|stone|name"
  } catch (e) {}
  return key[0] === "t" ? "terrain " + n : "cell id " + n;
};
"""


@tool("cells", "Read a rectangle of world cells as a character grid plus legend (cheaper than a screenshot). Cells are grouped by terrain or element type; P marks the player. Max 96x64.",
      {"x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer", "default": 32}, "h": {"type": "integer", "default": 16},
       "around_player": {"type": "boolean", "default": False, "description": "centre the rectangle on the player (x/y ignored)"}})
def t_cells(a):
    w, h = min(96, int(a.get("w", 32))), min(64, int(a.get("h", 16)))
    if a.get("around_player"):
        origin = f"const px = Math.floor(S.store.player.x / 4) - {w // 2}, py = Math.floor(S.store.player.y / 4) - {h // 2};"
    else:
        if "x" not in a or "y" not in a:
            raise ToolError("Give x and y, or around_player:true.")
        origin = f"const px = {int(a['x'])}, py = {int(a['y'])};"
    return js("need();" + CELL_NAMES + origin + f"""
      const chars = "#%&*+=@$ABCDEFGHIJKLMNOQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", map = new Map(), rows = [];
      let used = 0;  // "." is empty, "P" the player
      const pcx = Math.floor(S.store.player.x / 4), pcy = Math.floor(S.store.player.y / 4);
      for (let y = py; y < py + {h}; y++) {{
        let row = "";
        for (let x = px; x < px + {w}; x++) {{
          let key; try {{ const id = api.world.getCellId(S, x, y); key = typeof id === "number" ? cellKey(id) : "out"; }} catch (e) {{ key = "out"; }}
          if (!map.has(key)) map.set(key, key === "empty" ? "." : chars[Math.min(used++, chars.length - 1)]);
          row += (x === pcx && y === pcy) ? "P" : map.get(key);
        }}
        rows.push(row);
      }}
      const legend = {{}};
      for (const [key, c] of map) legend[c] = key === "out" ? "outside the world" : nameOf(key);
      return {{origin: [px, py], size: [{w}, {h}], player: [pcx, pcy], grid: rows, legend}};""")


@tool("paint", "Fill a cell rectangle with an element (by name, e.g. sand, water), a terrain (e.g. stone), or empty it. Changes the running world.",
      {"x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer", "default": 1}, "h": {"type": "integer", "default": 1},
       "element": {"type": "string"}, "terrain": {"type": "string"}, "empty": {"type": "boolean", "default": False}}, ["x", "y"])
def t_paint(a):
    w, h = int(a.get("w", 1)), int(a.get("h", 1))
    if w * h > 20000:
        raise ToolError("At most 20000 cells per call.")
    if sum(bool(a.get(k)) for k in ("element", "terrain", "empty")) != 1:
        raise ToolError("Give exactly one of element, terrain, empty.")
    return js("need();" + f"""
      const el = {json.dumps(a.get('element'))}, te = {json.dumps(a.get('terrain'))}, empty = {'true' if a.get('empty') else 'false'};
      let type = null;
      if (el) {{ type = api.elements.getElementTypeFromId(S, el); if (type == null) throw new Error("unknown element " + el); }}
      if (te) {{ type = api.terrains.getTypeByName(S, te); if (typeof type !== "number") throw new Error("unknown terrain " + te); }}
      let changed = 0;
      for (let y = {int(a['y'])}; y < {int(a['y'])} + {h}; y++) for (let x = {int(a['x'])}; x < {int(a['x'])} + {w}; x++) {{
        const id = api.world.getCellId(S, x, y), terrain = id !== 0 && api.terrains.isCellIdTerrain(id), element = id !== 0 && !terrain;
        if (empty) {{ if (element) api.elements.removeAt(S, x, y); else if (terrain) api.terrains.removeAt(S, x, y); else continue; }}
        else if (el) {{ if (element) api.elements.replaceAt(S, x, y, type); else {{ if (terrain) api.terrains.removeAt(S, x, y); api.elements.createAt(S, x, y, type); }} }}
        else {{ if (element) api.elements.removeAt(S, x, y); api.terrains.createAt(S, x, y, type); }}
        changed++;
      }}
      return {{changed, type}};""")


@tool("count", "Count the cells of each element and terrain type in a rectangle (whole numbers, no grid). Max 4,000,000 cells.",
      {"x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer", "default": 32}, "h": {"type": "integer", "default": 16}},
      ["x", "y"])
def t_count(a):
    w, h = int(a.get("w", 32)), int(a.get("h", 16))
    if w <= 0 or h <= 0 or w * h > 4_000_000:
        raise ToolError("w and h must be positive, at most 4,000,000 cells in all.")
    return js("need();" + CELL_NAMES + f"""
      const px = {int(a['x'])}, py = {int(a['y'])}, tally = new Map();
      for (let y = py; y < py + {h}; y++) for (let x = px; x < px + {w}; x++) {{
        let key; try {{ const id = api.world.getCellId(S, x, y); key = typeof id === "number" ? cellKey(id) : "out"; }} catch (e) {{ key = "out"; }}
        tally.set(key, (tally.get(key) || 0) + 1);
      }}
      const counts = {{}};
      for (const [key, n] of Array.from(tally).sort((p, q) => q[1] - p[1])) counts[key === "out" ? "outside the world" : nameOf(key)] = n;
      return {{origin: [px, py], size: [{w}, {h}], cells: {w * h}, counts}};""", timeout=120)


# Structure lookup by any of the names the game accepts (type id, structure name, i18n name).
STRUCT_HELPERS = r"""
const structType = (name) => {
  const t = api.structures.resolveTypeName(name);
  if (api.structures.getConfig(t)) return t;
  let all = Object.keys((S.sandkit && S.sandkit.mods && S.sandkit.mods.structures) || {});
  try { all = all.concat(Array.from(api.structures.getUnlockedTypes(S) || []).map(String)); } catch (e) {}
  all = Array.from(new Set(all)).filter(k => api.structures.getConfig(k));
  const low = String(name).toLowerCase().replace(/[\s_-]/g, "");
  const hit = all.find(k => k.toLowerCase().replace(/[\s_-]/g, "") === low);
  if (hit) return hit;
  const near = all.filter(k => k.toLowerCase().includes(String(name).toLowerCase())).slice(0, 20);
  throw new Error("unknown structure type " + JSON.stringify(name) + (near.length ? "; close: " + near.join(", ") : ""));
};
const elementType = (v) => {
  if (typeof v === "number") return v;
  try { const t = api.elements.getElementTypeFromId(S, v); if (t != null) return t; } catch (e) {}
  const low = String(v).toLowerCase(), types = Array.from(api.elements.getRegisteredTypes(S) || []);
  const name = (t) => { try { return String(api.elements.getName(S, t) || ""); } catch (e) { return ""; } };
  const id = (t) => { try { return String(api.elements.getElementIdFromType(S, t) || ""); } catch (e) { return ""; } };
  const hit = types.find(t => name(t).toLowerCase() === low || id(t).toLowerCase() === low);
  if (hit != null) return hit;
  const near = types.filter(t => (name(t) + " " + id(t)).toLowerCase().includes(low)).map(t => id(t) + " (" + name(t) + ")").slice(0, 12);
  throw new Error("unknown element " + JSON.stringify(v) + (near.length ? "; close: " + near.join(", ") : ""));
};
const describe = (s) => {
  if (!s) return null;
  const out = {type: s.type, x: s.x, y: s.y};
  try { const c = api.structures.getConfig(s.type); if (c && c.nameKey) out.name = api.i18n.t(c.nameKey); } catch (e) {}
  if (s.filter) {
    out.filter = Object.assign({}, s.filter);
    const names = [].concat(s.filter.elementType).filter(t => t != null).map(t => { try { return api.elements.getName(S, t) || t; } catch (e) { return t; } });
    if (names.length) out.filter.elements = names;
  }
  if (s.data !== undefined) out.data = ser(s.data, 3);
  if (s.queued) out.queued = true;
  if (s.angle !== undefined) out.angle = s.angle;
  return out;
};
"""


@tool("build", "Place a structure at cell (x, y) at once, without costs: type is a structure id (filterWall, conveyorRight, …). "
      "filter: element name(s) for filters, e.g. \"sand\" or [\"sand\", \"water\"], or {elements, mode:'allow'|'deny', affectsLiquid, affectsGas}. "
      "data: structure data merged over the defaults. Returns the placed structure as the world stores it.",
      {"type": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"},
       "filter": {"description": "element name, list of names, or {elements, mode, affectsLiquid, affectsGas}"},
       "data": {"type": "object"}, "angle": {"type": "number", "description": "for structures placed by drag direction"}},
      ["type", "x", "y"])
def t_build(a):
    spec = json.dumps({k: a.get(k) for k in ("type", "x", "y", "filter", "data", "angle")})
    return js("need();" + STRUCT_HELPERS + f"""
      const a = {spec}, type = structType(a.type), opts = {{instant: true}};
      if (a.filter != null) {{
        const f = typeof a.filter === "object" && !Array.isArray(a.filter) ? Object.assign({{}}, a.filter) : {{elements: a.filter}};
        const els = [].concat(f.elements !== undefined ? f.elements : f.elementType).filter(v => v != null).map(elementType);
        delete f.elements;
        if (!els.length) throw new Error("filter needs at least one element");
        f.elementType = els.length === 1 ? els[0] : els;
        f.mode = f.mode || "allow";
        opts.copiedStructure = {{filter: f}};
      }}
      if (a.data) opts.data = a.data;
      if (a.angle != null) opts.angle = a.angle;
      const placed = api.structures.build(S, {{x: a.x, y: a.y}}, type, opts);
      const s = (placed && typeof placed === "object" && placed.type !== undefined ? placed : null) || api.structures.getAtCell(S, a.x, a.y);
      if (!s) throw new Error("nothing was built at " + a.x + "," + a.y + " (blocked, off the snap grid, or not placeable there)");
      return {{built: describe(s)}};""")


def rect_args(a):
    w, h = int(a.get("w", 1)), int(a.get("h", 1))
    if w <= 0 or h <= 0 or w * h > 1_000_000:
        raise ToolError("w and h must be positive, at most 1,000,000 cells in all.")
    return int(a["x"]), int(a["y"]), w, h


@tool("remove_structure", "Remove the structure at cell (x, y), or every structure with a cell in the rectangle w x h. Returns what was removed.",
      {"x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer", "default": 1}, "h": {"type": "integer", "default": 1}},
      ["x", "y"])
def t_remove_structure(a):
    x, y, w, h = rect_args(a)
    return js("need();" + STRUCT_HELPERS + f"""
      const x0 = {x}, y0 = {y}, w = {w}, h = {h}, seen = new Set(), found = [];
      const take = (s) => {{ if (s && !seen.has(s)) {{ seen.add(s); found.push(s); }} }};
      if (w * h <= 4096) {{ for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) take(api.structures.getAtCell(S, x, y)); }}
      else for (const s of S.store.structures) if (s && s.x >= x0 - 16 && s.x < x0 + w && s.y >= y0 - 16 && s.y < y0 + h) take(s);
      const removed = [];
      for (const s of found) {{ const d = describe(s); api.structures.removeAt(S, s.x, s.y, {{removeCells: true}}); removed.push(d); }}  // removeCells: as the demolisher does
      await new Promise(r => setTimeout(r, 100));
      return {{removed: removed.length, structures: removed.slice(0, 50)}};""")


@tool("remove_element", "Remove the loose elements (sand, water, gases, …) at cell (x, y) or in the rectangle w x h; terrain and structures stay. Returns the count by element.",
      {"x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer", "default": 1}, "h": {"type": "integer", "default": 1}},
      ["x", "y"])
def t_remove_element(a):
    x, y, w, h = rect_args(a)
    return js("need();" + CELL_NAMES + f"""
      const tally = {{}}; let removed = 0;
      for (let y = {y}; y < {y} + {h}; y++) for (let x = {x}; x < {x} + {w}; x++) {{
        const id = api.world.getCellId(S, x, y);
        if (!id || api.terrains.isCellIdTerrain(id)) continue;
        const name = nameOf(cellKey(id));
        api.elements.removeAt(S, x, y);
        tally[name] = (tally[name] || 0) + 1; removed++;
      }}
      return {{removed, byElement: tally}};""", timeout=120)


# Game modules the engine API doesn't expose, found through webpack's require by what they contain
# (never by their numeric ids, which change between game builds).
WEBPACK = r"""
const wpRequire = () => {
  if (!mcp.__require) webpackChunksand_v1.push([[Symbol("mcp-bridge")], {}, (r) => { mcp.__require = r; }]);
  return mcp.__require;
};
const wpModule = (name, test) => {
  mcp.__modules = mcp.__modules || {};
  if (mcp.__modules[name]) return mcp.__modules[name];
  const req = wpRequire();
  for (const id of Object.keys(req.m)) {
    let src; try { src = String(req.m[id]); } catch (e) { continue; }
    if (!test.src(src)) continue;
    const exp = req(id);
    if (exp && test.exports(exp)) return (mcp.__modules[name] = exp);
  }
  throw new Error("game module '" + name + "' not found (game update?)");
};
const techModule = () => wpModule("tech", {
  src: (s) => s.includes("unlockTech:()=>") && s.includes("getTechNodes:()=>"),
  exports: (e) => typeof e.unlockTech === "function" && typeof e.getTechNodes === "function"});
const factoryModule = () => {
  const m = wpModule("factory", {src: (s) => s.includes('"factory.processing"') && s.includes("viability.level="), exports: () => true});
  const setLevel = Object.values(m).find(f => typeof f === "function" && f.length === 2 && /viability\.level=/.test(String(f)) && /Math\.min\(/.test(String(f)));
  if (!setLevel) throw new Error("factory level setter not found (game update?)");
  return {setLevel};
};
// Research a tech node with the game's own unlockTech, so every effect runs (buildings, items, recipes,
// upgrades, discoveries, events). Costs are bypassed; the node's row/col are left off the copy so the
// factory-level row gate doesn't apply; a locked node is unlocked first.
const researchNodes = (nodes) => {
  const T = techModule(), done = [], failed = [], unlockedLocks = [];
  const cheat = S.session.cheat || (S.session.cheat = {}), was = cheat.bypassCosts;
  cheat.bypassCosts = true;
  try {
    for (const node of nodes) {
      if (S.store.player.tech[node.id]) continue;
      if (T.isTechLocked(S, node.id)) { api.tech.setLocked(S, node.id, false); unlockedLocks.push(node.id); }
      const copy = Object.assign({}, node); delete copy.row; delete copy.col;
      let ok = false, err = null;
      try { ok = T.unlockTech(S, copy, {playSound: false, skipCostCheck: true, suppressMusic: true}); } catch (e) { err = e.message; }
      (ok && S.store.player.tech[node.id] ? done : failed).push(err ? node.id + ": " + err : node.id);
    }
  } finally { cheat.bypassCosts = was; }
  try { api.ui.update(S, 4); } catch (e) {}  // UI root: build menu and HUD pick up the new unlocks
  return {researched: done, failed, unlockedLocks, tutorialActive: !!S.store.tutorial.active || undefined};
};
"""


@tool("tech_unlock", "Research one tech node with the game's own unlock (its buildings, items, recipes and upgrades are granted; no cost; factory-level row gate and tech lock bypassed). "
      "id: node id (\"native.magnet:magnet\"), its last part (\"magnet\") or its display name. Without id: lists the nodes.",
      {"id": {"type": "string"}})
def t_tech_unlock(a):
    return js("need();" + WEBPACK + f"""
      const T = techModule(), nodes = T.getTechNodes(S), want = {json.dumps(a.get('id'))};
      const name = (n) => {{ try {{ return T.getTechDisplayName(S, n) || n.id; }} catch (e) {{ return n.id; }} }};
      if (!want) return {{total: nodes.length, researched: nodes.filter(n => S.store.player.tech[n.id]).length,
        nodes: nodes.map(n => (S.store.player.tech[n.id] ? "✓ " : "· ") + n.id + " — " + name(n) + " (row " + n.row + ")")}};
      const low = want.toLowerCase();
      const sid = (n) => String(n.id);
      const hits = [n => sid(n) === want, n => sid(n).toLowerCase() === low, n => sid(n).split(/[:.]/).pop().toLowerCase() === low,
                    n => String(name(n)).toLowerCase() === low].map(f => nodes.filter(f)).find(h => h.length);
      if (!hits) throw new Error("no tech node " + JSON.stringify(want) + "; close: " + nodes.filter(n => (sid(n) + " " + name(n)).toLowerCase().includes(low)).map(n => sid(n) + " (" + name(n) + ")").slice(0, 15).join(", "));
      if (hits.length > 1) throw new Error("ambiguous: " + hits.map(n => n.id).join(", "));
      const node = hits[0];
      if (S.store.player.tech[node.id]) return {{id: node.id, name: name(node), already: true}};
      const r = researchNodes([node]);
      return Object.assign({{id: node.id, name: name(node), ok: r.researched.length === 1, unlocks: ser(node.unlocks, 3)}}, r);""")


@tool("tech_unlock_all", "Research every tech node (game's own unlock per node, no cost, locks and row gates bypassed), then raise the factory level to its max unless factory_max:false. "
      "Unlike the cheat panel's Everything it doesn't touch gold, dungeons, augments or the save's cheat flag.",
      {"factory_max": {"type": "boolean", "default": True}})
def t_tech_unlock_all(a):
    return js("need();" + WEBPACK + f"""
      const T = techModule(), nodes = T.getTechNodes(S).slice().sort((p, q) => (p.row || 0) - (q.row || 0));
      const before = nodes.filter(n => S.store.player.tech[n.id]).length;
      const r = researchNodes(nodes);
      const out = {{total: nodes.length, before, after: nodes.filter(n => S.store.player.tech[n.id]).length, newly: r.researched.length,
                   failed: r.failed, unlockedLocks: r.unlockedLocks.length, tutorialActive: r.tutorialActive}};
      if ({'true' if a.get('factory_max', True) else 'false'}) {{
        const was = S.store.viability.level;
        factoryModule().setLevel(S, 1e6);
        out.factoryLevel = {{was, now: S.store.viability.level}};
        try {{ api.ui.update(S, 4); }} catch (e) {{}}
      }}
      out.cheatsUsed = !!(S.store.integrity && S.store.integrity.cheatsUsed);
      return out;""", timeout=60)


SNAPSHOTS = {}


def flatten(v, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(v, dict):
        for k, x in v.items():
            flatten(x, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            flatten(x, f"{prefix}[{i}]", out)
    else:
        out[prefix] = v
    return out


@tool("snapshot", "Store a copy of a state path (server side) under `name`, to compare later with snapshot_diff.",
      {"name": {"type": "string"}, "path": {"type": "string", "default": "store.player"}, "depth": {"type": "integer", "default": 6}}, ["name"])
def t_snapshot(a):
    path = a.get("path", "store.player")
    v = js("need();" + RESOLVE + f"return ser(resolve({json.dumps(path)}).cur, {int(a.get('depth', 6))});", timeout=60)
    SNAPSHOTS[a["name"]] = {"path": path, "depth": int(a.get("depth", 6)), "t": time.time(), "value": v}
    return {"stored": a["name"], "path": path, "leaves": len(flatten(v)), "all": sorted(SNAPSHOTS)}


@tool("snapshot_diff", "Compare snapshot `a` with snapshot `b`, or with the live value of a's path when b is omitted. Lists changed, added and removed leaves.",
      {"a": {"type": "string"}, "b": {"type": "string"}, "limit": {"type": "integer", "default": 100}}, ["a"])
def t_snapshot_diff(a):
    if a["a"] not in SNAPSHOTS:
        raise ToolError(f"No snapshot {a['a']!r}; have {sorted(SNAPSHOTS)}. Snapshots live in the server process.")
    s1 = SNAPSHOTS[a["a"]]
    if a.get("b"):
        s2 = SNAPSHOTS[a["b"]]["value"]
    else:
        s2 = js("need();" + RESOLVE + f"return ser(resolve({json.dumps(s1['path'])}).cur, {s1['depth']});", timeout=60)
    f1, f2 = flatten(s1["value"]), flatten(s2)
    changed = {k: [f1[k], f2[k]] for k in f1 if k in f2 and f1[k] != f2[k]}
    added = {k: f2[k] for k in f2 if k not in f1}
    removed = {k: f1[k] for k in f1 if k not in f2}
    lim = int(a.get("limit", 100))
    cut = lambda d: dict(list(d.items())[:lim])
    return {"path": s1["path"], "seconds": round(time.time() - s1["t"], 1), "counts": {"changed": len(changed), "added": len(added), "removed": len(removed)},
            "changed": cut(changed), "added": cut(added), "removed": cut(removed)}


@tool("events_watch", "Record game events by name (e.g. building:removed, factory:levelUp, tutorial:completed) into a bounded buffer for this page load; read with events_read.",
      {"names": {"type": "array", "items": {"type": "string"}}, "max": {"type": "integer", "default": 200}}, ["names"])
def t_events_watch(a):
    return js("need();" + f"""
      mcp.probes = mcp.probes || {{}};
      const out = {{}};
      for (const name of {json.dumps(a['names'])}) {{
        if (mcp.probes[name]) {{ out[name] = "already watching"; continue; }}
        const probe = mcp.probes[name] = {{max: {int(a.get('max', 200))}, count: 0, rows: []}};
        try {{
          sk.api.events.on(name, (payload) => {{
            probe.count++;
            const p = payload && typeof payload === "object" ? Object.fromEntries(Object.entries(payload).filter(([k]) => k !== "state")) : payload;
            probe.rows.push({{t: Date.now(), payload: ser(p, 2)}});
            if (probe.rows.length > probe.max) probe.rows.shift();
          }});
          out[name] = "watching";
        }} catch (e) {{ delete mcp.probes[name]; out[name] = "failed: " + e.message; }}
      }}
      return out;""")


@tool("events_read", "Read recorded events (all probes, or one `name`). clear:true empties the buffers.",
      {"name": {"type": "string"}, "limit": {"type": "integer", "default": 30}, "clear": {"type": "boolean", "default": False}})
def t_events_read(a):
    return js("need();" + f"""
      const probes = mcp.probes || {{}}, want = {json.dumps(a.get('name'))}, out = {{}};
      for (const [name, p] of Object.entries(probes)) {{
        if (want && name !== want) continue;
        out[name] = {{count: p.count, recent: p.rows.slice(-{int(a.get('limit', 30))}).map(r => ({{at: new Date(r.t).toISOString().slice(11, 23), payload: r.payload}}))}};
        if ({'true' if a.get('clear') else 'false'}) p.rows.length = 0;
      }}
      return out;""")


@tool("perf", "Sample rendering for `ms`: frames/s, frame-time p50/p95/max, JS heap, structure count.",
      {"ms": {"type": "integer", "default": 2000}})
def t_perf(a):
    ms = max(200, min(20000, int(a.get("ms", 2000))))
    return js(f"""
      const times = []; let last = performance.now();
      await new Promise(done => {{ const end = last + {ms}; const tick = (t) => {{ times.push(t - last); last = t; t < end ? requestAnimationFrame(tick) : done(); }}; requestAnimationFrame(tick); }});
      times.shift(); const s = times.slice().sort((x, y) => x - y), q = (p) => s.length ? +s[Math.min(s.length - 1, Math.floor(p * s.length))].toFixed(2) : null;
      const out = {{fps: +(times.length * 1000 / {ms}).toFixed(1), frameMs: {{p50: q(0.5), p95: q(0.95), max: q(1)}}}};
      if (performance.memory) out.heapMB = {{used: Math.round(performance.memory.usedJSHeapSize / 1e6), limit: Math.round(performance.memory.jsHeapSizeLimit / 1e6)}};
      try {{ out.structures = S.store.structures.length; }} catch (e) {{}}
      return out;""", timeout=ms / 1000 + 10)


@tool("cdp", "Send a raw Chrome DevTools Protocol command to the game page (escape hatch, e.g. Performance.getMetrics, Runtime.getHeapUsage).",
      {"method": {"type": "string"}, "params": {"type": "object", "default": {}}}, ["method"])
def t_cdp(a):
    return cdp.call(a["method"], a.get("params") or {}, timeout=60)


MOD_SKIP = {".git", "__pycache__", "node_modules", ".vscode", ".idea"}


def find_mod_root(path):
    """The folder holding modinfo.json: `path` itself, or its mod/, src/ or dist/ subfolder."""
    path = os.path.abspath(os.path.expanduser(path))
    for cand in (path, os.path.join(path, "mod"), os.path.join(path, "src"), os.path.join(path, "dist")):
        if os.path.isfile(os.path.join(cand, "modinfo.json")):
            return cand
    raise ToolError(f"No modinfo.json in {path} (nor in its mod/, src/ or dist/ subfolder).")


def install_folder(path):
    """Copies a mod folder (the one holding modinfo.json) to %APPDATA%/sandustry/mods/<id>. Overwrites, never deletes."""
    src = find_mod_root(path)
    with open(os.path.join(src, "modinfo.json"), encoding="utf-8-sig") as fh:
        info = json.load(fh)
    mod_id = info.get("id")
    if not mod_id or any(c in mod_id for c in '\\/:*?"<>|') or mod_id in (".", ".."):
        raise ToolError(f"modinfo.json in {src} has no usable id ({mod_id!r}).")
    dst = os.path.join(APPDATA, "mods", mod_id)
    out = {"id": mod_id, "version": info.get("version"), "from": src, "to": dst}
    if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(src):
        out["copied"] = 0  # already the installed folder
        return out
    copied = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in MOD_SKIP and not d.startswith(".")]
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), os.path.join(dst, rel, f))
            copied.append(os.path.normpath(os.path.join(rel, f)).replace("\\", "/"))
    extra = []
    for root, dirs, files in os.walk(dst):
        rel = os.path.relpath(root, dst)
        extra += [r for r in (os.path.normpath(os.path.join(rel, f)).replace("\\", "/") for f in files) if r not in copied]
    out["copied"] = len(copied)
    if extra:
        out["kept_extra_files"] = extra[:30]  # e.g. workshop.json written by the game's uploader
    return out


def install_bridge():
    """The companion mod, for first-time setup (--install-mod): copies ../mod and removes the 0.1.0 id."""
    removed = []
    for legacy in LEGACY_MOD_IDS:
        path = os.path.join(APPDATA, "mods", legacy)
        if os.path.isfile(os.path.join(path, "modinfo.json")) and "mcpbridge" in open(os.path.join(path, "modinfo.json"), encoding="utf-8").read():
            shutil.rmtree(path)
            removed.append(path)
    out = install_folder(MOD_SRC)
    out["removed_old"] = removed
    out["note"] = "Restart the game (game_launch restart:true) to load it."
    return out


DISABLED_FILE = os.path.join(APPDATA, "mods", MOD_ID, "disabled.json")  # read by the bridge mod at each page load


def disabled_mods(change_id=None, disable=None):
    try:
        with open(DISABLED_FILE, encoding="utf-8") as fh:
            ids = [x for x in json.load(fh) if isinstance(x, str)]
    except (OSError, ValueError, TypeError):
        ids = []
    if change_id and disable is not None:
        ids = [x for x in ids if x != change_id] + ([change_id] if disable else [])
        os.makedirs(os.path.dirname(DISABLED_FILE), exist_ok=True)
        with open(DISABLED_FILE, "w", encoding="utf-8") as fh:
            json.dump(ids, fh)
    return ids


def file_url_to_path(url):
    from urllib.parse import unquote
    if not url.startswith("file:///"):
        return None
    return os.path.normpath(unquote(url[len("file:///"):]))


def game_start_epoch():
    """When the running game started, from the main process's patch run in main.log (None if unknown)."""
    import datetime
    try:
        r = t_patches_status({})
        return datetime.datetime.strptime(r["run"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (ToolError, KeyError, ValueError):
        return None


def save_for_reload():
    """Saves the running world as a new save "MCP reload (<world>)" and waits until the file is on disk.
    mod_reload loads it, then moves it to the trash: the player's own saves are never written."""
    meta = js("need(); let last = null; try { last = window.electron.getLastPlayedGameSync(); } catch (e) {}"
              "return {world: S.store.meta.worldName || S.store.meta.name || 'world', worldId: S.store.meta.worldId, last};")
    name = f"MCP reload ({meta['world']})"
    t0 = time.time()
    sid = js("need(); const id = api.game.save(S, " + json.dumps(name) + ");"
             "await new Promise(r => setTimeout(r, 300));"
             "for (let i = 0; i < 300 && S.session.saving; i++) await new Promise(r => setTimeout(r, 100));"
             "return id;", timeout=60)
    path = os.path.join(SAVES, f"{sid}.save")
    for _ in range(100):
        if os.path.isfile(path) and os.path.getmtime(path) >= t0 - 1:
            break
        time.sleep(0.2)
    else:
        raise ToolError(f"Saving before the reload didn't reach the disk ({path}); nothing was reloaded.")
    return {"id": sid, "name": name, "worldId": meta["worldId"], "_last": meta.get("last")}


def trash_reload_save(saved):
    """After the reload: a copy of the reload save goes to the trash (mcp-reload-<worldId>.save, latest only), then
    the game deletes it, which also removes its Steam Cloud copy (a file only moved away comes back with the cloud).
    Only the save made by save_for_reload, checked by id and name."""
    meta = next((m for m in read_saves() if m["id"] == saved["id"]), None)
    if not meta or meta.get("name") != saved["name"]:
        return False
    os.makedirs(TRASH, exist_ok=True)
    for suffix in (".save", ".save.backup"):
        src = os.path.join(SAVES, saved["id"] + suffix)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(TRASH, f"mcp-reload-{saved['worldId']}{suffix}"))
    # Then nothing may point at it: the page address (a page reload would load it) and the game's
    # "last played" entry (set by the save; one of the game's buttons loads it unchecked) go back.
    r = js("const r = await window.electron.deleteSave(" + json.dumps(saved["id"]) + ");"
           "history.replaceState(history.state, '', location.pathname);"
           "const last = " + json.dumps(saved.get("_last")) + ";"
           "try { if (last) await window.electron.saveLastPlayedGame(JSON.parse(last)); else await window.electron.clearLastPlayedGame(); } catch (e) {}"
           "return r;", timeout=20)
    return bool(r and r.get("success")) and not os.path.exists(os.path.join(SAVES, saved["id"] + ".save"))


@tool("mod_reload", "Run mods' current code without restarting the game. With the bridge mod, every page load runs each mod's main, worker and worldgen code "
      "as it is on disk now (local and Workshop mods); this tool reloads the page. In a world it first saves (a new save, deleted with its Steam Cloud copy once the world has "
      "loaded from it, a copy kept in saves/.mcp-trash; the player's own saves are never written) and reloads into it, so progress is kept (save:false: reload without saving). In the main menu it reloads the menu. "
      "path: first copy that mod folder (the one with modinfo.json, or its parent with a mod/ subfolder) into the game's mods folder; works with the game closed. "
      "disable: true/false with id or path: stop or resume running that mod's code. "
      "Still read only at game start, listed under needs_restart: new mods, modinfo.json, assets, patches.json, the bridge mod itself.",
      {"path": {"type": "string", "description": "mod folder on disk, e.g. E:/Modding/Sandustry/mods/tight-filters"},
       "id": {"type": "string", "description": "mod id, for disable"},
       "disable": {"type": "boolean", "description": "true: the mod's code no longer runs; false: it runs again"},
       "save": {"type": "boolean", "default": True}, "timeout_s": {"type": "number", "default": 180}})
def t_mod_reload(a):
    out = {}
    mod_id = a.get("id")
    if a.get("path"):
        out["installed"] = install_folder(a["path"])
        mod_id = out["installed"]["id"]
    if not cdp.available():
        if not a.get("path") and a.get("disable") is None:
            raise ToolError("Game not reachable on the DevTools port: game_launch first.")
        if a.get("disable") is not None:
            out["disabled"] = disabled_mods(mod_id, a.get("disable"))
        out["note"] = "The game isn't running: this applies at the next start (game_launch)."
        return out
    disable = a.get("disable")
    if disable is not None and not mod_id:
        raise ToolError("disable needs id or path.")
    off = disabled_mods(mod_id, disable)
    if off:
        out["disabled"] = off
    try:
        st = js("need();" + f"""
      if (!mcp.reload) throw new Error("bridge mod " + mcp.version + " doesn't run fresh code: run `python -I sandustry_mcp.py --install-mod`, then game_launch restart:true once");
      const r = await window.electron.platform.workshop.getSandkitMods();
      const mods = ((r && r.data && r.data.mods) || []).map(m => ({{id: m.manifest.id, root: m.rootUrl, patches: m.manifest.patches}}));
      return {{mods, url: location.search}};""")
    except ToolError as e:
        if not a.get("path"):
            raise
        out["note"] = f"Copied, not reloaded: {e}"
        return out
    known = {m["id"]: m for m in st["mods"]}
    restart = []
    if mod_id and mod_id not in known:
        restart.append(f"{mod_id}: new mod (the game finds mod folders at start)")
    if mod_id == MOD_ID and a.get("path"):
        restart.append(f"{MOD_ID}: the bridge itself loads before it can refresh anything")
    started = game_start_epoch()
    for m in st["mods"]:
        folder = file_url_to_path(m.get("root") or "")
        if not folder or not started:
            continue
        for f in ["modinfo.json"] + ([m["patches"]] if isinstance(m.get("patches"), str) else []):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and os.path.getmtime(p) > started:
                restart.append(f"{m['id']}/{f}: changed since the game started")
    if restart:
        out["needs_restart"] = {"what": restart, "how": "game_launch restart:true (save first if needed)"}
    if mod_id and mod_id not in known:
        out["note"] = "Not reloaded: a new mod only loads after a game restart."
        return out
    url, timeout = st.get("url") or "", float(a.get("timeout_s", 180))
    from urllib.parse import parse_qs
    q = parse_qs(url.lstrip("?"))
    t0 = time.time()
    scene = (page_state() or {}).get("scene")
    if scene == "Game" and a.get("save", True):
        out["saved"] = save_for_reload()
        navigate("?db_load=" + out["saved"]["id"], timeout)
        wait_until(lambda s: s.get("scene") in ("Game", "Intro", "Deploy") or not s.get("bridge"), timeout, "the world")
        out["reloaded"] = {"save": out["saved"]["id"]}
    elif q.get("new_game"):
        r = t_new({"seed": q.get("seed", [""])[0] or None, "name": q.get("name", [""])[0] or None, "timeout_s": timeout})
        out["reloaded"] = {"new_world": r.get("name"), "seed": r.get("seed")}
    elif q.get("db_load"):
        navigate(url, timeout)
        wait_until(lambda s: s.get("scene") in ("Game", "Intro", "Deploy") or not s.get("bridge"), timeout, "the world")
        out["reloaded"] = {"save": q["db_load"][0]}
    else:
        t_menu({"timeout_s": timeout})
        out["reloaded"] = "main menu"
    if out["reloaded"] != "main menu":  # the world scene starts before every mod has run
        end = time.time() + 60
        while time.time() < end and not js("return !!(mcp && mcp.reload && mcp.reload.done);", timeout=5):
            time.sleep(0.3)
    out["seconds"] = round(time.time() - t0, 1)
    if out.get("saved"):
        out["saved"]["deleted_after_load"] = trash_reload_save(out["saved"])
        out["saved"].pop("_last", None)
    res = js("need(); const R = mcp.reload, ext = S.session && S.session.externalMods;" + f"""
      const changed = {{}}, errors = {{}}, oldCode = [];
      for (const [id, files] of Object.entries(R.mods)) for (const [f, what] of Object.entries(files)) {{
        if (what === "changed") (changed[id] = changed[id] || []).push(f);
        else if (what.startsWith("error")) errors[id + "/" + f] = what;
      }}
      // A save keeps the mod order it was created with: a mod ordered before the bridge ran its game-start copy.
      for (const m of (ext && ext.orderedMods) || []) {{
        const id = m.manifest.id;
        if (id !== {json.dumps(MOD_ID)} && m.manifest.entry && !(R.mods[id] || {{}})[m.manifest.entry]) oldCode.push(id);
      }}
      const failed = ((ext && ext.statuses) || []).filter(s => s.status !== "loaded").map(s => s.id + ": " + s.status + (s.error ? " (" + s.error + ")" : ""));
      const workerFails = [];
      for (const t of (ext && ext.workerResults) || []) for (const r of (t && t.entries) || []) if (r && !r.ok && r.error && !workerFails.includes(r.id + ": " + r.error)) workerFails.push(r.id + ": " + r.error);
      if (ext && !ext.workerResults && ext.workerEntries && ext.workerEntries.length) workerFails.push("no worker results: a worker entry threw, and the game then drops them all");
      return {{readMs: Math.round(R.readMs), mods: Object.keys(R.mods).length, changedSinceStart: changed, errors, failed, workerFails, oldCode}};""")
    out["code"] = {k: v for k, v in res.items() if v not in ({}, [], None)}
    if res.get("oldCode"):
        out["warning"] = ("These mods load before the bridge in this world's saved mod order, so they ran the copy read at game start: "
                          + ", ".join(res["oldCode"]) + ". Use a new_world (the bridge loads first there).")
    return out


@tool("patches_status","Which mod patches (patches.json entries) applied at the last game start, from main.log (works without the game). "
      "Lists failures with their reason; applied patches are counted per mod, listed with verbose:true.",
      {"mod": {"type": "string", "description": "only mods whose id contains this"}, "verbose": {"type": "boolean", "default": False}})
def t_patches_status(a):
    import re
    try:
        with open(MAIN_LOG, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 3_000_000))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError as e:
        raise ToolError(f"Can't read {MAIN_LOG}: {e}")
    # The main process logs a run's patches right after "Loaded N patch(es)" / "Setting up protocol interceptor".
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if "[workshop]" in lines[i] and ("Loaded " in lines[i] and "patch(es)" in lines[i] or "Skipping protocol interceptor" in lines[i]):
            start = i
            break
    if start is None:
        return {"file": MAIN_LOG, "note": "No patch run found in main.log (no game start since the log rotated?)."}
    stamp = lines[start][1:25]
    applied_re = re.compile(r"\[workshop\] Applied patch (\S+?):(\S+) to (\S+)")
    warn_re = re.compile(r"\[workshop\] Patch (\S+?):(\S+) to (\S+) (.*)")
    want = (a.get("mod") or "").lower()
    mods, failed = {}, []
    for line in lines[start:]:
        if "[workshop]" not in line:
            if "[startup] window:created" in line:
                break
            continue
        m = applied_re.search(line)
        if m:
            mod, pid, f = m.groups()
            if want in mod.lower():
                mods.setdefault(mod, {"applied": 0, "failed": 0, "patches": []})
                mods[mod]["applied"] += 1
                mods[mod]["patches"].append(f"{pid} → {f}")
            continue
        m = warn_re.search(line)
        if m and want in m.group(1).lower():
            mod, pid, f, why = m.groups()
            mods.setdefault(mod, {"applied": 0, "failed": 0, "patches": []})
            mods[mod]["failed"] += 1
            failed.append({"mod": mod, "patch": pid, "file": f, "reason": why.removesuffix("; skipping").strip()})
    if not a.get("verbose"):
        for v in mods.values():
            del v["patches"]
    head = lines[start]
    total = re.search(r"Loaded (\d+) patch", head)
    return {"run": stamp, "loaded": int(total.group(1)) if total else 0, "mods": mods, "failed": failed,
            "note": "A mod missing here has no patches, or wasn't enabled at that start."}


# ---------------------------------------------------------------- MCP stdio

def call_tool(name, args):
    if name not in TOOLS:
        raise ToolError(f"Unknown tool {name}")
    return TOOLS[name]["fn"](args or {})


def to_content(result):
    if isinstance(result, dict) and "__image__" in result:
        return [{"type": "image", "data": result["__image__"], "mimeType": result["mime"]}, {"type": "text", "text": result["text"]}]
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1)
    if len(text) > 60000:
        text = text[:60000] + "\n… (truncated)"
    return [{"type": "text", "text": text}]


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sandustry", "version": SERVER_VERSION},
                "instructions": "Drive Sandustry without screen control. Start with game_status, then game_launch if needed. "
                                "Prefer ui_list/game_status/logs over screenshot."}
    if method == "tools/list":
        return {"tools": [t["schema"] for t in TOOLS.values()]}
    if method == "tools/call":
        p = msg.get("params", {})
        try:
            return {"content": to_content(call_tool(p.get("name"), p.get("arguments")))}
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # noqa: BLE001 — report, keep serving
            log(traceback.format_exc())
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    if method == "ping":
        return {}
    if mid is not None:
        raise LookupError(method)
    return None


def serve():
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        try:
            result = handle(msg)
            if mid is None:
                continue
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        except LookupError as e:
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {e}"}}
        out.write(json.dumps(reply).encode("utf-8") + b"\n")
        out.flush()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--install-mod":
        print(json.dumps(install_bridge(), indent=1))
    elif len(sys.argv) >= 3 and sys.argv[1] == "--call":
        sys.stdout.reconfigure(encoding="utf-8")
        try:
            r = call_tool(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
            if isinstance(r, dict) and "__image__" in r:
                r = {k: v for k, v in r.items() if k != "__image__"}
            print(json.dumps(r, indent=1, ensure_ascii=False))
        except ToolError as e:
            print("ERROR:", e)
            sys.exit(1)
    else:
        serve()
