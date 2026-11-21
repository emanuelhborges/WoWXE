icueEvents = {
	onICUEInitialized: init,
	onDataUpdated: update,
};

// ---------- Settings ----------
// The WoWXE companion serves this. Needs the matching "url"
// permission in manifest.json.
const FEED_URL = "http://127.0.0.1:8765/state.json";
const POLL_MS = 250;
const WIDGET_VERSION = "1.4.0"; // release number shared by addon, companion and widget
const BASE_URL = FEED_URL.replace(/\/state\.json$/, "");
const CLASS_COLORS = {
	1: "#C69B6D", 2: "#F48CBA", 3: "#AAD372", 4: "#FFF468", 5: "#FFFFFF", 6: "#C41E3A", 7: "#0070DD",
	8: "#3FC7EB", 9: "#8788EE", 10: "#00FF98", 11: "#FF7C0A", 12: "#A330C9", 13: "#33937F",
};
const FACTION_ICONS = { Alliance: "resources/alliance.png", Horde: "resources/horde.png" };
const FACTION_COLORS = { Alliance: "#4a9bff", Horde: "#ff5a4a", Neutral: "#d8dbe2" };
// Open index.html in a normal browser with #mock at the end to preview with fake data.
const USE_MOCK = typeof location !== "undefined" && /mock/.test(location.hash);

const POWER_COLORS = {
	0: "#3498db", 1: "#e74c3c", 2: "#e67e22", 3: "#f1c40f", 5: "#c0392b", 6: "#00c8ff",
	7: "#9b59b6", 8: "#5b8def", 9: "#f5e6a8", 11: "#2980b9", 12: "#2ecc71", 13: "#8e44ad",
	16: "#9b7bff", 17: "#27ae60", 18: "#c0392b", 19: "#1abc9c",
};

const $ = (id) => document.getElementById(id);
let accent = "#e74c3c";
let lastGood = null;

// ---------- Data ----------
async function fetchFeed() {
	const ctl = new AbortController();
	const timer = setTimeout(() => ctl.abort(), 1500);
	try {
		const r = await fetch(FEED_URL, { cache: "no-store", signal: ctl.signal });
		return await r.json();
	} finally {
		clearTimeout(timer);
	}
}

const fmt = (n) => Number(n).toLocaleString("en-US");

// text = centre label ("cur / max"), pctText = right label. In combat the game may hide the
// exact numbers; then the centre shows what is known and the bar itself stays accurate.
function resourceView(r) {
	if (!r) return { pct: 0, text: "—", pctText: "", known: false };
	const pct = r.pct === null || r.pct === undefined ? null : r.pct;
	const has = (v) => v !== null && v !== undefined;
	let text = "—";
	if (has(r.cur) && has(r.max)) text = fmt(r.cur) + " / " + fmt(r.max);
	else if (has(r.cur_est) && has(r.max)) text = fmt(r.cur_est) + " / " + fmt(r.max);
	else if (has(r.cur)) text = fmt(r.cur);
	else if (has(r.max)) text = "? / " + fmt(r.max);
	return { pct: pct === null ? 0 : pct, text, pctText: pct === null ? "" : Math.round(pct) + "%", known: pct !== null };
}

// ---------- Pictures served by the companion (map and item art) ----------
// Loaded with fetch (same permission as the data feed) and kept in memory as blob URLs.
const pics = new Map(); // url -> { state, blobUrl }
function pic(url) {
	let p = pics.get(url);
	if (p) return p.state === "ok" ? p.blobUrl : null;
	p = { state: "loading", blobUrl: null };
	pics.set(url, p);
	fetch(url).then((r) => (r.ok ? r.blob() : Promise.reject(r.status)))
		.then((b) => { p.blobUrl = URL.createObjectURL(b); p.state = "ok"; })
		.catch(() => pics.delete(url)); // tried again on a later poll
	return null;
}

