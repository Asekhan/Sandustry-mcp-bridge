---
name: sandustry-testing
description: Test Sandustry mods and engine behaviour in the running game through the sandustry MCP server (mcp__sandustry__* tools) instead of screen control. Use whenever a task needs the game open — checking a mod loads, trying a mechanic, setting up a test world, reading game logs, inspecting state or cells, measuring performance.
---

# Testing in Sandustry through the MCP bridge

The `sandustry` MCP server drives the game through its DevTools port. Every tool is a cheap
text call; screenshots are the expensive exception. Do not use computer-use for the game.

If the `mcp__sandustry__*` tools aren't loaded, run the same tool from a shell:
`python -I <bridge>/server/sandustry_mcp.py --call <tool> "<json args>"`. Snapshots don't
survive between one-shot calls; everything else does.

## Start

1. `game_status`. If not reachable, `game_launch` (add `restart:true` when the game is running
   without the port; that loses unsaved progress, so say so first).
2. Mod files are read at game start. After editing a mod's JS, don't restart:
   `mod_reload {path:"<mod source folder>"}` copies it in (like `mod_install {path}`) and reloads
   the page with that mod's main/worker/worldgen code read fresh from disk; it stays "live" (fresh
   at every page load) until `mod_reload {id, forget:true}`. In a world it saves to
   "MCP reload (<world>)" first and reloads into that save, so progress is kept (trash that save
   when done); in the menu it reloads the menu. About 6-10 s. `mod_reload {id, disable:true}`
   switches a mod's code off. A new mod, or a change to `modinfo.json`, assets or `patches.json`,
   still needs `game_launch restart:true` (`mod_reload` lists such files under `needs_restart`).
   After a restart, `patches_status {mod}` says which `patches.json` entries applied and why the
   others didn't. If one worker entry throws, the game drops all worker results for that load.
3. `game_launch` returns once the main menu buttons exist; the bridge mod reports `bridge: <version>`.
   `bridge: null` means the companion mod isn't installed (`mod_install`, then restart).

## Get a world

- Throwaway test: `new_world {seed, name}`. It skips the intro cinematic, the opening story
  dialogue and the tutorial (normal starting unlocks granted). Use a fixed seed to reproduce.
- Existing save: `saves_list {filter}`, then `save_load {save}`.
  **Loading someone's real save lets autosave rotate its autosave slots** — prefer a new world,
  or ask before loading their saves.
- Other mods' popups (e.g. an "OK" alert) stay up: `ui_list` and `ui_click {text:"OK", exact:true}`.

## Look (cheapest first)

| Need | Tool |
|---|---|
| Where am I, what scene | `game_status`, `player` |
| What's around | `cells {around_player:true, w:48, h:24}` — char grid + legend, P = player |
| What's on screen to click | `ui_list {filter}` |
| Errors / mod output | `logs {level:"warn,error"}`; `logs {source:"main"}` for main.log (works with the game closed) |
| A value in state | `state {path:"store.player", depth:2}`; arrays paged, `find:"key=value"` |
| How much of each element in an area | `count {x, y, w, h}` — totals per element/terrain |
| Did X change anything | `snapshot {name, path}` → act → `snapshot_diff {a:name}` |
| Did an event fire | `events_watch {names:[...]}` → act → `events_read` |
| Frame rate | `perf {ms:3000}` |
| Visual check only | `screenshot {scale:0.4}` |

## Act

- Move: `move {direction:"left"|"right"|"up"|"down"|"jump", ms}`; `key {keys:"e"}` / `"ctrl+z"`.
- Place: `teleport {x, y}` (cells; 1 cell = 4 px). Coordinates from `cells`/`player`.
- World edits: `paint {x, y, w, h, element:"sand"|terrain:"stone"|empty:true}` (painted terrain
  is not kept by a save/load, painted elements are). Elements fall/flow
  right away; check with `cells` after a short wait. `remove_element {x, y, w, h}` clears loose
  elements only (terrain stays).
- Tech: `tech_unlock_all` researches every node with the game's own unlock (no cost; locks and
  factory row gates bypassed; factory level raised to max unless `factory_max:false`). One node:
  `tech_unlock {id:"magnet"}`; `tech_unlock {}` lists them. Never use the cheat panel's
  "Everything": it resets gold, completes dungeons and flags the save as cheated.
- Structures: `build {type:"filterWall", x, y, filter:"sand"}` places at once without cost and
  returns the stored structure (filter, data); `filter` also takes a list or
  `{elements, mode:"deny"}`, `data` merges over the defaults. `remove_structure {x, y, w, h}`.
- UI: `ui_click {text}` (real mouse event at the element's centre), `ui_type`, `mouse {x, y}`.
- Engine API: `api_list` (namespaces) → `api_list {path:"structures"}` (parameters) →
  `api_call {path:"structures.getAtCell", args:[x, y]}`. The state is passed first for you.
- Anything else: `eval {code}` with `S` (state), `api` (engine API), `sk` (sandkit), `mcp` in scope.

## Save hygiene

- `save_game {name}` makes a new save; `save_game {id}` overwrites — ask before overwriting
  someone's save.
- `main_menu` leaves **without saving**.
- Delete only with `save_trash {save, confirm:<exact id>}`: it moves the file to
  `saves/.mcp-trash/` (reversible). Never call `window.electron.deleteSave` — it is permanent and
  also deletes the Steam Cloud copy.
- Clean up test worlds you saved: `saves_list {filter:"<your test name>"}` then `save_trash`.
- `game_quit` when done, unless the user is using the game.

## Engine facts that save time

- Scenes: 1 MainMenu, 2 Intro, 3 Deploy, 4 Game (`S.store.scene.active`). Scene changes are page
  reloads (`?db_load=<id>`, `?new_game=true&seed=&name=`); every mod re-runs on each.
- Cell ids: 0 empty; terrain cells hold their terrain type; element cells pack the type with
  per-cell data — decode with `api.terrains.isCellIdTerrain(id)` and
  `api.elements.getResolvedTypeFromCellId(S, id)`. `cells` already does this.
- Engine API functions take the state first: `api.player.setPosition(S, xPx, yPx)`.
