-- Claude bridge for DaVinci Resolve (free edition, Lua).
--
-- Start it from Workspace > Console (Lua mode):
--   dofile(os.getenv("HOME").."/resolve-claude-mcp/bridge.lua")
--
-- Free Resolve 21.1 gates Python and external scripting to Studio, and its Lua
-- has no io/require/os.remove. What it does leave: dofile (so requests arrive as
-- Lua files), bmd.wait (so this loop runs without freezing the UI) and Fusion's
-- SavePrefs (the one file writer, so results go back hex-encoded inside a prefs file).

local VERSION = "1.0.0"
local HOME = os.getenv("HOME")
local BOX = HOME .. "/resolve-claude-mcp/mailbox/"
local POLL = 0.15

local fu = fu or fusion or app
local resolve = resolve or (bmd and bmd.scriptapp and bmd.scriptapp("Resolve"))
if not resolve or not fu then
  print("[claude-bridge] Resolve API not available here")
  return
end

-- JSON ----------------------------------------------------------------------

local function json_str(s)
  return '"' .. s:gsub('[%c"\\]', function(c)
    if c == '"' then return '\\"'
    elseif c == "\\" then return "\\\\"
    elseif c == "\n" then return "\\n"
    elseif c == "\r" then return "\\r"
    elseif c == "\t" then return "\\t"
    end
    return string.format("\\u%04x", c:byte())
  end) .. '"'
end