// Companion JSON -> what the page draws.
function toView(feed) {
	const power = resourceView(feed.power);
	power.label = (feed.power && feed.power.type_name ? feed.power.type_name : "Power").toUpperCase();
	power.color = POWER_COLORS[feed.power ? feed.power.type : 0] || "#8892a6";
	return {
		zone: feed.zone || "",
		name: feed.name || "",
		race: feed.race || "",
		faction: feed.faction || "",
		className: feed.class_name || "",
		classId: feed.class_id || 0,
		level: feed.level || 0,
		mapId: feed.map_id || 0,
		mapReady: !!(feed.map && feed.map.ready),
		mapVer: feed.map && feed.map.ver ? feed.map.ver : 0,
		artMessages: (() => {
			const a = feed.art, out = [];
			if (!a) return out;
			if (a.error) out.push("Game files: " + a.error);
			if (a.last_error) out.push("Map/icon: " + a.last_error);
			if (a.warning) out.push(a.warning);
			if (a.source === "ready" && feed.map && !feed.map.ready && a.chunks > 40 && a.descriptors === 0) out.push("Addon map data not received");
			if (a.source === "ready" && feed.map && !feed.map.ready && a.chunks === 0) out.push("Waiting for map data from the addon");
			return out;
		})(),
		versions: feed.versions || null,
		inCombat: !!(feed.flags && feed.flags.in_combat),
		health: resourceView(feed.health),
		power,
		xp: (() => {
			const x = feed.xp;
			const pct = x ? x.pct : 0;
			return { pct, pctText: Math.round(pct) + "%", text: x && x.max ? fmt(x.cur) + " / " + fmt(x.max) : "—", known: true };
		})(),
		pos: feed.pos || null,
		facing: feed.facing,
		bag: feed.bag || { free: 0, total: 0, slots: [] },
		char: feed.char || null,
		gear: feed.gear || [],
		prof: feed.prof || [],
		hideXp: !!(feed.char && feed.char.maxlevel),
		maxes: { hp: feed.health ? feed.health.max : null, mp: feed.power ? feed.power.max : null,
			mpName: feed.power && feed.power.type_name ? feed.power.type_name : "Power" },
	};
}

const MOCK = { t: 0 };
function getMockFeed() {
	MOCK.t += 0.05;
	const t = MOCK.t;
	const secret = Math.sin(t * 0.3) > 0.4; // pretend combat: exact numbers hidden, bar only
	const hpPct = 50 + 45 * Math.sin(t);
	const slots = [];
	for (let i = 0; i < 36; i++) slots.push(i % 5 === 4 ? -1 : [1, 2, 3, 4, 0][i % 5]);
	return {
		ok: true, status: "ok", art: /mockerr/.test(location.hash) ? { source: "ready", last_error: "icon 133884: not in the local archives; web fallback failed: HTTP 404", warning: "map 1439: 12 of 30 pictures are not on this PC" } : undefined, zone: "Westfall", level: 16, name: "Arthas Menethil", race: "Human",
		faction: "Alliance", class_name: "Paladin", class_id: 2, map_id: 0, map: { ready: false },
		flags: { in_combat: secret },
		health: { cur: secret ? null : Math.round(hpPct * 2000), max: 200000, pct: hpPct, source: secret ? "bar" : "value" },
		power: { cur: Math.round(4000 + 3000 * Math.sin(t * 0.7)), max: 10000, pct: 40 + 30 * Math.sin(t * 0.7), type: 0, type_name: "Mana" },
		xp: { cur: 1, max: 2, pct: (t * 3) % 100 },
		pos: { x: 0.5 + 0.3 * Math.cos(t * 0.5), y: 0.5 + 0.25 * Math.sin(t * 0.5) },
		facing: (t * 0.5) % (2 * Math.PI),
		bag: { free: slots.filter((q) => q < 0).length, total: slots.length, slots,
			items: slots.map((q, i) => ({ q, icon: 0, count: q >= 0 ? 1 + (q * 7) % 20 : 0, ready: false, item: q >= 0 ? 1000 + i : 0, tip: true })) },
		char: { maxlevel: /mockmax/.test(location.hash), ilvl: 23.4, speed: 102, str: 29, agi: 44, sta: 44, int: 49, spi: 50, mh_min: 23, mh_max: 32, ap: 38,
			hit: 2.0, crit: 5.2, spdmg: 6, spheal: 6, def: 85, defmax: 90, dodge: 7.3, armor: 402, buffs: "str:+,agi:+,sta:+,int:+,spi:+,armor:+" },
		gear: [1, 2, 3, 15, 5, 9, 10, 6, 7, 8, 11, 13, 16, 18].map((slot, i) => ({ slot, item: 4000 + slot, icon: 0, q: [1, 2, 3, 4][i % 4], ilvl: 18 + (i % 7), ready: false, tip: true })),
		prof: [{ kind: "primary", name: "Herbalism", cur: 120, max: 150, icon: 0 }, { kind: "primary", name: "Alchemy", cur: 95, max: 150, icon: 0 },
			{ kind: "secondary", name: "Fishing", cur: 40, max: 75, icon: 0 }, { kind: "secondary", name: "Cooking", cur: 1, max: 75, icon: 0 }],
	};
}

