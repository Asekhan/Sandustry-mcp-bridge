# MCP bridge wishlist

Commands an agent needed during real tasks and had to dig for (source reading, webpack, long
`eval`s). Candidates for first-class tools. Add to the list as they come up; say what it took.

| Wanted | What it took (4 Nov, filter test) |
|---|---|
| ✅ `tech_unlock_all` (and `tech_unlock {id}`) — **done** (4 Nov: fresh world 2 → 90 nodes, 12 locks lifted, factory 1 → 7, gold and cheat flag untouched) | `api.tech` has no research call. Took the webpack require (`webpackChunksand_v1.push`), module `77135` `unlockTech(S, node, {skipCostCheck})`, raising factory level to max (module `48525` `Q7(S, pc)`, tech rows are gated by it), `session.cheat.bypassCosts` on during the calls, and for the 12 `native.*` Mk2 nodes at row 26 setting `store.player.tech[id]` plus `api.player.buildings.add` by hand. The game's own cheat panel "Unlock → Everything" does much more (augments, dungeons, gold reset, cheat flag). |
| ✅ `build {type, x, y, filter, data}` — **done** (sand-only filter holds gold, lets sand through; deny and multi-element filters read back) | `api.structures.build(S, {x, y}, type, {instant: true, copiedStructure: {filter}})`; the filter option isn't documented anywhere. Return the placed structure. |
| ✅ `count {x, y, w, h}` per element type — **done**, tool `count` (tested 4 Nov) | `cells` gives a grid; tallying needed a loop over `getResolvedTypeAtCell`. |
| ✅ `remove_structure {x, y}` — **done**, also clears the building's cells (`removeCells`, as the demolisher) | `api.structures.removeAt(S, x, y)`. |
| ✅ `remove_element {x, y}` — **done**, rectangle, terrain kept | `api.elements.removeAt(S, x, y)`; `paint empty` works on rectangles only. |
| ✅ `mod_install {path}` for any local mod — **done**, `mod_install {path}` (tested 4 Nov) | `mod_install` installs only the bridge; other mods were copied by hand into `%APPDATA%\sandustry\mods\<id>`. |
| ✅ `patches_status` — **done**, reads the main process's "Applied patch" / skip lines in main.log (tested 4 Nov) | Whether each `patches.json` entry applied: no log line on success. Checked with `fetch("js/simulation-worker.js")` from the page and a regex. |
| Unlocked builds in the UI after tech changes — the tech tools call `api.ui.update(S, 4)` (Root); build menu not checked by eye | The build menu may need a reopen. A refresh call (`Aq(S, JU.Root)`, not exposed) would make it immediate. |
| ✅ Reload a mod's code without restarting the game — **done**, `mod_reload` (4 Nov) | The main process reads mod code and applies patches once at game start; `window.electron` is frozen. The bridge (loads first) wraps `window.Function` (mods compile as `new Function("__sandkit", …//# sourceURL=sandkit-workshop://<id>/<entry>)`) and `simulation.loadExternalModEntries`, and reads live mods' files with a sync XHR. Patches, manifests, assets and new mods still need a restart. |
| Re-apply `patches.json` without a restart | Not possible from the page: the main process serves patched files from a cache built at start (`protocol.handle("sandustry-patch")`). Would need CDP `Fetch` interception held open by the server for every page load. |
| `paint terrain` that survives a save | `api.terrains.createAt` cells are gone after save + load (the game's own save_load, not only `mod_reload`); painted elements stay. Found 4 Nov; cause not looked at. |
| Reload one mod in place (no page reload) | Not safe in general: the per-mod rollback the game runs on a failed entry (module with `uJ`/`Ie`) releases UI handles and owner-tagged sprites, shader effects, pipe consumers and area gates, but not event listeners, hooks, keybindings or registered content, and the worker runtime skips ids it already loaded. Would need an opt-in hot-swap pattern in our own mods. |
