# resolve-free MCP

Lets Claude drive DaVinci Resolve **free edition** (tested on 21.1.1) through a Lua bridge.

## Each time you open Resolve

1. Open a project.
2. Workspace > Console (F6), make sure it's in Lua mode, and run:

   ```
   dofile(os.getenv("HOME").."/resolve-claude-mcp/bridge.lua")
   ```

3. Leave the Console window open (you can keep editing). Ask Claude to stop it, or quit Resolve.

## How it works

Free Resolve 21.1 lets only in-app Lua reach its API, and that Lua has no sockets or file
writing. So `server.py` writes requests as Lua files into `mailbox/`, `bridge.lua` runs them,
and results come back through Fusion's `SavePrefs` as hex-encoded JSON.

## Files

- `bridge.lua`: runs inside Resolve
- `server.py`: the MCP server (registered as `resolve-free` in Claude Desktop and Claude Code)
- `.venv/`: Python environment for the server
- `mailbox/`: request/response files (private, cleaned up automatically)

## Fusion edits made by script

Free 21.1 shows script-built Fusion comps on the Fusion page but ignores them on the Edit page and
in renders until the comp has been edited on the Fusion page. The `commit_fusion` tool does that
(no-op edit inside an undo block). Call it after any Fusion change, with the playhead on a frame
where the target clip is the top video clip.

Blackmagic could close these gaps in a future update. Keep auto-update off.

## Install

```
git clone https://github.com/Shishodious/resolve-claude-mcp ~/resolve-claude-mcp
cd ~/resolve-claude-mcp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Register the server with Claude Code:

```
claude mcp add resolve-free -- ~/resolve-claude-mcp/.venv/bin/python ~/resolve-claude-mcp/server.py
```

The bridge expects the folder at `~/resolve-claude-mcp`.

## Optional: subject mattes (`matte/`)

`matte/matte.py` makes subject mattes with SAM 2.1 for cutout effects.

```
cd matte
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p models && curl -L -o models/sam2.1_hiera_small.pt \
  https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt
```

Model weights aren't in the repo.