function statusMessage(feed) {
	if (feed.status === "ok") return "";
	if (feed.status === "strip_not_found") return "Waiting for WoW (addon strip not found)";
	if (feed.status === "stale") return "WoW paused or loading";
	return "Waiting for companion";
}

// ---------- Rendering ----------
function getIcueProperty(name) {
	if (typeof window !== "undefined" && Object.prototype.hasOwnProperty.call(window, name)) {
		const v = window[name];
		if (v !== undefined && v !== null && v !== "") return v;
	}
	try {
		const v = Function('return typeof ' + name + ' !== "undefined" ? ' + name + " : undefined")();
		if (v !== undefined && v !== null && v !== "") return v;
	} catch (e) {}
	return undefined;
}

function shade(hex, amount) {
	const n = parseInt(hex.slice(1), 16);
	const f = (v) => Math.max(0, Math.min(255, Math.round(v + 255 * amount)));
	const r = f(n >> 16), g = f((n >> 8) & 255), b = f(n & 255);
	return "#" + ((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1);
}

function setBar(id, v, label) {
	const el = $(id);
	el.querySelector(".fill").style.width = Math.max(0, Math.min(100, v.pct)) + "%";
	el.querySelector(".mid").textContent = v.text;
	el.querySelector(".pct").textContent = v.pctText;
	if (label) el.querySelector("small").textContent = label;
	if (id === "health") el.classList.toggle("low", v.known !== false && v.pct < 25);
}

function drawMap(view) {
	const c = $("map");
	const dpr = window.devicePixelRatio || 1;
	const w = c.clientWidth, h = c.clientHeight;
	if (!w || !h) return;
	if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; }
	const g = c.getContext("2d");
	g.setTransform(dpr, 0, 0, dpr, 0, 0);
	g.clearRect(0, 0, w, h);

	// the real map picture, fitted (not stretched) into the panel; positions are relative to it
	let box = { x: 0, y: 0, w, h };
	let haveArt = false;
	const src = view.mapReady ? pic(BASE_URL + "/map/" + view.mapId + ".png?v=" + view.mapVer) : null;
	if (src) {
		let img = drawMap.img;
		if (!img || img.dataset.src !== src) {
			img = new Image();
			img.dataset.src = src;
			img.src = src;
			drawMap.img = img;
		}
		if (img.complete && img.naturalWidth) {
			const k = Math.min(w / img.naturalWidth, h / img.naturalHeight);
			box = { w: img.naturalWidth * k, h: img.naturalHeight * k };
			box.x = (w - box.w) / 2; box.y = (h - box.h) / 2;
			g.drawImage(img, box.x, box.y, box.w, box.h);
			haveArt = true;
		}
	}
	if (!haveArt) {
		g.strokeStyle = "#1c212b"; g.lineWidth = 1;
		const step = Math.max(20, Math.round(h / 10));
		for (let x = 0; x < w; x += step) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); }
		for (let y = 0; y < h; y += step) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
		if (view.pos && !view.mapReady) {
			g.fillStyle = "#5b6270"; g.font = Math.round(h * 0.05) + "px Segoe UI, sans-serif";
			g.textAlign = "center"; g.fillText("Map picture not captured yet", w / 2, h - h * 0.04);
		}
	}

	if (!view.pos) {
		g.fillStyle = "#5b6270"; g.font = Math.round(h * 0.06) + "px Segoe UI, sans-serif";
		g.textAlign = "center"; g.fillText("No position data here", w / 2, h / 2);
		return;
	}
	const px = box.x + view.pos.x * box.w, py = box.y + view.pos.y * box.h, s = Math.max(10, h * 0.045);
	g.save();
	g.translate(px, py);
	g.rotate(-(view.facing || 0)); // WoW facing: 0 = north, counterclockwise
	g.fillStyle = accent; g.strokeStyle = "#fff"; g.lineWidth = 2;
	g.beginPath(); g.moveTo(0, -s); g.lineTo(s * 0.75, s); g.lineTo(0, s * 0.5); g.lineTo(-s * 0.75, s); g.closePath();
	g.fill(); g.stroke();
	g.restore();
}