-- Resolve's lists carry a hidden `__flags` entry; it is never data.
local function list(t)
  local out = {}
  for k, v in pairs(t or {}) do
    if type(k) == "number" then out[#out + 1] = { k, v } end
  end
  table.sort(out, function(a, b) return a[1] < b[1] end)
  for i, kv in ipairs(out) do out[i] = kv[2] end
  return out
end

local function array_len(t)
  local n = 0
  for k in pairs(t) do
    if k ~= "__flags" then
      if type(k) ~= "number" or k < 1 or k % 1 ~= 0 then return nil end
      n = n + 1
    end
  end
  for i = 1, n do
    if t[i] == nil then return nil end
  end
  return n
end

local function encode(v, depth)
  depth = depth or 0
  local tv = type(v)
  if v == nil then return "null" end
  if tv == "boolean" then return tostring(v) end
  if tv == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if v % 1 == 0 and math.abs(v) < 2 ^ 53 then return string.format("%d", v) end
    return string.format("%.17g", v)
  end
  if tv == "string" then return json_str(v) end
  if tv == "table" then
    if depth > 32 then return '"<too deep>"' end
    local parts = {}
    local n = array_len(v)
    if n then
      for i = 1, n do parts[i] = encode(v[i], depth + 1) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    for k, x in pairs(v) do
      if type(x) ~= "function" and k ~= "__flags" then
        parts[#parts + 1] = json_str(tostring(k)) .. ":" .. encode(x, depth + 1)
      end
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  if tv == "function" then return "null" end
  return json_str(tostring(v))
end

local function hex(s)
  return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

local function respond(n, payload)
  fu:SetPrefs("Global.ClaudeBridge.Resp", "CBR1:" .. hex(encode(payload)) .. ":END")
  fu:SavePrefs(BOX .. "resp_" .. n .. ".prefs")
  -- Don't leave the payload in Resolve's own prefs file.
  fu:SetPrefs("Global.ClaudeBridge.Resp", "")
end

-- Resolve helpers -----------------------------------------------------------

local function pm() return resolve:GetProjectManager() end

local function project()
  local p = pm():GetCurrentProject()
  if not p then error("no project is open") end
  return p
end

local function timeline(name)
  local p = project()
  if name then
    for i = 1, p:GetTimelineCount() do
      local t = p:GetTimelineByIndex(i)
      if t and t:GetName() == name then return t end
    end
    error("timeline not found: " .. tostring(name))
  end
  local t = p:GetCurrentTimeline()
  if not t then error("no timeline is open") end
  return t
end

local function try(f, ...)
  local ok, v = pcall(f, ...)
  if ok then return v end
  return nil
end

local function fps_of(t)
  return tonumber(t:GetSetting("timelineFrameRate")) or tonumber(project():GetSetting("timelineFrameRate")) or 24
end

local function frames_to_tc(frames, fps)
  local nominal = math.floor(fps + 0.5)
  local f = math.floor(frames + 0.5)
  local ff = f % nominal
  local s = math.floor(f / nominal)
  return string.format("%02d:%02d:%02d:%02d", math.floor(s / 3600), math.floor(s / 60) % 60, s % 60, ff)
end

local function tc_to_frames(tc, fps)
  local h, m, s, f = tc:match("^(%d+):(%d+):(%d+)[:;](%d+)$")
  if not h then error("bad timecode (want HH:MM:SS:FF): " .. tostring(tc)) end
  local nominal = math.floor(fps + 0.5)
  return ((tonumber(h) * 60 + tonumber(m)) * 60 + tonumber(s)) * nominal + tonumber(f)
end

local function walk(folder, path, out)
  for _, c in ipairs(list(folder:GetClipList())) do out[#out + 1] = { clip = c, folder = path } end
  for _, sub in ipairs(list(folder:GetSubFolderList())) do walk(sub, path .. "/" .. sub:GetName(), out) end
  return out
end

local function all_clips()
  return walk(project():GetMediaPool():GetRootFolder(), "Master", {})
end

local function find_clip(ref)
  for _, e in ipairs(all_clips()) do
    if e.clip:GetUniqueId() == ref then return e.clip end
  end
  for _, e in ipairs(all_clips()) do
    if e.clip:GetName() == ref then return e.clip end
  end
  error("media pool clip not found: " .. tostring(ref))
end

local function clip_info(c, folder)
  local props = try(function() return c:GetClipProperty() end) or {}
  return {
    id = c:GetUniqueId(), name = c:GetName(), folder = folder,
    type = props["Type"], path = props["File Path"], duration = props["Duration"],
    frames = props["Frames"], fps = props["FPS"], resolution = props["Resolution"],
  }
end

local function track_item(t, track_type, track, index)
  local items = t:GetItemListInTrack(track_type, track) or {}
  local it = items[index]
  if not it then error(string.format("no item %s %d #%d", track_type, track, index)) end
  return it
end

local function item_info(it, index, fps)
  local mpi = try(function() return it:GetMediaPoolItem() end)
  local start, finish = it:GetStart(), it:GetEnd()
  return {
    index = index, name = it:GetName(),
    start = start, ["end"] = finish, duration = it:GetDuration(),
    start_tc = frames_to_tc(start, fps), end_tc = frames_to_tc(finish, fps),
    source_in = try(function() return it:GetLeftOffset() end),
    source_start = try(function() return it:GetSourceStartFrame() end),
    source_end = try(function() return it:GetSourceEndFrame() end),
    clip_id = mpi and mpi:GetUniqueId() or nil, clip_name = mpi and mpi:GetName() or nil,
    color = try(function() return it:GetClipColor() end),
    enabled = try(function() return it:GetClipEnabled() end),
  }
end

local function markers_of(t)
  local fps, start = fps_of(t), t:GetStartFrame()
  local out = {}
  for frame, m in pairs(t:GetMarkers() or {}) do
    if type(frame) == "number" then out[#out + 1] = {
      frame = frame, timecode = frames_to_tc(start + frame, fps), color = m.color,
      name = m.name, note = m.note, duration = m.duration, custom_data = m.customData,
    } end
  end
  table.sort(out, function(a, b) return a.frame < b.frame end)
  return out
end

local function set_folder(mp, path)
  local folder = mp:GetRootFolder()
  for part in path:gmatch("[^/]+") do
    if part ~= "Master" then
      local found
      for _, sub in ipairs(list(folder:GetSubFolderList())) do
        if sub:GetName() == part then found = sub break end
      end
      folder = found or mp:AddSubFolder(folder, part)
    end
  end
  mp:SetCurrentFolder(folder)
end

-- Operations ----------------------------------------------------------------

local running = true
local OPS = {}

function OPS.ping()
  local p = pm():GetCurrentProject()
  local t = p and p:GetCurrentTimeline()
  return {
    bridge = VERSION, resolve = resolve:GetVersionString(), product = resolve:GetProductName(),
    page = resolve:GetCurrentPage(), project = p and p:GetName(), timeline = t and t:GetName(),
  }
end

function OPS.project_info()
  local p = project()
  local cur = p:GetCurrentTimeline()
  local tls = {}
  for i = 1, p:GetTimelineCount() do
    local t = p:GetTimelineByIndex(i)
    tls[#tls + 1] = { index = i, name = t:GetName(), current = cur ~= nil and t:GetName() == cur:GetName() }
  end
  return {
    name = p:GetName(), timelines = tls, page = resolve:GetCurrentPage(),
    fps = p:GetSetting("timelineFrameRate"),
    width = p:GetSetting("timelineResolutionWidth"), height = p:GetSetting("timelineResolutionHeight"),
    projects_in_folder = pm():GetProjectListInCurrentFolder(),
  }
end

function OPS.timeline_info(a)
  local t = timeline(a.timeline)
  local fps = fps_of(t)
  local tracks = {}
  for _, kind in ipairs({ "video", "audio", "subtitle" }) do
    for i = 1, t:GetTrackCount(kind) do
      local items = {}
      for idx, it in ipairs(t:GetItemListInTrack(kind, i) or {}) do items[#items + 1] = item_info(it, idx, fps) end
      tracks[#tracks + 1] = {
        type = kind, index = i, name = t:GetTrackName(kind, i),
        enabled = try(function() return t:GetIsTrackEnabled(kind, i) end), items = items,
      }
    end
  end
  return {
    name = t:GetName(), fps = fps, start_frame = t:GetStartFrame(), end_frame = t:GetEndFrame(),
    start_timecode = t:GetStartTimecode(), playhead = t:GetCurrentTimecode(),
    markers = markers_of(t), tracks = tracks,
  }
end

function OPS.media_pool()
  local out = {}
  for _, e in ipairs(all_clips()) do out[#out + 1] = clip_info(e.clip, e.folder) end
  return out
end

function OPS.import_media(a)
  local mp = project():GetMediaPool()
  if a.folder then set_folder(mp, a.folder) end
  local out = {}
  for _, c in ipairs(list(mp:ImportMedia(a.paths))) do out[#out + 1] = clip_info(c, a.folder or "current") end
  if #out == 0 then error("Resolve imported nothing (check the paths)") end
  return out
end

function OPS.create_timeline(a)
  local mp = project():GetMediaPool()
  local t
  if a.clips and #a.clips > 0 then
    local items = {}
    for i, ref in ipairs(a.clips) do items[i] = find_clip(ref) end
    t = mp:CreateTimelineFromClips(a.name, items)
  else
    t = mp:CreateEmptyTimeline(a.name)
  end
  if not t then error("Resolve refused to create the timeline (name taken?)") end
  project():SetCurrentTimeline(t)
  return { name = t:GetName() }
end

function OPS.append_clips(a)
  local p = project()
  if a.timeline then p:SetCurrentTimeline(timeline(a.timeline)) end
  local infos = {}
  for i, c in ipairs(a.clips) do
    local info = { mediaPoolItem = find_clip(c.clip) }
    if c.start_frame then info.startFrame = c.start_frame end
    if c.end_frame then info.endFrame = c.end_frame end
    if c.track then info.trackIndex = c.track end
    if c.record_frame then info.recordFrame = c.record_frame end
    if c.media_type then info.mediaType = c.media_type end
    infos[i] = info
  end
  local made = list(p:GetMediaPool():AppendToTimeline(infos))
  local fps = fps_of(timeline())
  local out = {}
  for i, it in ipairs(made) do out[#out + 1] = item_info(it, i, fps) end
  if #out == 0 then error("Resolve appended nothing") end
  return out
end

function OPS.switch_timeline(a)
  if not project():SetCurrentTimeline(timeline(a.name)) then error("could not switch timeline") end
  return { name = a.name }
end

function OPS.markers(a)
  return markers_of(timeline(a.timeline))
end

function OPS.add_marker(a)
  local t = timeline(a.timeline)
  local frame = a.frame
  if a.timecode then frame = tc_to_frames(a.timecode, fps_of(t)) - t:GetStartFrame() end
  if not frame then error("give frame or timecode") end
  local ok = t:AddMarker(frame, a.color or "Blue", a.name or "", a.note or "", a.duration or 1, a.custom_data or "")
  if not ok then error("Resolve refused the marker (one already at that frame, or bad color?)") end
  return { frame = frame, timecode = frames_to_tc(t:GetStartFrame() + frame, fps_of(t)) }
end

function OPS.delete_markers(a)
  local t = timeline(a.timeline)
  local ok
  if a.frame then ok = t:DeleteMarkerAtFrame(a.frame) else ok = t:DeleteMarkersByColor(a.color or "All") end
  return { deleted = ok and true or false }
end

function OPS.set_playhead(a)
  local t = timeline(a.timeline)
  if not t:SetCurrentTimecode(a.timecode) then error("Resolve refused the timecode") end
  return { playhead = t:GetCurrentTimecode() }
end

function OPS.open_page(a)
  if not resolve:OpenPage(a.page) then error("unknown page: " .. tostring(a.page)) end
  return { page = resolve:GetCurrentPage() }
end

function OPS.delete_clips(a)
  local t = timeline(a.timeline)
  local items = {}
  for i, ref in ipairs(a.items) do items[i] = track_item(t, ref.track_type or "video", ref.track, ref.index) end
  if not t:DeleteClips(items, a.ripple and true or false) then error("Resolve refused to delete the clips") end
  return { deleted = #items }
end

function OPS.set_clip(a)
  local t = timeline(a.timeline)
  local it = track_item(t, a.track_type or "video", a.track, a.index)
  local results = {}
  for k, v in pairs(a.properties or {}) do results[k] = it:SetProperty(k, v) and true or false end
  if a.color then results.color = it:SetClipColor(a.color) and true or false end
  if a.enabled ~= nil then results.enabled = it:SetClipEnabled(a.enabled) and true or false end
  return results
end

function OPS.render_info()
  local p = project()
  local jobs = {}
  for _, j in ipairs(list(p:GetRenderJobList())) do
    local st = try(function() return p:GetRenderJobStatus(j.JobId) end)
    jobs[#jobs + 1] = { id = j.JobId, name = j.OutputFilename, dir = j.TargetDir, timeline = j.TimelineName, status = st }
  end
  return {
    presets = p:GetRenderPresetList(), formats = p:GetRenderFormats(),
    current = p:GetCurrentRenderFormatAndCodec(), rendering = p:IsRenderingInProgress(), jobs = jobs,
  }
end

function OPS.add_render_job(a)
  local p = project()
  if a.timeline then p:SetCurrentTimeline(timeline(a.timeline)) end
  if a.preset and not p:LoadRenderPreset(a.preset) then error("unknown render preset: " .. a.preset) end
  if a.format and a.codec and not p:SetCurrentRenderFormatAndCodec(a.format, a.codec) then
    error("Resolve refused format/codec " .. a.format .. "/" .. a.codec)
  end
  local s = { TargetDir = a.target_dir, CustomName = a.name }
  if a.mark_in then s.MarkIn = a.mark_in s.MarkOut = a.mark_out else s.SelectAllFrames = true end
  if not p:SetRenderSettings(s) then error("Resolve refused the render settings") end
  local id = p:AddRenderJob()
  if not id or id == "" then error("Resolve refused to add the render job") end
  return { job_id = id }
end

function OPS.start_render(a)
  local p = project()
  local ok
  if a.job_ids and #a.job_ids > 0 then ok = p:StartRendering(unpack(a.job_ids)) else ok = p:StartRendering() end
  if not ok then error("Resolve refused to start rendering") end
  return { rendering = true }
end

function OPS.stop_render()
  project():StopRendering()
  return { rendering = project():IsRenderingInProgress() }
end

function OPS.save_project()
  return { saved = pm():SaveProject() and true or false }
end

function OPS.run_lua(_, req)
  if type(req.fn) ~= "function" then error("no code") end
  local p = pm():GetCurrentProject()
  return req.fn({
    resolve = resolve, fusion = fu, bmd = bmd, project = p,
    timeline = p and p:GetCurrentTimeline(), media_pool = p and p:GetMediaPool(),
  })
end

function OPS.stop()
  running = false
  return { stopped = true }
end

-- Loop ----------------------------------------------------------------------

local function read_seq()
  local ok, n = pcall(dofile, BOX .. "seq.lua")
  if ok and type(n) == "number" then return n end
  return nil
end

local function handle(n, path)
  local ok, req = pcall(dofile, path)
  if not ok or type(req) ~= "table" then
    respond(n, { ok = false, error = "bad request: " .. tostring(req) })
    return
  end
  local op = OPS[req.op]
  if not op then
    respond(n, { ok = false, error = "unknown op: " .. tostring(req.op) })
    return
  end
  local ok2, res = pcall(op, req.args or {}, req)
  if ok2 then respond(n, { ok = true, result = res }) else respond(n, { ok = false, error = tostring(res) }) end
end

-- A newer bridge started in this Resolve takes over; this one then exits.
local me = tostring(os.time()) .. "-" .. tostring(math.random(1, 1e9))
fu:SetPrefs("Global.ClaudeBridge.Instance", me)

local last = read_seq() or 0
print("[claude-bridge] " .. VERSION .. " running on " .. resolve:GetVersionString() .. ". Leave this Console open; Claude can now drive Resolve.")

while running do
  local seq = read_seq()
  if seq and seq > last then
    for n = last + 1, seq do
      local path = BOX .. "req_" .. n .. ".lua"
      if bmd.fileexists(path) then handle(n, path) end
    end
    last = seq
  elseif seq and seq < last then
    last = seq
  end
  local owner = fu:GetPrefs("Global.ClaudeBridge.Instance")
  if owner ~= nil and owner ~= me then break end
  bmd.wait(POLL)
end

print("[claude-bridge] stopped.")
