"""MCP server for DaVinci Resolve (free edition) through a Lua bridge.

Free Resolve 21.1 only lets Lua scripts inside the app reach its API, and that Lua
cannot open sockets or write files. So the two sides talk through a mailbox folder:

- requests: this server writes ``mailbox/req_<n>.lua`` (a Lua table the bridge
  ``dofile``s), then bumps ``mailbox/seq.lua``;
- results: the bridge stores hex-encoded JSON in a Fusion pref and ``SavePrefs`` it
  to ``mailbox/resp_<n>.prefs``.

Start the bridge in Resolve with Workspace > Console:
    dofile(os.getenv("HOME").."/resolve-claude-mcp/bridge.lua")
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
BOX = Path(os.environ.get("RESOLVE_CLAUDE_MAILBOX", ROOT / "mailbox"))
RESP_RE = re.compile(rb'"CBR1:([0-9a-f]*):END"')
START_HINT = (
    "The Resolve bridge isn't answering. In DaVinci Resolve open a project, then "
    'Workspace > Console and run: dofile(os.getenv("HOME").."/resolve-claude-mcp/bridge.lua")'
)

_lock = threading.Lock()


def _lua_str(s: str) -> str:
    out = []
    for b in s.encode("utf-8"):
        if b in (34, 92) or b < 32 or b >= 127:
            out.append(f"\\{b:03d}")
        else:
            out.append(chr(b))
    return '"' + "".join(out) + '"'


def to_lua(v: Any) -> str:
    """Serialise JSON-like data as a Lua literal (ASCII only)."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else "0"
    if isinstance(v, str):
        return _lua_str(v)
    if isinstance(v, (list, tuple)):
        return "{" + ", ".join(to_lua(x) for x in v) + "}"
    if isinstance(v, dict):
        return "{" + ", ".join(f"[{_lua_str(str(k))}]={to_lua(x)}" for k, x in v.items() if x is not None) + "}"
    raise TypeError(f"can't send {type(v).__name__} to Resolve")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="ascii")
    os.replace(tmp, path)


def _read_seq() -> int:
    try:
        m = re.search(r"-?\d+", (BOX / "seq.lua").read_text())
        return int(m.group()) if m else 0
    except FileNotFoundError:
        return 0


def _cleanup(upto: int) -> None:
    for p in [*BOX.glob("req_*"), *BOX.glob("resp_*")]:
        m = re.match(r"(?:req|resp)_(\d+)\.", p.name)
        if m and int(m.group(1)) <= upto:
            p.unlink(missing_ok=True)


def call(op: str, args: dict | None = None, code: str | None = None, timeout: float = 30.0) -> Any:
    """Send one operation to the bridge and wait for its result."""
    with _lock:
        BOX.mkdir(parents=True, exist_ok=True)
        os.chmod(BOX, 0o700)
        n = _read_seq() + 1
        _cleanup(n - 1)
        body = f"return {{op={to_lua(op)}, args={to_lua(args or {})}"
        if code is not None:
            body += f", fn=function(ctx)\n{code}\nend"
        body += "}\n"
        _write_atomic(BOX / f"req_{n}.lua", body)
        _write_atomic(BOX / "seq.lua", f"return {n}\n")

        resp = BOX / f"resp_{n}.prefs"
        deadline = time.monotonic() + timeout
        data = None
        while time.monotonic() < deadline:
            if resp.exists():
                m = RESP_RE.search(resp.read_bytes())
                if m:
                    data = json.loads(bytes.fromhex(m.group(1).decode()).decode("utf-8", "replace"))
                    break
            time.sleep(0.05)
        (BOX / f"req_{n}.lua").unlink(missing_ok=True)
        resp.unlink(missing_ok=True)

    if data is None:
        raise RuntimeError(START_HINT)
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "unknown error"))
    return data.get("result")


mcp = FastMCP(
    "resolve-free",
    instructions=(
        "Controls the user's running DaVinci Resolve (free edition) through a Lua bridge. "
        "Call resolve_status first. Timeline item start/end are absolute timeline frames; "
        "marker frames are offsets from the timeline start. Tracks and item indexes are 1-based. "
        "Use run_lua for anything the other tools don't cover. " + START_HINT
    ),
)


@mcp.tool()
def resolve_status() -> dict:
    """Check the bridge is running; returns Resolve version, page, open project and timeline."""
    return call("ping", timeout=5)


@mcp.tool()
def get_project() -> dict:
    """Current project: name, timelines, frame rate, resolution, current page."""
    return call("project_info")