function drawBag(bag) {
	const root = $("slots");
	const items = bag.items || bag.slots.map((q) => ({ q, icon: 0, count: 0, ready: false }));
	const n = items.length;
	const rows = Math.max(4, Math.ceil(n / 8));
	if (root.children.length !== n) {
		root.innerHTML = "";
		for (let i = 0; i < n; i++) { const d = document.createElement("div"); d.className = "slot"; root.appendChild(d); }
	}
	for (let i = 0; i < n; i++) {
		const it = items[i];
		const el = root.children[i];
		const src = it.q >= 0 && it.ready ? pic(BASE_URL + "/icon/" + it.icon + ".png") : null;
		const key = it.q + "|" + (src || "") + "|" + it.count + "|" + (it.failed ? 1 : 0);
		if (el.dataset.key === key) continue;
		el.dataset.key = key;
		el.className = "slot" + (it.q >= 0 ? " q-" + it.q : "") + (it.q >= 0 && !src ? " noicon" : "");
		el.textContent = "";
		if (it.q < 0) continue;
		if (src) {
			const img = document.createElement("img");
			img.src = src;
			el.appendChild(img);
		} else {
			el.textContent = it.failed ? "?" : "◆"; // placeholder until the icon is available
		}
		if (it.count > 1) {
			const cnt = document.createElement("span");
			cnt.className = "cnt";
			cnt.textContent = it.count;
			el.appendChild(cnt);
		}
	}
	$("bagfree").textContent = bag.free + " free / " + bag.total + " slots";
}

// ---------- Column 1 views (Info / Gear / Professions) and the paper doll ----------
// character sheet sections, like the game's character window: [title, [[key, label, kind]...]]
// kind: "int" (default), "pct" (one decimal + %), "dec" (one decimal)
const STAT_COLUMNS = [
	[["General", [["hp", "Health"], ["mp", "Mana"], ["speed", "Movement Speed", "pct0"], ["ilvl", "Item Level", "dec"]]],
	 ["Primary Attributes", [["str", "Strength"], ["agi", "Agility"], ["sta", "Stamina"], ["int", "Intellect"], ["spi", "Spirit"]]]],
	[["Weapons", [["mh", "Main Hand"], ["ap", "Attack Power"]]],
	 ["Modifiers", [["hit", "Hit Chance", "pct"], ["crit", "Critical Strike", "pct"], ["spdmg", "Spell Damage"], ["spheal", "Spell Healing"],
		["haste", "Haste", "pct"], ["mastery", "Mastery", "pct"], ["vers", "Versatility", "pct"]]],
	 ["Defense", [["def", "Defense"], ["dodge", "Dodge", "pct"], ["parry", "Parry", "pct"], ["block", "Block", "pct"], ["armor", "Armor"]]]],
];
// inventory slot ids, laid out like the character window
const DOLL_LEFT = [[1, "Head"], [2, "Neck"], [3, "Shoulder"], [15, "Back"], [5, "Chest"], [4, "Shirt"], [19, "Tabard"], [9, "Wrist"]];
const DOLL_RIGHT = [[10, "Hands"], [6, "Waist"], [7, "Legs"], [8, "Feet"], [11, "Ring"], [12, "Ring"], [13, "Trinket"], [14, "Trinket"]];
const DOLL_WEAP = [[16, "Main hand"], [17, "Off hand"], [18, "Ranged"]];
let gearBySlot = {};

let currentView = "info";
try { currentView = localStorage.getItem("wowxe.view") || "info"; } catch (e) { /* no storage */ }
if (!["info", "gear", "prof"].includes(currentView)) currentView = "info";
function setView(v) {
	currentView = v;
	document.body.dataset.view = v;
	hideTip();
	try { localStorage.setItem("wowxe.view", v); } catch (e) { /* no storage */ }
}

function statValue(c, key, kind, view) {
	if (key === "hp") return view.maxes.hp ? fmt(view.maxes.hp) : null;
	if (key === "mp") return view.maxes.mp ? fmt(view.maxes.mp) : null;
	if (key === "mh") return c.mh_min !== undefined && c.mh_max !== undefined ? c.mh_min + " - " + c.mh_max : null;
	if (key === "def") return c.def !== undefined ? c.def + (c.defmax ? " / " + c.defmax : "") : null;
	const v = c[key];
	if (v === undefined || v === null) return null;
	const n = Number(v);
	if (kind === "pct") return n.toFixed(1) + "%";
	if (kind === "pct0") return Math.round(n) + "%";
	if (kind === "dec") return n.toFixed(1);
	return fmt(Math.round(n));
}

