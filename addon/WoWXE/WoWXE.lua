--[[
WoWXE  (protocol v3)

Draws a small strip of colored squares in the top-left corner of the screen.
A companion app reads the strip from the screen and serves it to a second-screen
widget. This addon is DISPLAY ONLY: it never sends input and never decides anything.

Midnight "secret values": in combat the game may hand addons health/power values
that can be shown but not read. This addon never does math on such a value.
  * When a value is NOT secret, it is encoded as plain numbers in the data cells.
  * When a value IS secret, the matching flag bit is set and the number cells stay 0.
  * Optionally (ENABLE_BAR_MIRROR) the raw values are also handed straight to
    StatusBars, so the strip shows a bar fill. That mirrors what the normal
    player frame shows on screen. See README before leaving this on.

Layout (CELL x CELL pixel squares, origin = top-left of the strip)
  row 0  data cells
  row 1  health bar   (BAR_W px wide)
  row 2  power bar
  row 3  xp bar
  row 4  bag slot quality colors (BAG_CELLS)
  row 5  zone name, 3 bytes per cell (ZONE_CELLS)
  row 6  character name (NAME_CELLS)
  row 7  race name (RACE_CELLS)
  row 8  bag slot icon file id, 24 bit per cell
  row 9  bag slot stack count, R = count (capped 255)
  row 10 map description stream: the file ids of the current map's picture tiles and explored-area
         pictures, sent in small chunks that repeat. The companion reads the pictures straight from
         the game files, so nothing has to be drawn on screen. (cells: 0 = map id + chunk number,
         1 = chunk count + length, 2.. = payload bytes)
  Optional (off by default, /wowxe flash on): the CAPTURE AREA below the strip. When the addon sees
  a map or an item icon it has not shown yet, it draws that art there for a moment so the companion
  can take a picture of it. Row 0 cells 16..20 say what is being shown.

Row 0 data cells (RGB bytes)
  c0..c3  sync:  magenta, cyan, magenta, cyan
  c4  R=protocol, G=flags, B=heartbeat counter
  c5  R,G=mapID (16 bit)           B=level
  c6  R,G=pos x*65535              B=bag free slots (capped 255)
  c7  R,G=pos y*65535              B=bag total slots (capped 255)
  c8  xp current (24 bit)
  c9  xp max (24 bit)
  c10 health current (24 bit)
  c11 health max (24 bit)
  c12 power current (24 bit)
  c13 power max (24 bit)
  c14 R=power type, G=class id
  c15 R,G=facing radians/(2*pi)*65535
  c16 R=capture kind (0 none, 1 item icon, 2 map)
  c17 capture id (24 bit): icon file id or map id
  c18 R,G=capture draw width px     B=capture sequence number
  c19 R,G=capture draw height px    B=faction (0 ?, 1 Alliance, 2 Horde, 3 Neutral)
  c20 R=capture flags (bit0: map art API unavailable, bit1: exploration API unavailable)

Flags (bit 0 = 1)
  0 in combat          1 health cur secret   2 health max secret
  3 power cur secret   4 power max secret    5 position valid
  6 bar mirror failed  7 facing valid
]]

local CELL = 3   -- pixels per cell: keep in step with the companion. 3 (not 2) so the middle pixel
                 -- still lands inside the right cell if the game snaps edges by one pixel
local PROTOCOL = 3
local VERSION = "1.3.0"   -- release number shared by addon, companion and widget
local BAR_W = 256   -- how wide the bar fill is; the companion reads values the game hides from it
local BAG_CELLS = 64
local ZONE_CELLS = 12
local NAME_CELLS = 12
local RACE_CELLS = 8
local DATA_CELLS = 24
local STREAM_ROW = 10
local CAP_Y = 12 * CELL      -- optional capture area sits just below the strip
local CAP_W, CAP_H = 804, 536
local ICON_PX = 64
local HOLD_ICON, HOLD_MAP = 5, 20   -- ticks (0.1 s each) a capture stays on screen
local UPDATE_INTERVAL = 0.1
local ENABLE_BAR_MIRROR = true

local issecret = issecretvalue or function() return false end

local STRIP_W = math.max(BAG_CELLS * CELL, BAR_W)
local STRIP_H = 11 * CELL

local QUALITY_RGB = {
	[0] = { 157, 157, 157 }, [1] = { 255, 255, 255 }, [2] = { 30, 255, 0 },
	[3] = { 0, 112, 221 },   [4] = { 163, 53, 238 },  [5] = { 255, 128, 0 },
	[6] = { 230, 204, 128 }, [7] = { 0, 204, 255 },
}

-- ---------------------------------------------------------------- frame
local f = CreateFrame("Frame", "WoWXEFrame", UIParent)
f:SetFrameStrata("TOOLTIP")
f:SetIgnoreParentScale(true)
f:SetSize(STRIP_W, STRIP_H)
f:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)

local bg = f:CreateTexture(nil, "BACKGROUND")
bg:SetAllPoints(f)
bg:SetColorTexture(0, 0, 0, 1)

local function newCell(row, col)
	local t = f:CreateTexture(nil, "ARTWORK")
	t:SetSize(CELL, CELL)
	t:SetPoint("TOPLEFT", f, "TOPLEFT", col * CELL, -row * CELL)
	t:SetColorTexture(0, 0, 0, 1)
	return t
end

local function setCell(t, r, g, b)
	t:SetColorTexture(r / 255, g / 255, b / 255, 1)
end

local dataCells = {}
for c = 0, DATA_CELLS - 1 do dataCells[c] = newCell(0, c) end

local bagCells = {}
for c = 0, BAG_CELLS - 1 do bagCells[c] = newCell(4, c) end

local zoneCells = {}
for c = 0, ZONE_CELLS - 1 do zoneCells[c] = newCell(5, c) end

local nameCells = {}
for c = 0, NAME_CELLS - 1 do nameCells[c] = newCell(6, c) end

local raceCells = {}
for c = 0, RACE_CELLS - 1 do raceCells[c] = newCell(7, c) end

local iconCells, countCells = {}, {}
for c = 0, BAG_CELLS - 1 do
	iconCells[c] = newCell(8, c)
	countCells[c] = newCell(9, c)