@mcp.tool()
def get_timeline(timeline: str | None = None) -> dict:
    """Tracks, clips (with frames and timecodes), markers and playhead of a timeline (default: current)."""
    return call("timeline_info", {"timeline": timeline})


@mcp.tool()
def list_media_pool() -> list:
    """Every clip in the media pool with id, folder, file path, duration and fps."""
    return call("media_pool")


@mcp.tool()
def import_media(paths: list[str], folder: str | None = None) -> list:
    """Import files (absolute paths) into the media pool, optionally into a folder like "Master/Footage"
    (created if missing)."""
    return call("import_media", {"paths": [os.path.abspath(os.path.expanduser(p)) for p in paths], "folder": folder}, timeout=120)


@mcp.tool()
def create_timeline(name: str, clips: list[str] | None = None) -> dict:
    """Create a timeline, empty or from media pool clips (names or ids), and switch to it."""
    return call("create_timeline", {"name": name, "clips": clips}, timeout=60)


@mcp.tool()
def append_clips(clips: list[dict], timeline: str | None = None) -> list:
    """Append media pool clips to a timeline. Each entry: {"clip": name or id, optional "start_frame",
    "end_frame" (source frames), "track" (1-based), "record_frame" (absolute timeline frame),
    "media_type" (1 video only, 2 audio only)}."""
    return call("append_clips", {"clips": clips, "timeline": timeline}, timeout=60)


@mcp.tool()
def switch_timeline(name: str) -> dict:
    """Make a timeline the current one."""
    return call("switch_timeline", {"name": name})


@mcp.tool()
def get_markers(timeline: str | None = None) -> list:
    """Timeline markers with frame offset, timecode, color, name and note."""
    return call("markers", {"timeline": timeline})


@mcp.tool()
def add_marker(
    timecode: str | None = None,
    frame: int | None = None,
    color: str = "Blue",
    name: str = "",
    note: str = "",
    duration: int = 1,
    timeline: str | None = None,
) -> dict:
    """Add a timeline marker at a timecode (HH:MM:SS:FF) or frame offset from the timeline start.
    Colors: Blue, Cyan, Green, Yellow, Red, Pink, Purple, Fuchsia, Rose, Lavender, Sky, Mint, Lemon,
    Sand, Cocoa, Cream."""
    return call("add_marker", {"timecode": timecode, "frame": frame, "color": color, "name": name,
                               "note": note, "duration": duration, "timeline": timeline})


@mcp.tool()
def delete_markers(color: str | None = None, frame: int | None = None, timeline: str | None = None) -> dict:
    """Delete the marker at a frame offset, or all markers of a color ("All" for every marker)."""
    return call("delete_markers", {"color": color, "frame": frame, "timeline": timeline})


@mcp.tool()
def set_playhead(timecode: str, timeline: str | None = None) -> dict:
    """Move the playhead to a timecode (HH:MM:SS:FF)."""
    return call("set_playhead", {"timecode": timecode, "timeline": timeline})


@mcp.tool()
def open_page(page: str) -> dict:
    """Switch Resolve page: media, cut, edit, fusion, color, fairlight, deliver."""
    return call("open_page", {"page": page})


@mcp.tool()
def delete_clips(items: list[dict], ripple: bool = False, timeline: str | None = None) -> dict:
    """Delete timeline clips. Each item: {"track_type": "video"|"audio"|"subtitle", "track": 1-based,
    "index": 1-based position in that track, as listed by get_timeline}."""
    return call("delete_clips", {"items": items, "ripple": ripple, "timeline": timeline})


@mcp.tool()
def set_clip(
    track: int,
    index: int,
    track_type: str = "video",
    properties: dict | None = None,
    color: str | None = None,
    enabled: bool | None = None,
    timeline: str | None = None,
) -> dict:
    """Change a timeline clip: Inspector properties (e.g. {"ZoomX": 1.2, "Pan": 100, "Opacity": 50,
    "RotationAngle": 5}), clip color, or enabled state."""
    result = call("set_clip", {"track": track, "index": index, "track_type": track_type, "properties": properties,
                               "color": color, "enabled": enabled, "timeline": timeline})
    refresh_viewer()
    return result


@mcp.tool()
def render_info() -> dict:
    """Render presets, formats, current format/codec, queue jobs and whether a render is running."""
    return call("render_info")