function drawStats(view) {
	const root = $("stats");
	const c = view.char;
	const sig = JSON.stringify([c, view.maxes]);
	if (root.dataset.sig === sig) return;
	root.dataset.sig = sig;
	root.innerHTML = "";
	if (!c) { root.innerHTML = '<div class="empty">Waiting for character data from the game…</div>'; return; }
	const buffs = {};
	for (const b of String(c.buffs || "").split(",")) { const [k, d] = b.split(":"); if (k) buffs[k] = d; }
	for (const col of STAT_COLUMNS) {
		const colEl = document.createElement("div");
		colEl.className = "scol";
		for (const [title, rows] of col) {
			const items = rows.map(([key, label, kind]) => [key, label, statValue(c, key, kind, view)]).filter((r) => r[2] !== null);
			if (!items.length) continue;
			const h = document.createElement("div");
			h.className = "shead";
			h.textContent = title === "General" ? "General" : title;
			colEl.appendChild(h);
			for (const [key, label, value] of items) {
				const row = document.createElement("div");
				row.className = "srow";
				const l = document.createElement("span"), v = document.createElement("b");
				l.textContent = (key === "mp" ? view.maxes.mpName : label) + ":";
				v.textContent = value;
				if (buffs[key] === "+") v.className = "up";
				else if (buffs[key] === "-") v.className = "down";
				row.appendChild(l); row.appendChild(v);
				colEl.appendChild(row);
			}
		}
		root.appendChild(colEl);
	}
}

function drawProfs(view) {
	const root = $("profs");
	const list = view.prof || [];
	const srcs = list.map((p) => (p.icon > 0 && p.ready ? pic(BASE_URL + "/icon/" + p.icon + ".png") : null));
	const sig = JSON.stringify([list, srcs]);   // pictures load asynchronously: redraw once each is in
	if (root.dataset.sig === sig) return;
	root.dataset.sig = sig;
	root.innerHTML = "";
	if (!list.length) { root.innerHTML = '<div class="empty">No professions received from the game yet…</div>'; return; }
	for (const kind of ["primary", "secondary"]) {
		const group = list.filter((p) => p.kind === kind);
		if (!group.length) continue;
		const h = document.createElement("h3");
		h.textContent = kind === "primary" ? "Primary" : "Secondary";
		root.appendChild(h);
		for (const p of group) {
			const row = document.createElement("div");
			row.className = "prof " + kind;
			const icon = document.createElement("div");
			icon.className = "pic";
			const src = srcs[list.indexOf(p)];
			if (src) icon.style.backgroundImage = "url(" + src + ")";
			const bar = document.createElement("div");
			bar.className = "pbar";
			const fill = document.createElement("div");
			fill.className = "pfill";
			fill.style.width = (p.max > 0 ? Math.min(100, (100 * p.cur) / p.max) : 0) + "%";
			const txt = document.createElement("div");
			txt.className = "ptxt";
			const a = document.createElement("span"), b = document.createElement("span");
			a.textContent = p.name;
			b.textContent = p.cur + " / " + p.max;
			txt.appendChild(a); txt.appendChild(b);
			bar.appendChild(fill); bar.appendChild(txt);
			row.appendChild(icon); row.appendChild(bar);
			root.appendChild(row);
		}
	}
}

function gearSlot(slot, label) {
	const g = gearBySlot[slot];
	const src = g && g.ready && g.icon > 0 ? pic(BASE_URL + "/icon/" + g.icon + ".png") : null;
	const key = slot + "|" + (g ? g.item + "|" + g.q + "|" + g.ilvl : "") + "|" + (src || "");
	return { key, build(el) {
		el.className = "gslot" + (g ? " q-" + g.q : "");
		el.dataset.slot = slot;
		el.textContent = "";
		if (!g) { el.textContent = label; return; }
		if (src) { const img = document.createElement("img"); img.src = src; el.appendChild(img); }
		else el.textContent = "◆";
	} };
}

function fillSlots(root, list) {
	if (root.children.length !== list.length) {
		root.innerHTML = "";
		for (let i = 0; i < list.length; i++) root.appendChild(document.createElement("div"));
	}
	list.forEach(([slot, label], i) => {
		const el = root.children[i];
		const s = gearSlot(slot, label);
		if (el.dataset.key === s.key) return;
		el.dataset.key = s.key;
		s.build(el);
	});
}