end

local streamCells = {}
for c = 0, BAG_CELLS - 1 do streamCells[c] = newCell(STREAM_ROW, c) end

-- capture area: black backdrop plus a pool of textures for map tiles and one icon
local capFrame = CreateFrame("Frame", nil, f)
capFrame:SetSize(CAP_W, CAP_H)
capFrame:SetPoint("TOPLEFT", f, "TOPLEFT", 0, -CAP_Y)
if capFrame.SetClipsChildren then capFrame:SetClipsChildren(true) end
local capBg = capFrame:CreateTexture(nil, "BACKGROUND")
capBg:SetAllPoints(capFrame)
capBg:SetColorTexture(0, 0, 0, 1)
local capIcon = capFrame:CreateTexture(nil, "ARTWORK")
local mapTiles = {}
capFrame:Hide()

local function newBar(row)
	local bar = CreateFrame("StatusBar", nil, f)
	bar:SetSize(BAR_W, CELL)
	bar:SetPoint("TOPLEFT", f, "TOPLEFT", 0, -row * CELL)
	bar:SetStatusBarTexture("Interface\\Buttons\\WHITE8X8")
	bar:SetStatusBarColor(1, 1, 1, 1)
	bar:SetMinMaxValues(0, 1)
	bar:SetValue(0)
	return bar
end

local healthBar, powerBar, xpBar = newBar(1), newBar(2), newBar(3)

-- ---------------------------------------------------------------- encoding
local function byte3(n)
	n = math.floor(n + 0.5)
	if n < 0 then n = 0 elseif n > 16777215 then n = 16777215 end
	return math.floor(n / 65536), math.floor(n / 256) % 256, n % 256
end

local function word(n)
	n = math.floor(n + 0.5)
	if n < 0 then n = 0 elseif n > 65535 then n = 65535 end
	return math.floor(n / 256), n % 256
end

local function clampByte(n)
	n = math.floor(n)
	if n < 0 then return 0 elseif n > 255 then return 255 end
	return n
end

-- Returns the value only if it is a plain number we may do math on.
local function plain(v)
	-- issecret() first: comparing a secret value (even to nil) can raise an error
	if issecret(v) then return nil end
	if v == nil then return nil end
	if type(v) ~= "number" then return nil end
	return v
end

-- Hand a (possibly secret) value straight to a bar. No math, no comparisons.
local function feedBar(bar, cur, max)
	local ok1 = pcall(bar.SetMinMaxValues, bar, 0, max)
	local ok2 = pcall(bar.SetValue, bar, cur)
	return ok1 and ok2
end

-- ---------------------------------------------------------------- slow data
local zoneText = ""
local bagFree, bagTotal = 0, 0

-- Cut a string to maxBytes without splitting a UTF-8 character.
local function utf8cut(str, maxBytes)
	if #str <= maxBytes then return str end
	local cut = maxBytes
	while cut > 0 do
		local b = str:byte(cut + 1)
		if b and b >= 128 and b < 192 then cut = cut - 1 else break end
	end
	return str:sub(1, cut)
end

local function writeText(cells, count, text)
	text = utf8cut(text, count * 3)
	for c = 0, count - 1 do
		local a, b, d = text:byte(c * 3 + 1, c * 3 + 3)
		setCell(cells[c], a or 0, b or 0, d or 0)
	end
end

local function safeString(ok, v)
	if ok and type(v) == "string" and not issecret(v) then return v end
	return ""
end

local faction = 0

local function refreshZone()
	zoneText = safeString(pcall(GetZoneText))
	writeText(zoneCells, ZONE_CELLS, zoneText)
end

local function refreshIdentity()
	local fullName = ""
	if GetUnitName then fullName = safeString(pcall(GetUnitName, "player", true)) end
	if fullName == "" then fullName = safeString(pcall(UnitName, "player")) end
	writeText(nameCells, NAME_CELLS, fullName)
	local okR, race = pcall(UnitRace, "player")
	writeText(raceCells, RACE_CELLS, safeString(okR, race))
	faction = 0
	if UnitFactionGroup then
		local okF, fg = pcall(UnitFactionGroup, "player")
		fg = safeString(okF, fg)
		if fg == "Alliance" then faction = 1
		elseif fg == "Horde" then faction = 2
		elseif fg ~= "" then faction = 3 end
	end
end

-- ---------------------------------------------------------------- captures
local capQueue, capSeen, capQueued = {}, {}, {}
local capCur, capTicks, capSeq = nil, 0, 0
local capFlags = 0
local mapArtMissing, exploreMissing = false, false
local lastExploreRefresh = 0
local capW, capH = 0, 0

local function flashOn() return WoWXEDB and WoWXEDB.flash end

