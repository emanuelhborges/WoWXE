-- Minimal stand-in for the WoW API, just enough to run WoWXE.lua offline.
-- Frames/textures record what the addon asked for so Python can render the strip.

REGIONS = {}
PRINTED = {}

-- "Secret" values: may be passed around and handed to widgets, but any math or
-- ordering comparison raises an error, like the real game on tainted code.
local SECRET_MT = {}
local function secretError() error("attempt to use a secret value in math/comparison", 2) end
for _, k in ipairs({ "__add", "__sub", "__mul", "__div", "__mod", "__unm", "__lt", "__le", "__len" }) do
	SECRET_MT[k] = secretError
end
local realType = type
function Secret(v) return setmetatable({ __secret = true, val = v }, SECRET_MT) end
function issecretvalue(v) return realType(v) == "table" and rawget(v, "__secret") == true end
-- In the real game type() on a secret reports the underlying type, so mimic that.
function type(v)
	if realType(v) == "table" and rawget(v, "__secret") == true then return realType(rawget(v, "val")) end
	return realType(v)
end

STUB = { failSecretBarRange = false }

WORLD = {
	health = 1, healthMax = 1, power = 1, powerMax = 1, ptype = 0,
	xp = 0, xpMax = 1, level = 1, combat = false,
	mapID = nil, pos = nil, facing = nil, zone = "", classID = 1,
	name = "Arthas", race = "Human", faction = "Alliance",
	-- map art: nil = API missing; else { w, h, tw, th, tiles = { fileIDs } }
	art = {},
	bags = {},
}

-- ---- frames --------------------------------------------------------------
local Methods = {}
function Methods:SetFrameStrata() end
function Methods:SetIgnoreParentScale(v) self.ignoreScale = v end
function Methods:SetSize(w, h) self.w, self.h = w, h end
function Methods:SetPoint(point, rel, relPoint, x, y) self.x, self.y = x or 0, y or 0 end
function Methods:SetAllPoints() self.allPoints = true end
function Methods:SetScale(s) self.scale = s end
function Methods:SetColorTexture(r, g, b, a) self.color = { r, g, b, a or 1 } end
function Methods:SetScript(name, fn) self.scripts[name] = fn end
function Methods:RegisterEvent(e) self.events[e] = true end
function Methods:Hide() self.shown = false end
function Methods:Show() self.shown = true end
function Methods:SetTexture(f) self.file = f end
function Methods:SetTexCoord() end
function Methods:SetClipsChildren() end
function Methods:SetStatusBarTexture() end
function Methods:SetStatusBarColor() end
function Methods:SetMinMaxValues(mn, mx)
	if STUB.failSecretBarRange and issecretvalue(mx) then error("bar range rejected secret", 2) end
	self.min, self.max = mn, mx
end
function Methods:SetValue(v) self.value = v end
function Methods:CreateTexture()
	local t = { kind = "tex", w = 0, h = 0, x = 0, y = 0, color = { 0, 0, 0, 1 }, shown = true }
	t.parent = self
	setmetatable(t, { __index = Methods })
	table.insert(REGIONS, t)
	return t
end

local function newObject(kind)
	local o = { kind = kind, w = 0, h = 0, x = 0, y = 0, scale = 1, shown = true, scripts = {}, events = {} }
	return setmetatable(o, { __index = Methods })
end

UIParent = newObject("frame")
function CreateFrame(kind, name, parent)
	local o = newObject(kind == "StatusBar" and "bar" or "frame")
	o.parent = parent
	table.insert(REGIONS, o)
	if name then _G[name] = o end
	return o
end

function GetPhysicalScreenSize() return 2560, 1440 end

-- ---- game API ------------------------------------------------------------
function UnitHealth() return WORLD.health end
function UnitHealthMax() return WORLD.healthMax end
function UnitPowerType() return WORLD.ptype, "MANA" end
function UnitPower() return WORLD.power end
function UnitPowerMax() return WORLD.powerMax end
function UnitXP() return WORLD.xp end
function UnitXPMax() return WORLD.xpMax end
function UnitLevel() return WORLD.level end
function UnitAffectingCombat() return WORLD.combat end
function UnitClass() return "Class", "CLASS", WORLD.classID end
function GetPlayerFacing() return WORLD.facing end
function GetZoneText() return WORLD.zone end
function UnitName() return WORLD.name end
function UnitRace() return WORLD.race, "Human", 1 end
function UnitFactionGroup() return WORLD.faction end