function drawDoll(view) {
	gearBySlot = {};
	for (const g of view.gear || []) gearBySlot[g.slot] = g;
	fillSlots($("dleft"), DOLL_LEFT);
	fillSlots($("dright"), DOLL_RIGHT);
	fillSlots($("dweap"), DOLL_WEAP);
	$("dname").textContent = view.name || "";
	$("dname").style.color = CLASS_COLORS[view.classId] || "#e8eaf0";
	$("dsub").textContent = [view.level ? "Level " + view.level : "", view.race, view.className].filter(Boolean).join(" ");
	const il = view.char && view.char.ilvl;
	$("dilvl").textContent = il ? "Item level " + Number(il).toFixed(1) : "";
	const fi = $("dfaction"), fsrc = FACTION_ICONS[view.faction];
	if (fsrc) { if (fi.getAttribute("src") !== fsrc) fi.setAttribute("src", fsrc); fi.style.display = "block"; }
	else fi.style.display = "none";
}

let hintOpen = false;
function render(view, message, standby) {
	setBar("health", view.health, "HEALTH");
	setBar("mana", view.power, view.power.label);
	const fill = $("mana").querySelector(".fill");
	fill.style.background = "linear-gradient(90deg, " + shade(view.power.color, -0.15) + ", " + view.power.color + ")";
	setBar("xp", view.xp, "XP");
	$("xp").style.display = view.hideXp ? "none" : "";   // max level: no XP bar, like the game
	$("cname").textContent = view.name || "Character";
	const race = $("crace");
	race.textContent = view.race;
	race.style.color = FACTION_COLORS[view.faction] || "#d8dbe2";
	const cls = $("cclass");
	cls.textContent = view.className;
	cls.style.color = CLASS_COLORS[view.classId] || "#d8dbe2";
	const fi = $("faction");
	const fsrc = FACTION_ICONS[view.faction];
	if (fsrc) { if (fi.getAttribute("src") !== fsrc) fi.setAttribute("src", fsrc); fi.style.display = "block"; }
	else fi.style.display = "none";
	$("clevel").textContent = view.level ? "Level " + view.level : "";
	const p = view.pos;
	const coords = p ? (p.x * 100).toFixed(1) + ", " + (p.y * 100).toFixed(1) : "";
	$("maptitle").textContent = view.zone ? view.zone + (coords ? " - " + coords : "") : "Map";
	$("status").textContent = message;
	$("status").style.display = message ? "block" : "none";
	$("hud").classList.toggle("dim", !!message);
	const msgs = view.artMessages || [];
	const btn = $("errbtn"), hint = $("arthint");
	const sig = msgs.join("\n");
	if (hint.dataset.sig !== sig) {
		hint.dataset.sig = sig;
		hint.innerHTML = "";
		for (const m of msgs) { const p = document.createElement("p"); p.textContent = m; hint.appendChild(p); }
	}
	btn.style.display = msgs.length ? "block" : "none";
	if (!msgs.length) hintOpen = false;
	hint.style.display = msgs.length && hintOpen ? "block" : "none";
	document.body.classList.toggle("standby", !!standby);
	drawMap(view);
	drawBag(view.bag);
	tipItems = view.bag.items || [];
	drawStats(view);
	drawProfs(view);
	drawDoll(view);
	const v = view.versions || {};
	const parts = ["widget " + WIDGET_VERSION, "companion " + (v.companion || "?"), "addon " + (v.addon || "?")];
	const ver = $("ver");
	ver.textContent = parts.join("  ·  ");
	ver.classList.toggle("bad", !!((v.companion && v.companion !== WIDGET_VERSION) || (v.addon && v.addon !== WIDGET_VERSION)));
}

// ---------- Loop ----------
let timer = null;
let busy = false;
async function tick() {
	if (busy) return;
	busy = true;
	try {
		if (USE_MOCK) {
			render(toView(getMockFeed()), "");
		} else {
			let feed;
			try {
				feed = await fetchFeed();
			} catch (e) {
				feed = { ok: false, status: "offline" };
			}
			const message = feed.ok ? "" : feed.status === "offline"
				? "Companion offline: start wowxe_companion.py"
				: statusMessage(feed);
			if (feed.ok || feed.status === "stale") lastGood = toView(feed);
			const standby = !feed.ok && feed.status !== "stale";
			if (lastGood) render(lastGood, message, standby);
			else render(toView({ health: null, power: null, xp: null, bag: { free: 0, total: 0, slots: [] } }), message, standby);
		}
	} catch (e) {
		/* keep the last frame */
	} finally {
		busy = false;
	}
}