@mcp.tool()
def add_render_job(
    target_dir: str,
    name: str,
    preset: str | None = None,
    format: str | None = None,
    codec: str | None = None,
    mark_in: int | None = None,
    mark_out: int | None = None,
    timeline: str | None = None,
) -> dict:
    """Queue a render of a timeline (whole timeline unless mark_in/mark_out frames are given).
    Optionally load a preset first, or set format+codec (e.g. "mp4", "H264")."""
    return call("add_render_job", {"target_dir": os.path.abspath(os.path.expanduser(target_dir)), "name": name,
                                   "preset": preset, "format": format, "codec": codec, "mark_in": mark_in,
                                   "mark_out": mark_out, "timeline": timeline})


@mcp.tool()
def start_render(job_ids: list[str] | None = None) -> dict:
    """Start rendering the given queued jobs, or the whole queue."""
    return call("start_render", {"job_ids": job_ids})


@mcp.tool()
def stop_render() -> dict:
    """Stop the current render."""
    return call("stop_render")


@mcp.tool()
def save_project() -> dict:
    """Save the current project."""
    return call("save_project")


REFRESH_LUA = """
local t = ctx.timeline
if not t then return end
local tc = t:GetCurrentTimecode()
local fps = tonumber(t:GetSetting("timelineFrameRate")) or 24
local n = math.floor(fps + 0.5)
local h, m, s, f = tc:match("^(%d+):(%d+):(%d+)[:;](%d+)$")
if not h then return end
local frames = ((tonumber(h) * 60 + tonumber(m)) * 60 + tonumber(s)) * n + tonumber(f) + 1
local sec = math.floor(frames / n)
t:SetCurrentTimecode(string.format("%02d:%02d:%02d:%02d", math.floor(sec / 3600), math.floor(sec / 60) % 60, sec % 60, frames % n))
bmd.wait(0.1)
t:SetCurrentTimecode(tc)
"""


def refresh_viewer() -> None:
    """Script edits to Fusion comps don't redraw a paused Edit-page viewer; nudge the playhead so it does."""
    try:
        call("run_lua", code=REFRESH_LUA, timeout=5)
    except RuntimeError:
        pass


@mcp.tool()
def run_lua(code: str, timeout: float = 30, refresh: bool = True) -> Any:
    """Run Lua inside Resolve for anything the other tools don't cover. The code is the body of
    function(ctx); ctx has resolve, fusion, bmd, project, timeline, media_pool. `return` a value
    (tables become JSON). Free-edition Lua has no io/require/os.remove. API reference:
    /Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/README.txt
    With refresh (default) the playhead is nudged afterwards so the viewer shows the change."""
    result = call("run_lua", code=code, timeout=timeout)
    if refresh:
        refresh_viewer()
    return result


COMMIT_FUSION_LUA = """
local t = ctx.timeline
local back = resolve:GetCurrentPage()
t:SetCurrentTimecode(TIMECODE)
resolve:OpenPage("fusion")
bmd.wait(1.5)
local comp = ctx.fusion:GetCurrentComp()
if not comp then error("the Fusion page has no composition at that timecode") end
local touched
comp:StartUndo("Claude commit")
for k, tool in pairs(comp:GetToolList(false) or {}) do
  if type(k) == "number" and tool.Blend then
    local v = tool:GetInput("Blend", comp.CurrentTime)
    if type(v) == "number" then
      tool:SetInput("Blend", v + 0.001, comp.CurrentTime)
      tool:SetInput("Blend", v, comp.CurrentTime)
      touched = tool:GetAttrs().TOOLS_Name
      break
    end
  end
end
comp:EndUndo(true)
bmd.wait(0.5)
resolve:OpenPage(back == "fusion" and "edit" or back)
local names = {}
for k, tool in pairs(comp:GetToolList(false) or {}) do
  if type(k) == "number" then names[#names + 1] = tool:GetAttrs().TOOLS_Name end
end
return {touched = touched, tools = names}
"""


@mcp.tool()
def commit_fusion(timecode: str) -> dict:
    """Make script-made Fusion changes show on the Edit page and in renders. Resolve only picks up a
    clip's Fusion comp once it has been edited on the Fusion page, so this opens that page with the
    playhead at `timecode` (pick a frame where the target clip is the TOP video clip), makes a no-op
    edit inside an undo block, and returns to the previous page. Call it after building or changing
    Fusion comps with run_lua."""
    return call("run_lua", code=COMMIT_FUSION_LUA.replace("TIMECODE", to_lua(timecode)), timeout=30)


@mcp.tool()
def stop_bridge() -> dict:
    """Stop the bridge loop running inside Resolve."""
    return call("stop", timeout=5)


if __name__ == "__main__":
    mcp.run()
