# MCP Bridge: drive Sandustry without screen control

An MCP server that lets a coding agent start, navigate and play Sandustry from tool calls (saves,
new worlds, buttons by text, keys, teleport, logs, screenshots) instead of taking over the screen.
Verified in game on 0.6.0, 2026-10-28.

```
Claude Code ──stdio MCP──> server/sandustry_mcp.py ──DevTools protocol (127.0.0.1:9222)──> game page
                                                                                           └ mod/ publishes window.__sandustryMcp
```

- **`server/sandustry_mcp.py`**: Python 3.10, standard library only (no Node on this machine).
  It launches `Sandustry.exe --remote-debugging-port=9222` directly (Steam must be running). Electron
  honours the switch and binds it to 127.0.0.1. Each tool is then a `Runtime.evaluate`, `Input.*`
  or `Page.captureScreenshot` call on the game page.
- **`mod/`** (`guillaume.mcpbridge`): a Sandkit mod that runs in the main menu and in game. It puts
  the mod's `sandkit` object on `window.__sandustryMcp` (`sandkit.state` is the full game state,
  `sandkit.engine.api` is the game's own internal API) and keeps a log buffer (console, errors,
  events, with repeats collapsed). It opens no port and makes no network calls.

## Setup

Needs Windows, Python 3.10 or newer (standard library only), and Sandustry on Steam. This is a
developer tool: it's released here and is not on the Workshop.