// ---------- Item tooltip (tap a backpack slot) ----------
let tipKey = null, tipTimer = null, tipItems = [];
function hideTip() {
	tipKey = null;
	clearTimeout(tipTimer);
	$("tip").style.display = "none";
	for (const el of document.querySelectorAll(".sel")) el.classList.remove("sel");
}
function placeTip(el) {
	const tip = $("tip"), r = el.getBoundingClientRect();
	tip.style.display = "block";
	const w = tip.offsetWidth, h = tip.offsetHeight;
	let x = r.left - w - 8;
	if (x < 4) x = Math.min(r.right + 8, innerWidth - w - 4);
	const y = Math.max(4, Math.min(r.top + r.height / 2 - h / 2, innerHeight - h - 4));
	tip.style.left = x + "px";
	tip.style.top = y + "px";
}
function fillTip(data, message, icon) {
	const tip = $("tip");
	tip.innerHTML = "";
	if (!data) {
		const d = document.createElement("div");
		d.className = "wait";
		d.textContent = message;
		tip.appendChild(d);
		return;
	}
	data.lines.forEach((ln, i) => {
		const row = document.createElement("div");
		row.className = "row" + (i === 0 ? " name" : "");
		const l = document.createElement("span"), r = document.createElement("span");
		l.textContent = ln.l;
		r.textContent = ln.r;
		l.style.color = r.style.color = "#" + ln.c;
		row.appendChild(l);
		row.appendChild(r);
		tip.appendChild(row);
	});
}
// key "b<index>" = backpack slot, "g<slot>" = equipped item
function resolveTip(key) {
	if (key[0] === "b") {
		const idx = Number(key.slice(1));
		const it = tipItems[idx];
		return { el: $("slots").children[idx], item: it && it.q >= 0 ? it.item : null, has: !!(it && it.q >= 0) };
	}
	const slot = Number(key.slice(1));
	const g = gearBySlot[slot];
	return { el: document.querySelector('#doll .gslot[data-slot="' + slot + '"]'), item: g ? g.item : null, has: !!g };
}
async function showTip(key) {
	const t = resolveTip(key);
	if (!t.el || !t.has) return hideTip();
	tipKey = key;
	for (const e of document.querySelectorAll(".sel")) e.classList.remove("sel");
	t.el.classList.add("sel");
	let data = null;
	if (t.item) {
		try {
			const r = await fetch(BASE_URL + "/tip/" + t.item + ".json");
			if (r.ok) data = await r.json();
		} catch (e) { /* offline */ }
	}
	if (tipKey !== key) return;
	fillTip(data, t.item ? "Tooltip not received from the game yet…" : "Item not identified yet…");
	placeTip(resolveTip(key).el || t.el);
	if (!data) {
		clearTimeout(tipTimer);
		tipTimer = setTimeout(() => { if (tipKey === key) showTip(key); }, 2000);
	}
}

function init() {
	$("slots").addEventListener("click", (e) => {
		const el = e.target.closest(".slot");
		if (!el) return;
		const key = "b" + Array.prototype.indexOf.call($("slots").children, el);
		e.stopPropagation();
		if (key === tipKey) hideTip(); else showTip(key);
	});
	$("doll").addEventListener("click", (e) => {
		const el = e.target.closest(".gslot");
		if (!el) return;
		const key = "g" + el.dataset.slot;
		e.stopPropagation();
		if (key === tipKey) hideTip(); else showTip(key);
	});
	for (const b of document.querySelectorAll("#tabs button")) {
		b.addEventListener("click", (e) => { e.stopPropagation(); setView(b.dataset.view); });
	}
	setView(currentView);
	document.addEventListener("click", hideTip);
	$("errbtn").addEventListener("click", () => {
		hintOpen = !hintOpen;
		$("arthint").style.display = hintOpen ? "block" : "none";
	});
	update();
	if (!timer) timer = setInterval(tick, POLL_MS);
	tick();
}

function update() {
	accent = getIcueProperty("accentColor") || "#e74c3c";
}

if (typeof iCUE_initialized !== "undefined" && iCUE_initialized) init();
else if (typeof iCUE_initialized === "undefined") init();