local function queueCapture(kind, id, urgent, force)
	if not force and not flashOn() then return end
	if not id or id <= 0 then return end
	local key = kind .. ":" .. id
	if capSeen[key] or capQueued[key] then return end
	capQueued[key] = true
	local item = { kind = kind, id = id, key = key }
	if urgent then table.insert(capQueue, 1, item) else capQueue[#capQueue + 1] = item end
end

local overlayTiles = {}
local function hideCaptureArt()
	capIcon:Hide()
	for _, t in pairs(mapTiles) do t:Hide() end
	for _, t in pairs(overlayTiles) do t:Hide() end
end

local function updateCapFlags()
	capFlags = (mapArtMissing and 1 or 0) + (exploreMissing and 2 or 0)
end

-- Explored areas: the game stores them as overlay pictures on top of the base map.
local OVERLAY_TILE = 256
local function drawOverlays(mapID, k)
	if not (C_MapExplorationInfo and C_MapExplorationInfo.GetExploredMapTextures) then
		exploreMissing = true
		updateCapFlags()
		return
	end
	local ok, infos = pcall(C_MapExplorationInfo.GetExploredMapTextures, mapID)
	if not ok or type(infos) ~= "table" then return end
	local used = 0
	for _, info in ipairs(infos) do
		local tw, th = plain(info.textureWidth), plain(info.textureHeight)
		local ox, oy = plain(info.offsetX) or 0, plain(info.offsetY) or 0
		local ids = info.fileDataIDs
		if tw and th and tw > 0 and th > 0 and type(ids) == "table" then
			local nw, nh = math.ceil(tw / OVERLAY_TILE), math.ceil(th / OVERLAY_TILE)
			for j = 1, nh do
				local ph = (j < nh) and OVERLAY_TILE or (th - (nh - 1) * OVERLAY_TILE)
				local fh = 2 ^ math.ceil(math.log(ph) / math.log(2))
				for i = 1, nw do
					local pw = (i < nw) and OVERLAY_TILE or (tw - (nw - 1) * OVERLAY_TILE)
					local fw = 2 ^ math.ceil(math.log(pw) / math.log(2))
					local fid = ids[(j - 1) * nw + i]
					if fid and used < 400 then
						used = used + 1
						local t = overlayTiles[used]
						if not t then
							t = capFrame:CreateTexture(nil, "OVERLAY")
							overlayTiles[used] = t
						end
						t:SetSize(pw * k, ph * k)
						t:SetPoint("TOPLEFT", capFrame, "TOPLEFT", (ox + (i - 1) * OVERLAY_TILE) * k, -(oy + (j - 1) * OVERLAY_TILE) * k)
						t:SetTexture(fid)
						t:SetTexCoord(0, pw / fw, 0, ph / fh)
						t:Show()
					end
				end
			end
		end
	end
end

local function drawIcon(id)
	hideCaptureArt()
	capIcon:SetSize(ICON_PX, ICON_PX)
	capIcon:SetPoint("TOPLEFT", capFrame, "TOPLEFT", 0, 0)
	capIcon:SetTexture(id)
	capIcon:SetTexCoord(0.07, 0.93, 0.07, 0.93)
	capIcon:Show()
	return ICON_PX, ICON_PX
end

local function drawMap(mapID)
	hideCaptureArt()
	if not (C_Map and C_Map.GetMapArtLayers and C_Map.GetMapArtLayerTextures) then
		mapArtMissing = true
		updateCapFlags()
		return nil
	end
	local okL, layers = pcall(C_Map.GetMapArtLayers, mapID)
	local layer = okL and type(layers) == "table" and layers[1] or nil
	local okT, texs = pcall(C_Map.GetMapArtLayerTextures, mapID, 1)
	if not layer or not okT or type(texs) ~= "table" or #texs == 0 then return nil end
	local lw, lh = plain(layer.layerWidth), plain(layer.layerHeight)
	local tw, th = plain(layer.tileWidth), plain(layer.tileHeight)
	if not (lw and lh and tw and th) or lw <= 0 or lh <= 0 or tw <= 0 or th <= 0 then return nil end
	local k = math.min(CAP_W / lw, CAP_H / lh)
	local cols = math.ceil(lw / tw)
	for i = 1, math.min(#texs, 48) do
		local t = mapTiles[i]
		if not t then
			t = capFrame:CreateTexture(nil, "ARTWORK")
			mapTiles[i] = t
		end
		local col, row = (i - 1) % cols, math.floor((i - 1) / cols)
		t:SetSize(math.ceil(tw * k) + 1, math.ceil(th * k) + 1)
		t:SetPoint("TOPLEFT", capFrame, "TOPLEFT", math.floor(col * tw * k + 0.5), -math.floor(row * th * k + 0.5))
		t:SetTexture(texs[i])
		t:Show()
	end
	drawOverlays(mapID, k)
	return math.floor(lw * k + 0.5), math.floor(lh * k + 0.5)
end

-- ---------------------------------------------------------------- map description stream
local STREAM_PAYLOAD = (BAG_CELLS - 2) * 3
local descBytes, descMap = nil, 0
local streamSkip = 0
-- The stream carries several messages one after another: the map description (id = mapID),
-- the slot -> item index (id 0xFFFE) and one tooltip per distinct bag item (id 0xFFFF).
local msgs, msgPos, msgIdx = {}, 1, 0
local bagIndexBytes, tipCache, tipOrder = nil, {}, {}
local charBytes, gearBytes, profBytes = nil, nil, nil   -- stats / equipped items / professions messages
local bagPresent, equipPresent = {}, {}

local function u16(n) n = math.floor(n + 0.5); if n < 0 then n = 0 elseif n > 65535 then n = 65535 end; return string.char(math.floor(n / 256), n % 256) end
local function u32(n)
	n = math.floor(n + 0.5); if n < 0 then n = 0 end
	return string.char(math.floor(n / 16777216) % 256, math.floor(n / 65536) % 256, math.floor(n / 256) % 256, n % 256)
end

local function buildDescriptor(mapID)
	if not (C_Map and C_Map.GetMapArtLayers and C_Map.GetMapArtLayerTextures) then return nil end
	local okL, layers = pcall(C_Map.GetMapArtLayers, mapID)
	local layer = okL and type(layers) == "table" and layers[1] or nil
	local okT, texs = pcall(C_Map.GetMapArtLayerTextures, mapID, 1)
	if not layer or not okT or type(texs) ~= "table" or #texs == 0 or #texs > 255 then return nil end
	local lw, lh = plain(layer.layerWidth), plain(layer.layerHeight)
	local tw, th = plain(layer.tileWidth), plain(layer.tileHeight)
	if not (lw and lh and tw and th) then return nil end
	local parts = { u16(mapID), u16(lw), u16(lh), u16(tw), u16(th), string.char(#texs) }
	for i = 1, #texs do
		local id = plain(texs[i])
		if not id then return nil end   -- not a file id on this client
		parts[#parts + 1] = u32(id)
	end
	local ovl = {}
	if C_MapExplorationInfo and C_MapExplorationInfo.GetExploredMapTextures then
		local ok, infos = pcall(C_MapExplorationInfo.GetExploredMapTextures, mapID)
		if ok and type(infos) == "table" then
			for _, info in ipairs(infos) do
				local w, h = plain(info.textureWidth), plain(info.textureHeight)
				local ids = info.fileDataIDs
				if w and h and type(ids) == "table" and #ids < 65535 and #ovl < 255 then
					local one = { u16(w), u16(h), u16(plain(info.offsetX) or 0), u16(plain(info.offsetY) or 0), u16(#ids) }
					for i = 1, #ids do one[#one + 1] = u32(plain(ids[i]) or 0) end
					ovl[#ovl + 1] = table.concat(one)
				end
			end
		end
	end
	parts[#parts + 1] = string.char(#ovl)
	parts[#parts + 1] = table.concat(ovl)
	local body = table.concat(parts)
	local sum = 0
	for i = 1, #body do sum = (sum + body:byte(i)) % 65536 end
	body = body .. u16(sum)
	if #body > STREAM_PAYLOAD * 40 then return nil end
	return body
end

local function withSum(body)
	local sum = 0
	for i = 1, #body do sum = (sum + body:byte(i)) % 65536 end
	return body .. u16(sum)
end

local function rebuildMessages()
	local list = { { id = 0xFFFD, bytes = withSum(VERSION) } }
	if descBytes then list[#list + 1] = { id = descMap, bytes = descBytes } end
	if charBytes then list[#list + 1] = { id = 0xFFFC, bytes = charBytes } end
	if gearBytes then list[#list + 1] = { id = 0xFFFB, bytes = gearBytes } end
	if profBytes then list[#list + 1] = { id = 0xFFFA, bytes = profBytes } end
	if bagIndexBytes then list[#list + 1] = { id = 0xFFFE, bytes = bagIndexBytes } end
	for _, id in ipairs(tipOrder) do
		local b = tipCache[id]
		if b then list[#list + 1] = { id = 0xFFFF, bytes = b } end
	end
	msgs = list
	if msgPos > #msgs then msgPos, msgIdx = 1, 0 end
end

-- tooltips are wanted for every item in the bags and every equipped item
local function rebuildTipOrder()
	local want = {}
	for id in pairs(bagPresent) do want[id] = true end
	for id in pairs(equipPresent) do want[id] = true end
	tipOrder = {}
	for id in pairs(want) do tipOrder[#tipOrder + 1] = id end
	table.sort(tipOrder)
	for id in pairs(tipCache) do if not want[id] then tipCache[id] = nil end end
end

local function refreshDescriptor(mapID)
	descMap = mapID or 0
	descBytes = mapID and buildDescriptor(mapID) or nil
	rebuildMessages()
end

-- ---------------------------------------------------------------- item tooltips
local function cleanText(v)
	if v == nil or issecret(v) or type(v) ~= "string" then return "" end
	v = v:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|T.-|t", ""):gsub("|A.-|a", "")
	v = v:gsub("%c", " ")
	return v
end

local function hexColor(c)
	if type(c) ~= "table" then return "ffffff" end
	local r, g, b = plain(c.r), plain(c.g), plain(c.b)
	if not (r and g and b) then return "ffffff" end
	return string.format("%02x%02x%02x", clampByte(r * 255), clampByte(g * 255), clampByte(b * 255))
end

-- kind "bag": (bag, slot);  kind "inv": ("player", inventory slot)
local function tipLinesFromApi(kind, bag, slot)
	local fn = C_TooltipInfo and (kind == "bag" and C_TooltipInfo.GetBagItem or C_TooltipInfo.GetInventoryItem)
	if not fn then return nil end
	local ok, data = pcall(fn, bag, slot)
	if not ok or type(data) ~= "table" then return nil end
	if TooltipUtil and TooltipUtil.SurfaceArgs then pcall(TooltipUtil.SurfaceArgs, data) end
	if type(data.lines) ~= "table" then return nil end
	local out = {}
	for _, ln in ipairs(data.lines) do
		out[#out + 1] = { hexColor(ln.leftColor), cleanText(ln.leftText), cleanText(ln.rightText) }
	end
	return out
end

local scanTip
local function tipLinesFromTooltip(kind, bag, slot)
	if not (CreateFrame and _G) then return nil end
	local ok, lines = pcall(function()
		scanTip = scanTip or CreateFrame("GameTooltip", "WoWXEScanTip", nil, "GameTooltipTemplate")
		scanTip:SetOwner(WorldFrame or UIParent, "ANCHOR_NONE")
		scanTip:ClearLines()
		if kind == "bag" then scanTip:SetBagItem(bag, slot) else scanTip:SetInventoryItem(bag, slot) end
		local out = {}
		for i = 1, scanTip:NumLines() do
			local L, R = _G["WoWXEScanTipTextLeft" .. i], _G["WoWXEScanTipTextRight" .. i]
			local r, g, b = 1, 1, 1
			if L and L.GetTextColor then r, g, b = L:GetTextColor() end
			out[#out + 1] = { hexColor({ r = r, g = g, b = b }), cleanText(L and L:GetText()), cleanText(R and R:GetText()) }
		end
		return out
	end)
	return ok and lines or nil
end

-- Returns the message body for one item, or nil when the game has not loaded its data yet.
local function buildTip(itemID, kind, bag, slot)
	local lines = tipLinesFromApi(kind, bag, slot) or tipLinesFromTooltip(kind, bag, slot)
	if not lines or #lines == 0 then return nil end
	local first = lines[1][2]
	if first == "" or first == (RETRIEVING_ITEM_INFO or "Retrieving item information") then return nil end
	local parts, size = { u32(itemID) }, 4
	for _, ln in ipairs(lines) do
		local row = ln[1] .. utf8cut(ln[2], 160) .. "\31" .. utf8cut(ln[3], 80) .. "\n"
		if size + #row > 2800 then break end
		parts[#parts + 1] = row
		size = size + #row
	end
	return withSum(table.concat(parts))
end

local function stepStream()
	streamSkip = streamSkip + 1
	if streamSkip < 2 then return end   -- each chunk stays up for 2 ticks (0.2 s)
	streamSkip = 0
	local m = msgs[msgPos]
	if not m then
		setCell(streamCells[0], 0, 0, 0)
		setCell(streamCells[1], 0, 0, 0)
		return
	end
	local total = math.ceil(#m.bytes / STREAM_PAYLOAD)
	if msgIdx >= total then
		msgPos = msgPos % #msgs + 1
		msgIdx = 0
		m = msgs[msgPos]
		total = math.ceil(#m.bytes / STREAM_PAYLOAD)
	end
	local mr, mg = word(m.id)
	setCell(streamCells[0], mr, mg, msgIdx)
	local lr, lg = word(#m.bytes)
	setCell(streamCells[1], total, lr, lg)
	local first = msgIdx * STREAM_PAYLOAD
	for c = 2, BAG_CELLS - 1 do
		local i = first + (c - 2) * 3
		local a, b, d = m.bytes:byte(i + 1, i + 3)
		setCell(streamCells[c], a or 0, b or 0, d or 0)
	end
	msgIdx = msgIdx + 1
end

local function startCapture(item)
	local w, h
	if item.kind == 1 then w, h = drawIcon(item.id) else w, h = drawMap(item.id) end
	if not w then
		capSeen[item.key] = true  -- cannot be drawn on this client; do not retry forever
		capQueued[item.key] = nil
		return
	end
	capSeq = (capSeq + 1) % 256
	capCur, capW, capH = item, w, h
	capTicks = item.kind == 1 and HOLD_ICON or HOLD_MAP
	capFrame:Show()
end

local function endCapture()
	capFrame:Hide()
	hideCaptureArt()
	capSeen[capCur.key] = true
	capQueued[capCur.key] = nil
	capCur, capW, capH = nil, 0, 0
end

local function stepCapture(inCombat)
	if capCur then
		capTicks = capTicks - 1
		if capTicks <= 0 then endCapture() end
	elseif not inCombat and #capQueue > 0 then
		startCapture(table.remove(capQueue, 1))
	end
end

local function refreshBags()
	if not C_Container then return end
	local idx, free, total = 0, 0, 0
	local index, present = {}, {}
	for bag = 0, 4 do
		local n = C_Container.GetContainerNumSlots(bag) or 0
		total = total + n
		free = free + (C_Container.GetContainerNumFreeSlots(bag) or 0)
		for slot = 1, n do
			if idx < BAG_CELLS then
				local info = C_Container.GetContainerItemInfo(bag, slot)
				if info then
					local q = QUALITY_RGB[plain(info.quality) or 1] or QUALITY_RGB[1]
					setCell(bagCells[idx], q[1], q[2], q[3])
					local icon = plain(info.iconFileID) or 0
					setCell(iconCells[idx], byte3(icon))
					setCell(countCells[idx], clampByte(plain(info.stackCount) or 1), 0, 0)
					if icon > 0 then queueCapture(1, icon, false) end
					local itemID = plain(info.itemID) or 0
					index[idx + 1] = { itemID, icon }
					if itemID > 0 and not present[itemID] then
						present[itemID] = true
						if not tipCache[itemID] then
							local ok, body = pcall(buildTip, itemID, "bag", bag, slot)
							if ok and body then tipCache[itemID] = body end
						end
					end
				else
					setCell(bagCells[idx], 0, 0, 0)
					setCell(iconCells[idx], 0, 0, 0)
					setCell(countCells[idx], 0, 0, 0)
				end
			end
			idx = idx + 1
		end
	end
	for c = idx, BAG_CELLS - 1 do
		setCell(bagCells[c], 0, 0, 0)
		setCell(iconCells[c], 0, 0, 0)
		setCell(countCells[c], 0, 0, 0)
	end
	bagFree, bagTotal = free, total
	-- stream: slot -> (item id, icon id) and a tooltip for every distinct item currently in the bags
	local n = math.min(idx, BAG_CELLS)
	local parts = { u16(n) }
	for i = 1, n do
		local e = index[i]
		parts[#parts + 1] = u32(e and e[1] or 0)
		parts[#parts + 1] = u32(e and e[2] or 0)
	end
	bagIndexBytes = withSum(table.concat(parts))
	bagPresent = present
	rebuildTipOrder()
	rebuildMessages()
end

-- ---------------------------------------------------------------- character sheet: stats, gear, professions
local function num(v)
	v = plain(v)
	return type(v) == "number" and v or nil
end

local function call(fn, ...)
	if type(fn) ~= "function" then return nil end
	local ok, a, b, c, d, e = pcall(fn, ...)
	if ok then return a, b, c, d, e end
	return nil
end

local function atMaxLevel()
	local lvl = num(UnitLevel and UnitLevel("player"))
	if IsPlayerAtEffectiveMaxLevel then
		local v = call(IsPlayerAtEffectiveMaxLevel)
		if v ~= nil then return v and true or false end
	end
	local mx = num(call(GetMaxPlayerLevel)) or num(MAX_PLAYER_LEVEL) or num(call(GetMaxLevelForPlayerExpansion))
	return (lvl and mx and lvl >= mx) and true or false
end

local function buildCharText()
	local rows = { "maxlevel=" .. (atMaxLevel() and "1" or "0") }
	local buffed = {}
	local function add(k, v, fmt)
		if v then rows[#rows + 1] = k .. "=" .. string.format(fmt or "%d", fmt and v or math.floor(v + 0.5)) end
	end
	local function addPos(k, v, fmt)   -- only when the game would show a non-zero value
		if v and v > 0 then add(k, v, fmt) end
	end
	local function mark(k, pos, neg)
		pos, neg = num(pos) or 0, num(neg) or 0
		if neg < 0 then buffed[#buffed + 1] = k .. ":-" elseif pos > 0 then buffed[#buffed + 1] = k .. ":+" end
	end
	-- general
	add("ilvl", num(select(2, pcall(GetAverageItemLevel or function() end))), "%.1f")
	local _, run = call(GetUnitSpeed, "player")
	if num(run) and num(run) > 0 then add("speed", num(run) / (num(BASE_MOVEMENT_SPEED) or 7) * 100) end
	-- primary attributes
	for i, k in ipairs({ "str", "agi", "sta", "int", "spi" }) do
		local _, eff, pos, neg = call(UnitStat, "player", i)
		add(k, num(eff))
		mark(k, pos, neg)
	end
	-- weapons
	local lo, hi = call(UnitDamage, "player")
	if num(lo) and num(hi) then
		add("mh_min", math.max(math.floor(num(lo)), 1))
		add("mh_max", math.max(math.ceil(num(hi)), 1))
	end
	local base, pos, neg = call(UnitAttackPower, "player")
	if num(base) then
		add("ap", num(base) + (num(pos) or 0) + (num(neg) or 0))
		mark("ap", pos, neg)
	end
	-- modifiers
	addPos("hit", num(call(GetHitModifier)), "%.1f")
	add("crit", num(call(GetCritChance)), "%.1f")
	local sd = nil
	for school = 2, 7 do
		local v = num(call(GetSpellBonusDamage, school))
		if v and (not sd or v > sd) then sd = v end
	end
	add("spdmg", sd)
	add("spheal", num(call(GetSpellBonusHealing)))
	addPos("haste", num(call(GetHaste)), "%.1f")
	addPos("mastery", num(call(GetMasteryEffect)), "%.1f")
	if CR_VERSATILITY_DAMAGE_DONE then
		addPos("vers", num(call(GetCombatRatingBonus, CR_VERSATILITY_DAMAGE_DONE)), "%.1f")
	end
	-- defense
	local dbase, dmod = call(UnitDefense, "player")
	if num(dbase) then
		add("def", num(dbase) + (num(dmod) or 0))
		local lvl = num(UnitLevel and UnitLevel("player"))
		if lvl then add("defmax", lvl * 5) end
	end
	add("dodge", num(call(GetDodgeChance)), "%.1f")
	addPos("parry", num(call(GetParryChance)), "%.1f")
	addPos("block", num(call(GetBlockChance)), "%.1f")
	local _, armor, _, apos, aneg = call(UnitArmor, "player")
	add("armor", num(armor))
	mark("armor", apos, aneg)
	if #buffed > 0 then rows[#rows + 1] = "buffs=" .. table.concat(buffed, ",") end
	return table.concat(rows, "\n")
end

local function buildGear()
	local parts, n = {}, 0
	local equip = {}
	for slot = 1, 19 do
		local id = num(call(GetInventoryItemID, "player", slot))
		if id and id > 0 then
			local icon = call(GetInventoryItemTexture, "player", slot)
			if type(icon) ~= "number" then
				icon = num(C_Item and call(C_Item.GetItemIconByID, id)) or num(call(GetItemIcon, id)) or 0
			end
			local q = num(call(GetInventoryItemQuality, "player", slot)) or 1
			local link = call(GetInventoryItemLink, "player", slot)
			local ilvl = 0
			if link then
				ilvl = num(C_Item and call(C_Item.GetDetailedItemLevelInfo, link)) or num(call(GetDetailedItemLevelInfo, link)) or 0
			end
			n = n + 1
			parts[#parts + 1] = string.char(slot) .. u32(id) .. u32(math.floor(icon)) .. string.char(clampByte(q)) .. u16(ilvl)
			equip[id] = true
			if not tipCache[id] then
				local ok, body = pcall(buildTip, id, "inv", "player", slot)
				if ok and body then tipCache[id] = body end
			end
		end
	end
	equipPresent = equip
	return string.char(n) .. table.concat(parts)
end

local PRIMARY = { Alchemy = 1, Blacksmithing = 1, Enchanting = 1, Engineering = 1, Herbalism = 1, Inscription = 1,
	Jewelcrafting = 1, Leatherworking = 1, Mining = 1, Skinning = 1, Tailoring = 1 }
local SECONDARY = { Cooking = 1, ["First Aid"] = 1, Fishing = 1, Archaeology = 1 }

local function buildProfessions()
	local rows = {}
	local function add(group, name, cur, max, icon)
		name = cleanText(name)
		if name ~= "" and num(cur) and num(max) then
			rows[#rows + 1] = group .. "|" .. name .. "|" .. math.floor(num(cur)) .. "|" .. math.floor(num(max)) .. "|" .. math.floor(num(icon) or 0)
		end
	end
	if GetProfessions and GetProfessionInfo then
		local ok, a, b, c, d, e = pcall(GetProfessions)
		if ok then
			local idxs = { a, b, c, d, e }   -- may have holes (no archaeology, only one primary...)
			for i = 1, 5 do
				local idx = idxs[i]
				if idx then
					local ok2, name, icon, cur, max = pcall(GetProfessionInfo, idx)
					if ok2 then add(i <= 2 and "P" or "S", name, cur, max, icon) end
				end
			end
		end
	elseif GetNumSkillLines and GetSkillLineInfo then
		for i = 1, (call(GetNumSkillLines) or 0) do
			local ok, name, header, _, rank, _, _, maxRank = pcall(GetSkillLineInfo, i)
			if ok and not header and name then
				if PRIMARY[name] then add("P", name, rank, maxRank, 0)
				elseif SECONDARY[name] then add("S", name, rank, maxRank, 0) end
			end
		end
	end
	return table.concat(rows, "\n")
end

local function refreshChar()
	local changed = false
	local ok1, c = pcall(buildCharText)
	if ok1 and c then
		local b = withSum(c)
		if b ~= charBytes then charBytes, changed = b, true end
	end
	local ok2, g = pcall(buildGear)
	if ok2 and g then
		local b = withSum(g)
		if b ~= gearBytes then gearBytes, changed = b, true end
	end
	local ok3, p = pcall(buildProfessions)
	if ok3 and p then
		local b = withSum(p)
		if b ~= profBytes then profBytes, changed = b, true end
	end
	rebuildTipOrder()
	if changed then rebuildMessages() end
	return changed
end

-- ---------------------------------------------------------------- main tick
local beat = 0
local lastMapID = nil

local function setNumberCell(cell, v)
	local n = plain(v)
	if n then setCell(cell, byte3(n)) else setCell(cell, 0, 0, 0) end
	return n ~= nil
end

local function tick()
	beat = (beat + 1) % 256
	local flags = 0

	local inCombat = UnitAffectingCombat("player")
	if not issecret(inCombat) and inCombat then flags = flags + 1 end

	-- health / power
	local hp, hpMax = UnitHealth("player"), UnitHealthMax("player")
	local ptype = UnitPowerType("player")
	local pw, pwMax = UnitPower("player", ptype), UnitPowerMax("player", ptype)

	if not setNumberCell(dataCells[10], hp) then flags = flags + 2 end
	if not setNumberCell(dataCells[11], hpMax) then flags = flags + 4 end
	if not setNumberCell(dataCells[12], pw) then flags = flags + 8 end
	if not setNumberCell(dataCells[13], pwMax) then flags = flags + 16 end

	if ENABLE_BAR_MIRROR then
		local okH = feedBar(healthBar, hp, hpMax)
		local okP = feedBar(powerBar, pw, pwMax)
		if not (okH and okP) then flags = flags + 64 end
	end

	-- xp
	local xp, xpMax = plain(UnitXP("player")), plain(UnitXPMax("player"))
	setNumberCell(dataCells[8], xp)
	setNumberCell(dataCells[9], xpMax)
	if xp and xpMax and xpMax > 0 then
		xpBar:SetMinMaxValues(0, xpMax)
		xpBar:SetValue(xp)
	else
		xpBar:SetMinMaxValues(0, 1)
		xpBar:SetValue(0)
	end

	-- position / map
	local mapID, px, py = nil, nil, nil
	-- C_Map may be missing or different on some clients (e.g. WoW Forever): guard it
	if C_Map and C_Map.GetBestMapForUnit then
		local okMap, m = pcall(C_Map.GetBestMapForUnit, "player")
		if okMap then mapID = plain(m) end
	end
	if mapID and C_Map.GetPlayerMapPosition then
		local okPos, pos = pcall(C_Map.GetPlayerMapPosition, mapID, "player")
		if okPos and pos then
			local okXY, x, y = pcall(pos.GetXY, pos)
			if okXY then px, py = plain(x), plain(y) end
		end
	end
	if mapID and mapID ~= lastMapID then
		lastMapID = mapID
		refreshDescriptor(mapID)
		queueCapture(2, mapID, true)
	end
	local level = plain(UnitLevel("player")) or 0
	local mr, mg = word(mapID or 0)
	setCell(dataCells[5], mr, mg, clampByte(level))

	if px and py then
		flags = flags + 32
		local xr, xg = word(px * 65535)
		local yr, yg = word(py * 65535)
		setCell(dataCells[6], xr, xg, clampByte(bagFree))
		setCell(dataCells[7], yr, yg, clampByte(bagTotal))
	else
		setCell(dataCells[6], 0, 0, clampByte(bagFree))
		setCell(dataCells[7], 0, 0, clampByte(bagTotal))
	end

	-- facing
	local okF, facing = pcall(GetPlayerFacing)
	facing = okF and plain(facing) or nil
	if facing then
		flags = flags + 128
		local fr, fg = word(facing / (2 * math.pi) * 65535)
		setCell(dataCells[15], fr, fg, 0)
	else
		setCell(dataCells[15], 0, 0, 0)
	end

	-- class / power type
	local classID = 0
	local okC, _, _, cid = pcall(UnitClass, "player")
	if okC then classID = plain(cid) or 0 end
	setCell(dataCells[14], clampByte(plain(ptype) or 0), clampByte(classID), 0)

	-- captures (paused in combat so nothing flashes during a fight)
	stepStream()
	stepCapture(flags % 2 == 1)
	if capCur then
		setCell(dataCells[16], capCur.kind, 0, 0)
		setCell(dataCells[17], byte3(capCur.id))
		local wr, wg = word(capW)
		local hr, hg = word(capH)
		setCell(dataCells[18], wr, wg, capSeq)
		setCell(dataCells[19], hr, hg, faction)
	else
		setCell(dataCells[16], 0, 0, 0)
		setCell(dataCells[17], 0, 0, 0)
		setCell(dataCells[18], 0, 0, capSeq)
		setCell(dataCells[19], 0, 0, faction)
	end
	setCell(dataCells[20], capFlags, 0, 0)

	-- header
	setCell(dataCells[4], PROTOCOL, flags, beat)
end

-- ---------------------------------------------------------------- setup
local warnedScale = false
local function applyScale()
	local ph
	if GetPhysicalScreenSize then
		local _, h = GetPhysicalScreenSize()
		ph = h
	end
	if ph and ph > 0 then
		f:SetScale(768 / ph)
	elseif not warnedScale then
		warnedScale = true
		print("WoWXE: GetPhysicalScreenSize is unavailable on this client, so the strip cannot be sized to exact pixels. The companion may not find it.")
	end
end

-- sync pattern never changes
setCell(dataCells[0], 255, 0, 255)
setCell(dataCells[1], 0, 255, 255)
setCell(dataCells[2], 255, 0, 255)
setCell(dataCells[3], 0, 255, 255)

local acc, charAcc = 0, 0
f:SetScript("OnUpdate", function(_, elapsed)
	charAcc = charAcc + elapsed
	if charAcc >= 3 then
		charAcc = 0
		refreshChar()
	end
	acc = acc + elapsed
	if acc >= UPDATE_INTERVAL then
		acc = 0
		tick()
	end
end)

f:RegisterEvent("PLAYER_ENTERING_WORLD")
f:RegisterEvent("PLAYER_LEVEL_UP")
f:RegisterEvent("UI_SCALE_CHANGED")
f:RegisterEvent("DISPLAY_SIZE_CHANGED")
f:RegisterEvent("BAG_UPDATE_DELAYED")
pcall(f.RegisterEvent, f, "MAP_EXPLORATION_UPDATED")  -- not present on every client
f:RegisterEvent("ZONE_CHANGED")
f:RegisterEvent("GET_ITEM_INFO_RECEIVED")
for _, ev in ipairs({ "PLAYER_EQUIPMENT_CHANGED", "SKILL_LINES_CHANGED", "CHAT_MSG_SKILL", "UNIT_INVENTORY_CHANGED",
		"PLAYER_AVG_ITEM_LEVEL_UPDATE", "PLAYER_XP_UPDATE" }) do
	pcall(f.RegisterEvent, f, ev)   -- not every event exists on every client
end
f:RegisterEvent("ZONE_CHANGED_INDOORS")
f:RegisterEvent("ZONE_CHANGED_NEW_AREA")
f:SetScript("OnEvent", function(_, event)
	if event == "UI_SCALE_CHANGED" or event == "DISPLAY_SIZE_CHANGED" then
		applyScale()
	elseif event == "PLAYER_EQUIPMENT_CHANGED" or event == "SKILL_LINES_CHANGED" or event == "CHAT_MSG_SKILL"
			or event == "UNIT_INVENTORY_CHANGED" or event == "PLAYER_AVG_ITEM_LEVEL_UPDATE" then
		refreshChar()
	elseif event == "BAG_UPDATE_DELAYED" or event == "GET_ITEM_INFO_RECEIVED" then
		refreshBags()
		if event == "GET_ITEM_INFO_RECEIVED" then refreshChar() end
	elseif event == "MAP_EXPLORATION_UPDATED" then
		-- new area discovered: describe the map again; with flash on also re-draw it (at most once a minute)
		if lastMapID then
			refreshDescriptor(lastMapID)
			if (GetTime and GetTime() or 0) - lastExploreRefresh >= 60 then
				lastExploreRefresh = GetTime and GetTime() or 0
				capSeen["2:" .. lastMapID] = nil
				queueCapture(2, lastMapID, true)
			end
		end
	elseif event == "PLAYER_ENTERING_WORLD" or event == "PLAYER_LEVEL_UP" then
		applyScale()
		refreshZone()
		refreshIdentity()
		refreshBags()
		refreshChar()
	else
		refreshZone()
	end
end)

applyScale()

-- ---------------------------------------------------------------- slash command
SLASH_WOWXE1 = "/wowxe"
SlashCmdList["WOWXE"] = function(msg)
	msg = (msg or ""):lower()
	if msg == "hide" then
		f:Hide()
		print("WoWXE: strip hidden (the companion cannot read it while hidden).")
	elseif msg == "show" then
		f:Show()
		print("WoWXE: strip shown.")
	elseif msg == "ver" or msg == "version" then
		print("WoWXE addon v" .. VERSION .. " (protocol " .. PROTOCOL .. ")")
	elseif msg == "who" then
		print("WoWXE name check:")
		print("  UnitName:", safeString(pcall(UnitName, "player")))
		if UnitFullName then print("  UnitFullName:", safeString(pcall(UnitFullName, "player")), select(2, pcall(UnitFullName, "player"))) end
		if UnitPVPName then print("  UnitPVPName:", safeString(pcall(UnitPVPName, "player"))) end
		if GetUnitName then print("  GetUnitName(true):", safeString(pcall(GetUnitName, "player", true))) end
	elseif msg == "mapinfo" then
		print("WoWXE map check (stand in a zone you have explored):")
		local okM, mid = pcall(C_Map and C_Map.GetBestMapForUnit or function() end, "player")
		print("  mapID:", tostring(okM and mid or nil))
		print("  C_Map.GetMapArtLayers:", tostring(C_Map and C_Map.GetMapArtLayers ~= nil))
		print("  C_MapExplorationInfo:", tostring(C_MapExplorationInfo ~= nil))
		print("  legacy GetNumMapOverlays:", tostring(GetNumMapOverlays ~= nil))
		print("  capture flags (1 = no map art API, 2 = no exploration API):", capFlags)
		if okM and mid and C_Map and C_Map.GetMapArtLayers then
			local okL, layers = pcall(C_Map.GetMapArtLayers, mid)
			local L = okL and type(layers) == "table" and layers[1]
			if L then print("  layer:", tostring(L.layerWidth), "x", tostring(L.layerHeight), "tile", tostring(L.tileWidth), "x", tostring(L.tileHeight)) end
			local okT, t = pcall(C_Map.GetMapArtLayerTextures, mid, 1)
			print("  base tiles:", tostring(okT and type(t) == "table" and #t or nil))
			print("  base tile id type:", type(okT and type(t) == "table" and t[1] or nil), tostring(okT and type(t) == "table" and t[1] or nil))
			print("  map description bytes:", tostring(descBytes and #descBytes or "none (not sent)"))
		end
		if okM and mid and C_MapExplorationInfo and C_MapExplorationInfo.GetExploredMapTextures then
			local okE, infos = pcall(C_MapExplorationInfo.GetExploredMapTextures, mid)
			print("  explored overlays:", tostring(okE and type(infos) == "table" and #infos or ("error: " .. tostring(infos))))
			if okE and type(infos) == "table" and infos[1] then
				local i = infos[1]
				print("  first:", tostring(i.textureWidth), "x", tostring(i.textureHeight), "offset", tostring(i.offsetX), tostring(i.offsetY),
					"files", tostring(type(i.fileDataIDs) == "table" and #i.fileDataIDs or nil))
			end
		end
	elseif msg == "flash on" or msg == "flash off" then
		WoWXEDB = WoWXEDB or {}
		WoWXEDB.flash = (msg == "flash on")
		if WoWXEDB.flash then
			capSeen, capQueued, capQueue = {}, {}, {}
			lastMapID = nil
			refreshBags()
			print("WoWXE: flash capture ON (map and icons are drawn top-left for a moment, then saved).")
		else
			print("WoWXE: flash capture OFF (the companion reads the art from the game files).")
		end
	elseif msg == "map" then
		if lastMapID then
			capSeen["2:" .. lastMapID] = nil
			capQueued["2:" .. lastMapID] = nil
			queueCapture(2, lastMapID, true, true)
			print("WoWXE: capturing this zone's map once (the top-left corner shows it for about 2 seconds).")
		else
			print("WoWXE: no map for this location.")
		end
	elseif msg == "recapture" then
		capSeen, capQueued, capQueue = {}, {}, {}
		lastMapID = nil
		refreshBags()
		print("WoWXE: will re-save the current map and bag icons (brief flashes top-left).")
	elseif msg == "test" then
		local ptype = UnitPowerType("player")
		print("WoWXE secret check (run it in and out of combat):")
		print("  health secret:", issecret(UnitHealth("player")))
		print("  health max secret:", issecret(UnitHealthMax("player")))
		print("  power secret:", issecret(UnitPower("player", ptype)))
		print("  power max secret:", issecret(UnitPowerMax("player", ptype)))
		print("  in combat:", UnitAffectingCombat("player") and true or false)
	else
		print("WoWXE commands: /wowxe ver | show | hide | test | who | mapinfo | map | recapture | flash on|off")
	end
end