1. Download `sandustry-mcp-bridge-<version>.zip` from the
   [releases](https://github.com/Asekhan/Sandustry-mcp-bridge/releases) and unzip it anywhere.
2. Install the companion mod into `%APPDATA%\sandustry\mods\guillaume.mcpbridge`:
   ```bash
   python -I server/sandustry_mcp.py --install-mod
   ```
3. Register the server with your MCP client. For Claude Code, put this in a project's `.mcp.json`
   (use the absolute path to where you unzipped it):
   ```json
   {
     "mcpServers": {
       "sandustry": {
         "command": "python",
         "args": ["-I", "C:/path/to/sandustry-mcp-bridge/server/sandustry_mcp.py"],
         "env": { "PYTHONIOENCODING": "utf-8" }
       }
     }
   }
   ```
   The tools appear as `mcp__sandustry__*` in the next session.
4. Call `game_launch`. It also works as `game_launch restart:true` if the game is already running
   without the port. The server finds `Sandustry.exe` in your Steam libraries; set
   `SANDUSTRY_EXE` if it doesn't. `SANDUSTRY_APPDATA` and `SANDUSTRY_DEVTOOLS_PORT` override the
   data folder and the port (9222).

One-shot calls for scripts and debugging, without an MCP client:

```bash
python -I server/sandustry_mcp.py --call saves_list "{\"limit\":5}"
```

**Security:** while the game runs this way, any local process can drive it through
127.0.0.1:9222, as with any DevTools port. Use it on your own machine.

## Tools

| Tool | What it does |
|---|---|
| `game_status` | Running? scene (MainMenu / Intro / Deploy / Game), world, player cell, tutorial state |
| `game_launch` / `game_quit` | Start with the DevTools port and wait for the menu buttons / quit (`force` kills) |
| `saves_list` | Reads save headers from disk (works without the game) |
| `save_load` | Loads by id or name the way the Load window does (`?db_load=<id>`) and waits for the world |
| `save_game` | New save, or overwrite with `id` (`api.game.save`) |
| `save_trash` | Moves a save and its .backup to `saves/.mcp-trash/` (reversible); needs `confirm` = exact id |
| `main_menu` | Back to the menu **without saving** |
| `new_world` | `?new_game=true&seed&name`, then skips intro, opening story dialogue and tutorial (each optional) |
| `skip_intro` / `skip_story` / `skip_tutorial` | The individual skips (see below) |
| `ui_list` / `ui_click` / `ui_type` | Visible clickables with text and position; click by text, id or selector (real mouse event) |
| `key` / `move` / `mouse` | CDP key and mouse input; `move` holds A/D/W/S/Space for `ms` |
| `player` / `teleport` | Position in px and cells (1 cell = 4 px); teleport stops velocity and snaps the camera |
| `logs` | Bridge log buffer (`level`, `grep`, `since_seq`, `full` for stacks) or the tail of `main.log` |
| `screenshot` | Scaled JPEG of the game view (works with the window in the background) |
| `eval` | Async JS in the page with `S` (state), `api` (engine API), `sk`, `mcp` in scope |
| `state` | Browse a state path (`store.player`, `api.player`, `window.x`); arrays paged, `find` by text or `key=value` |
| `api_list` / `api_call` | Discover the engine API (90+ namespaces, parameter lists, source heads) / call one with the state passed first |
| `cells` / `paint` | Character grid of a cell rectangle with legend (`around_player`) / fill a rectangle with an element, terrain or empty |
| `snapshot` / `snapshot_diff` | Keep a copy of a state path in the server, then list changed/added/removed leaves |
| `events_watch` / `events_read` | Record named game events (`building:removed`, `factory:levelUp`…) in bounded buffers |
| `perf` | rAF sample: fps, frame-time p50/p95/max, heap, structure count |
| `cdp` | Raw DevTools Protocol command |
| `wait_for` | Wait for a scene or a JS condition |
| `mod_install` | Copies `mod/` into `%APPDATA%\sandustry\mods` |

Input is focus-emulated (`Emulation.setFocusEmulationEnabled`), so the game takes keys and clicks
while its window is behind other windows. Nothing here needs computer-use.

## What mods can do in the main menu (0.6.0 source + tested)

- **Mod code runs in the main menu.** Each scene change is a page load (`?db_load=…`,
  `?new_game=…`, no query = menu), and every load re-runs all mods. `sandkit.state.store.scene.active`
  is 1 MainMenu, 2 Intro, 3 Deploy, 4 Game. It already says 1 while the splash plays, so wait for the
  menu buttons instead.
- **UI:** `sandkit.api.ui.regions.mount("mainMenu.actions", …)` (Mod Sync) or plain DOM. The menu
  buttons are React `div.cursor-pointer` elements. Their `main-menu-*` ids are focus-navigation
  props, not DOM ids.
- **Saves (the preload `window.electron` bridge):** `getSaveFiles()` (header lines), `load(id)`,
  `deleteSave(id)` (**permanent, and also deletes the Steam Cloud copy**: the MCP never calls it),
  `exportSave`, `importSave`, `getSaveFolder`, `saveExistsSync`, `getLastPlayedGameSync`,
  `customMaps.*`, `localMods.*`, `platform.workshop.*`, `getSettingsSync` / `saveSettings`,
  `appQuit`, `openDevTools`, `log(level, scope, msg)` → main.log.
- **Starting a game:** set `location.search` to `db_load=<id>` (load) or
  `new_game=true&seed=…&name=…` (new world), exactly as the menu does. `skip_version_check` loads
  newer saves.
- **Engine API (`sandkit.engine.api`, takes the state first):** `game.start(S,{skip})`,
  `game.save(S,name,id?)`, `game.load(S,id)`, `player.setPosition/setVelocity/getPosition`,
  `camera.snapToPlayer/setFocusAtWorld`, `progression.complete(S,{domain:"tutorial",grantNormalUnlocks})`,
  `ui.update(S, componentId)`, `debug.register(...)`, and more. The game's own quick-save debug
  panel uses the same calls.
- **Skipping the onboarding:**
  - Intro: the Skip Intro button runs `game.start(S,{skip:true})`, stops the intro music and clears
    `S.session.notifications.story`. `game.start` alone leaves the Mission Brief cinematic, so
    `skip_intro` presses the button.
  - Story dialogue (ZOE): queued in `S.session.notifications.story` once the world runs. Clear it
    and re-render component 21 (StoryNotifications). Story objectives keep tracking.
  - Tutorial: `progression.complete(S,{domain:"tutorial",grantNormalUnlocks:true})` is what Skip
    Tutorial does (unlocks Shaker and the normal starting tech). It is saved with the world.
- **Debug mode:** `localStorage["debug.active"]="true"` + reload exposes `window.__debug` (state,
  `admin.run("gun" | "teleporter" | …)`). It isn't needed with the bridge mod, and it turns on
  debug overlays.

## Related

[LopeKinz/SandLoader-Mods](https://github.com/LopeKinz/SandLoader-Mods) ("MCP Ultimate Debug
Bridge") does the in-world half on the third-party SandLoader. It needs SandLoader's native `node`
permission and runs an unauthenticated TCP server on 127.0.0.1:47651. By its README it doesn't
cover the main menu or saves. Ours needs no loader and no in-game server. Not downloaded, not reused.
Its tool list (from the README) inspired `state` paging, `api_list`/`api_call`, `cells`/`paint`,
snapshots, event probes, `perf` and `cdp`. Its DOM, scoped-file and worker tools are covered by
`eval`, `ui_*` and the shell.

Cell ids: `0` is empty, terrain cells hold their terrain type, element cells pack the element type
with per-cell data (sand reads e.g. `1016453`). Decode with `api.terrains.isCellIdTerrain(id)` and
`api.elements.getResolvedTypeFromCellId(S, id)`.

## Known gaps

- Menu-ready detection matches English button labels (`New` / `Continue` / `Load`).
- Workshop mods' own popups (e.g. SpaceWorld's "Atmosphere reservation") are left alone: `ui_click text:"OK"`.
- `save_trash` leaves the Steam Cloud copy; the cloud could bring the save back on another machine.
