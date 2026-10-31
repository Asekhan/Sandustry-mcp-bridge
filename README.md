# MCP Bridge for Sandustry

Let an AI agent (Claude Code or any MCP client) play-test your Sandustry mods: start the game,
open a save or a fresh world, click menus, walk, teleport, place sand, read logs and game state.
All of it goes through tool calls, with no screenshots-and-clicks loop. It's a developer tool for
modders, released here and not on the Workshop. Tested on Sandustry 0.6.0 (Windows, Steam).

**What an agent can do with it**

- **Menus and saves:** list, load, save and trash saves; start a new world with the intro, the
  opening story and the tutorial skipped; click any button by its text.
- **Play:** keys, movement, mouse, teleport, player position.
- **Look:** game logs, an ASCII map of the cells around a point, state browsing, screenshots when
  a picture is really needed.
- **Poke:** paint elements or terrain, call any function of the game's engine API, run JavaScript,
  diff state before and after an action, record game events, sample the frame rate.

**How it works**

```
MCP client ──stdio──> server/sandustry_mcp.py ──DevTools protocol, 127.0.0.1:9222──> game window
                                                                                     └ mod/ (asekhan.mcpbridge)
```

The server launches `Sandustry.exe --remote-debugging-port=9222` and drives the game page over
the Chrome DevTools Protocol. It's one Python file using only the standard library. The companion
Sandkit mod exposes the mod API and keeps a log buffer for the server. It adds no content, opens no
port and makes no network calls. It runs in the main menu too, which is where the save and
new-world tools work.

## Installation (for your agent)

The simplest way is to ask your agent: *"Install the Sandustry MCP bridge from
https://github.com/Asekhan/Sandustry-mcp-bridge"*. The steps below are written so it can follow
them, and so can you.

Requirements: Windows, Python 3.10+, Sandustry on Steam.

1. Download `sandustry-mcp-bridge-<version>.zip` from the
   [latest release](https://github.com/Asekhan/Sandustry-mcp-bridge/releases/latest) and unzip it
   to a permanent folder.
2. Install the companion mod (copies `mod/` to `%APPDATA%\sandustry\mods\asekhan.mcpbridge`):
   `python -I server/sandustry_mcp.py --install-mod`
3. Register the server, using the absolute path of `server/sandustry_mcp.py`:
   - Claude Code:
     `claude mcp add --transport stdio --scope user --env PYTHONIOENCODING=utf-8 sandustry -- python -I C:/path/to/sandustry-mcp-bridge/server/sandustry_mcp.py`
   - Other clients: the usual `mcpServers` entry, with command `python`, args
     `["-I", "<path>/server/sandustry_mcp.py"]` and env `{"PYTHONIOENCODING": "utf-8"}`.
4. Claude Code only, optional: copy `skill/sandustry-testing/` to `~/.claude/skills/`. It teaches
   the testing workflow: cheapest way to look, save safety, when to restart.
5. Start a new session so the tools load (`mcp__sandustry__*` in Claude Code), then call
   `game_launch`.

Settings, as environment variables: `SANDUSTRY_EXE` if the game isn't found in the Steam libraries,
`SANDUSTRY_APPDATA` for the data folder, `SANDUSTRY_DEVTOOLS_PORT` (default 9222).

## Usage notes (for your agent)

- Start with `game_status`. If the game isn't reachable, call `game_launch`. If the game is running
  without the DevTools port, use `game_launch restart:true`, which loses unsaved progress: ask the
  user first.
- Mods are read at game start: after installing or editing one, `game_launch restart:true`.
- For test worlds, prefer `new_world {seed, name}` to loading the user's saves: autosave would
  rotate their autosave slots.
- Look cheaply first: `game_status`, `cells {around_player:true}`, `ui_list`, `logs`, `state`. Use
  `screenshot` only for visual checks.
- `main_menu` leaves without saving. `save_trash` moves a save to `saves/.mcp-trash/` and needs the
  exact id as `confirm`. Nothing here deletes a save permanently.
- Popups from other mods stay up: `ui_click {text:"OK", exact:true}`.
- Without an MCP client, every tool also runs from a shell:
  `python -I server/sandustry_mcp.py --call <tool> "<json args>"`.

## Tools

| Area | Tools |
|---|---|
| Game | `game_status`, `game_launch`, `game_quit`, `wait_for`, `mod_install` |
| Saves | `saves_list`, `save_load`, `save_game`, `save_trash`, `main_menu` |
| New world | `new_world`, `skip_intro`, `skip_story`, `skip_tutorial` |
| UI and input | `ui_list`, `ui_click`, `ui_type`, `key`, `move`, `mouse` |
| Player | `player`, `teleport` (cells; 1 cell = 4 px) |
| Look | `logs`, `screenshot`, `state`, `cells` |
| Change | `paint`, `api_list`, `api_call`, `eval`, `cdp` |
| Measure | `snapshot`, `snapshot_diff`, `events_watch`, `events_read`, `perf` |

Each tool describes its own parameters to the client.

## Good to know

- **Security:** while the game runs with the DevTools port, any program on the machine can drive
  it through 127.0.0.1:9222. Use it on your own computer.
- `save_trash` doesn't touch the Steam Cloud copy, so the cloud may bring a save back.
- Modders: in the cell grid, terrain cells hold their terrain type, while element cells pack the
  type with per-cell data. `cells` decodes them with `api.terrains.isCellIdTerrain` and
  `api.elements.getResolvedTypeFromCellId`.

## Related

[MCP Ultimate Debug Bridge](https://github.com/LopeKinz/SandLoader-Mods) by LopeKinz is another
MCP mod for Sandustry. It runs on SandLoader and has a different toolset, with its own dashboard,
mod-config and loader diagnostics. Its tool list inspired several tools here.

MIT license.