C_Map = {
	GetMapArtLayers = function(id)
		local a = WORLD.art and WORLD.art[id]
		if not a then return {} end
		return { { layerWidth = a.w, layerHeight = a.h, tileWidth = a.tw, tileHeight = a.th } }
	end,
	GetMapArtLayerTextures = function(id)
		local a = WORLD.art and WORLD.art[id]
		return a and a.tiles or {}
	end,
	GetBestMapForUnit = function() return WORLD.mapID end,
	GetPlayerMapPosition = function()
		local p = WORLD.pos
		if not p then return nil end
		return setmetatable({}, { __index = { GetXY = function() return p.x, p.y end } })
	end,
}

C_MapExplorationInfo = {
	GetExploredMapTextures = function(id) return (WORLD.explored or {})[id] or {} end,
}
function GetTime() return 1000 end
function GetUnitName() return WORLD.fullName or WORLD.name end

local function bag(i) return WORLD.bags[i] or { size = 0, items = {} } end
C_Container = {
	GetContainerNumSlots = function(i) return bag(i).size end,
	GetContainerNumFreeSlots = function(i)
		local n = 0
		for _ in pairs(bag(i).items) do n = n + 1 end
		return bag(i).size - n, 0
	end,
	GetContainerItemInfo = function(i, slot)
		local q = bag(i).items[slot]
		if q == nil then return nil end
		if type(q) == "table" then return q end
		return { quality = q }
	end,
}

C_TooltipInfo = {
	GetBagItem = function(i, slot)
		local it = bag(i).items[slot]
		local tip = type(it) == "table" and it.tip or nil
		if not tip then return nil end
		local lines = {}
		for _, l in ipairs(tip) do
			lines[#lines + 1] = { leftText = l[1], leftColor = { r = l[2] or 1, g = l[3] or 1, b = l[4] or 1 }, rightText = l.right }
		end
		return { lines = lines }
	end,
}

local function tipData(tip)
	if not tip then return nil end
	local lines = {}
	for _, l in ipairs(tip) do
		lines[#lines + 1] = { leftText = l[1], leftColor = { r = l[2] or 1, g = l[3] or 1, b = l[4] or 1 }, rightText = l.right }
	end
	return { lines = lines }
end
C_TooltipInfo.GetInventoryItem = function(unit, slot)
	local g = (WORLD.gear or {})[slot]
	return g and tipData(g.tip) or nil
end

-- character sheet
function UnitStat(_, i) local v = (WORLD.stats or {})[i]; return v, v, (i == 4 and 5 or 0), 0 end
function UnitArmor() return WORLD.armor, WORLD.armor end
function UnitAttackPower() return WORLD.ap, 0, 0 end
function GetCritChance() return WORLD.crit end
function UnitDamage() return WORLD.mh and WORLD.mh[1], WORLD.mh and WORLD.mh[2] end
function GetHitModifier() return WORLD.hit end
function GetSpellBonusDamage(school) return school == 4 and WORLD.spdmg or 0 end
function GetSpellBonusHealing() return WORLD.spheal end
function UnitDefense() return WORLD.def, 0 end
function GetDodgeChance() return WORLD.dodge end
function GetParryChance() return 0 end
function GetUnitSpeed() return 0, 7.14, 4.5, 4.72 end
function GetAverageItemLevel() return WORLD.ilvl, WORLD.ilvl end
function IsPlayerAtEffectiveMaxLevel() return WORLD.maxLevel and true or false end
function GetInventoryItemID(_, slot) local g = (WORLD.gear or {})[slot]; return g and g.id end
function GetInventoryItemTexture(_, slot) local g = (WORLD.gear or {})[slot]; return g and g.icon end
function GetInventoryItemQuality(_, slot) local g = (WORLD.gear or {})[slot]; return g and g.q end
function GetInventoryItemLink(_, slot) local g = (WORLD.gear or {})[slot]; return g and ("item:" .. g.id) end
function GetDetailedItemLevelInfo(link)
	for _, g in pairs(WORLD.gear or {}) do if "item:" .. g.id == link then return g.ilvl end end
end
function GetProfessions()
	local p = WORLD.profs or {}
	return p[1] and 1, p[2] and 2, p[3] and 3, p[4] and 4, p[5] and 5
end
function GetProfessionInfo(i) local p = WORLD.profs[i]; return p[1], p[4], p[2], p[3] end

SlashCmdList = {}
function print(...)
	local parts = {}
	for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
	table.insert(PRINTED, table.concat(parts, " "))
end
