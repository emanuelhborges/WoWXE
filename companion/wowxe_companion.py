#!/usr/bin/env python3
"""
WoWXE companion (protocol v3)

Reads the pixel strip drawn by the WoWXE addon from the top-left corner of a
monitor and serves the decoded values as JSON on http://127.0.0.1:8765/state.json
for the iCUE widget. Display only: it never sends input to the game.

    pip install mss numpy
    python wowxe_companion.py                 # primary monitor
    python wowxe_companion.py --monitor 2     # WoW is on monitor 2
    python wowxe_companion.py --image shot.png --once   # decode a screenshot and exit

WoW must run in Windowed or Windowed (Fullscreen) mode so the screen can be captured.
"""
import argparse
import io
import json
import os
import queue
import re
import struct
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

# ---- layout: must match WoWXE.lua -------------------------------------
CELL = 3
PROTOCOL = 3
BAR_W = 256
BAG_CELLS = 64
ZONE_CELLS = 12
NAME_CELLS = 12
RACE_CELLS = 8
DATA_CELLS = 24
STRIP_W = max(BAG_CELLS * CELL, BAR_W)
STRIP_H = 11 * CELL
STREAM_ROW = 10
VERSION = "1.3.0"   # release number shared by addon, companion and widget
STREAM_PAYLOAD = (BAG_CELLS - 2) * 3
CAP_Y = 12 * CELL          # capture area (map / icon art) below the strip
CAP_W, CAP_H = 804, 536
KIND_ICON, KIND_MAP = 1, 2
FACTIONS = {0: "", 1: "Alliance", 2: "Horde", 3: "Neutral"}

SYNC = [(255, 0, 255), (0, 255, 255), (255, 0, 255), (0, 255, 255)]

QUALITY_RGB = {
    0: (157, 157, 157), 1: (255, 255, 255), 2: (30, 255, 0), 3: (0, 112, 221),
    4: (163, 53, 238), 5: (255, 128, 0), 6: (230, 204, 128), 7: (0, 204, 255),
}

POWER_NAMES = {
    0: "Mana", 1: "Rage", 2: "Focus", 3: "Energy", 5: "Runes", 6: "Runic Power",
    7: "Soul Shards", 8: "Astral Power", 9: "Holy Power", 11: "Maelstrom", 12: "Chi",
    13: "Insanity", 16: "Arcane Charges", 17: "Fury", 18: "Pain", 19: "Essence",
}
CLASS_NAMES = {
    1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest", 6: "Death Knight",
    7: "Shaman", 8: "Mage", 9: "Warlock", 10: "Monk", 11: "Druid", 12: "Demon Hunter",
    13: "Evoker",
}

STALE_AFTER = 1.5  # seconds without the addon's heartbeat changing


# ---- decoding ----------------------------------------------------------------
def _px(frame, ox, oy, row, col):
    """Center pixel of a cell as an (r, g, b) tuple of ints."""
    y = oy + row * CELL + CELL // 2
    x = ox + col * CELL + CELL // 2
    r, g, b = frame[y, x]
    return int(r), int(g), int(b)


def find_origin(frame):
    """Find the top-left pixel of the strip by looking for the sync pattern."""
    h, w, _ = frame.shape
    magenta = (frame[:, :, 0] == 255) & (frame[:, :, 1] == 0) & (frame[:, :, 2] == 255)
    ys, xs = np.nonzero(magenta)
    if len(ys) == 0:
        return None
    # Only consider cell corners: a magenta pixel with no magenta to its left / above.
    for y, x in list(zip(ys, xs))[:5000]:
        if x > 0 and magenta[y, x - 1]:
            continue
        if y > 0 and magenta[y - 1, x]:
            continue
        if x + 4 * CELL > w or y + STRIP_H > h:
            continue
        if _check_sync(frame, int(x), int(y)):
            return int(x), int(y)
    return None


def _check_sync(frame, ox, oy):
    try:
        for i, color in enumerate(SYNC):
            if _px(frame, ox, oy, 0, i) != color:
                return False
        return True
    except IndexError:
        return False


def _u24(c):
    return c[0] * 65536 + c[1] * 256 + c[2]


def _u16(r, g):
    return r * 256 + g


def _bar_fraction(frame, ox, oy, row):
    y = oy + row * CELL + CELL // 2
    seg = frame[y, ox:ox + BAR_W, :].astype(np.float32)
    if seg.shape[0] < BAR_W:
        return None
    return float(np.clip(seg.mean(axis=1).sum() / 255.0 / BAR_W, 0.0, 1.0))


def _quality_from_color(c):
    if c == (0, 0, 0):
        return -1
    best, best_d = 1, 1e9
    for q, rgb in QUALITY_RGB.items():
        d = sum((a - b) ** 2 for a, b in zip(c, rgb))
        if d < best_d:
            best, best_d = q, d
    return best


def decode(frame, origin=None):
    """
    frame: HxWx3 uint8 RGB array containing the strip.
    Returns (state_dict, origin) or (None, None) if the strip is not found.
    """
    if origin is None or not _check_sync(frame, *origin):
        origin = find_origin(frame)
        if origin is None:
            return None, None
    ox, oy = origin

    d = [_px(frame, ox, oy, 0, c) for c in range(DATA_CELLS)]
    protocol, flags, beat = d[4]
    if protocol != PROTOCOL:
        return None, origin

    f = {
        "in_combat": bool(flags & 1),
        "health_cur_secret": bool(flags & 2),
        "health_max_secret": bool(flags & 4),
        "power_cur_secret": bool(flags & 8),
        "power_max_secret": bool(flags & 16),
        "pos_valid": bool(flags & 32),
        "bar_mirror_failed": bool(flags & 64),
        "facing_valid": bool(flags & 128),
    }

    map_id = _u16(d[5][0], d[5][1])
    level = d[5][2]
    bag_free, bag_total = d[6][2], d[7][2]
    pos = None
    if f["pos_valid"]:
        pos = {"x": _u16(d[6][0], d[6][1]) / 65535.0, "y": _u16(d[7][0], d[7][1]) / 65535.0}
    facing = None
    if f["facing_valid"]:
        facing = _u16(d[15][0], d[15][1]) / 65535.0 * 2 * np.pi

    def resource(cur_c, max_c, cur_secret, max_secret, bar_row):
        cur = None if cur_secret else _u24(cur_c)
        mx = None if max_secret else _u24(max_c)
        pct, source = None, "unavailable"
        if cur is not None and mx:
            pct, source = max(0.0, min(100.0, cur * 100.0 / mx)), "value"
        elif not f["bar_mirror_failed"] and bar_row is not None:
            frac = _bar_fraction(frame, ox, oy, bar_row)
            if frac is not None:
                pct, source = frac * 100.0, "bar"
        # The game hides the exact current value in some situations. When the maximum is known,
        # estimate the current value from the bar fill (precision is about max/480).
        est = None
        if cur is None and mx and source == "bar":
            est = int(round(mx * pct / 100.0))
        return {"cur": cur, "max": mx, "pct": pct, "source": source, "cur_est": est}

    health = resource(d[10], d[11], f["health_cur_secret"], f["health_max_secret"], 1)
    power = resource(d[12], d[13], f["power_cur_secret"], f["power_max_secret"], 2)
    power["type"] = d[14][0]
    power["type_name"] = POWER_NAMES.get(d[14][0], "Power")

    xp_cur, xp_max = _u24(d[8]), _u24(d[9])
    xp_pct = (xp_cur * 100.0 / xp_max) if xp_max else 0.0

    zone_bytes = bytearray()
    for c in range(ZONE_CELLS):
        zone_bytes.extend(_px(frame, ox, oy, 5, c))
    zone = bytes(zone_bytes).split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    def text(row, count):
        raw = bytearray()
        for c in range(count):
            raw.extend(_px(frame, ox, oy, row, c))
        return bytes(raw).split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    shown = min(bag_total, BAG_CELLS)
    slots = [_quality_from_color(_px(frame, ox, oy, 4, c)) for c in range(shown)]
    items = []
    for c in range(shown):
        q = slots[c]
        icon = _u24(_px(frame, ox, oy, 8, c)) if q >= 0 else 0
        count = _px(frame, ox, oy, 9, c)[0] if q >= 0 else 0
        items.append({"q": q, "icon": icon, "count": count})

    capture = {
        "kind": d[16][0], "id": _u24(d[17]), "w": _u16(d[18][0], d[18][1]),
        "h": _u16(d[19][0], d[19][1]), "seq": d[18][2],
    }
    faction = FACTIONS.get(d[19][2], "")

    stream = None
    s_hdr, s_cnt = _px(frame, ox, oy, STREAM_ROW, 0), _px(frame, ox, oy, STREAM_ROW, 1)
    if s_cnt[0] > 0:
        raw = bytearray()
        for c in range(2, BAG_CELLS):
            raw.extend(_px(frame, ox, oy, STREAM_ROW, c))
        stream = {"map_id": _u16(s_hdr[0], s_hdr[1]), "idx": s_hdr[2], "total": s_cnt[0],
                  "length": _u16(s_cnt[1], s_cnt[2]), "payload": bytes(raw)}

    state = {
        "protocol": protocol,
        "heartbeat": beat,
        "flags": f,
        "map_id": map_id,
        "zone": zone,
        "name": text(6, NAME_CELLS),
        "race": text(7, RACE_CELLS),
        "faction": faction,
        "map_art_missing": bool(d[20][0] & 1),
        "explore_missing": bool(d[20][0] & 2),
        "capture": capture,
        "_stream": stream,
        "level": level,
        "class_id": d[14][1],
        "class_name": CLASS_NAMES.get(d[14][1], ""),
        "pos": pos,
        "facing": facing,
        "health": health,
        "power": power,
        "xp": {"cur": xp_cur, "max": xp_max, "pct": xp_pct},
        "bag": {"free": bag_free, "total": bag_total, "slots": slots, "items": items},
    }
    return state, origin


# ---- art cache ---------------------------------------------------------------
class Captures:
    """Saves the map / icon art the addon briefly draws below the strip, once per id."""

    DIRS = {KIND_ICON: "icons", KIND_MAP: "maps"}

    def __init__(self, root):
        self.root = Path(root)
        self.ready = set()
        self.ver = {}
        self.flash_maps = set()   # maps saved from an on-screen capture: not overwritten by incomplete game-file maps
        self.fixed_maps = set()   # maps downloaded as complete pictures: never overwritten
        self._cur = None
        self._best = None
        for kind, name in self.DIRS.items():
            d = self.root / name
            d.mkdir(parents=True, exist_ok=True)
            for f in d.glob("*.png"):
                if f.stem.isdigit():
                    self.ready.add((kind, int(f.stem)))
                    if kind == KIND_MAP and f.with_suffix(".src").exists():
                        self.fixed_maps.add(int(f.stem))

    def path(self, kind, ident):
        return self.root / self.DIRS[kind] / f"{int(ident)}.png"

    def has(self, kind, ident):
        return (kind, ident) in self.ready

    def observe(self, frame, origin, cap):
        key = None
        if origin and cap["kind"] in self.DIRS and cap["id"] > 0:
            key = (cap["kind"], cap["id"], cap["seq"])
        if key != self._cur:
            self._finalize()
            self._cur, self._best = key, None
        if key is None:
            return
        ox, oy = origin
        w = max(1, min(CAP_W, cap["w"]))
        h = max(1, min(CAP_H, cap["h"]))
        y0, x0 = oy + CAP_Y, ox
        if y0 + h > frame.shape[0] or x0 + w > frame.shape[1]:
            return  # capture region too small: raise --region
        crop = frame[y0:y0 + h, x0:x0 + w]
        if (crop.max(axis=2) > 12).mean() > 0.05:   # something has actually been drawn
            self._best = crop.copy()                # keep the latest = best loaded frame

    def _finalize(self):
        if self._cur is None or self._best is None:
            return
        kind, ident, _ = self._cur
        if kind == KIND_MAP and ident in self.fixed_maps:
            return
        from PIL import Image
        target = self.path(kind, ident)
        tmp = target.with_suffix(".tmp")
        Image.fromarray(self._best).save(tmp, format="PNG")
        os.replace(tmp, target)
        self.ready.add((kind, ident))
        self.ver[(kind, ident)] = int(time.time())
        if kind == KIND_MAP:
            self.flash_maps.add(ident)


# ---- art from the game files -------------------------------------------------
def parse_descriptor(b):
    """Map description sent by the addon: base picture tiles and explored-area pictures."""
    map_id, lw, lh, tw, th = struct.unpack(">HHHHH", b[:10])
    nb = b[10]
    pos = 11
    base = [struct.unpack(">I", b[pos + 4 * i:pos + 4 * i + 4])[0] for i in range(nb)]
    pos += 4 * nb
    overlays = []
    n_ov = b[pos]
    pos += 1
    for _ in range(n_ov):
        w, h, ox, oy, nf = struct.unpack(">HHHHH", b[pos:pos + 10])
        pos += 10
        ids = [struct.unpack(">I", b[pos + 4 * i:pos + 4 * i + 4])[0] for i in range(nf)]
        pos += 4 * nf
        overlays.append({"w": w, "h": h, "ox": ox, "oy": oy, "ids": ids})
    return {"map_id": map_id, "lw": lw, "lh": lh, "tw": tw, "th": th, "base": base, "overlays": overlays}


def compose_map(desc, read, missing=None):
    """Build the world-map picture (base tiles + explored areas) like the in-game map.
    Pictures the install does not have are left blank and their ids appended to `missing`."""
    from PIL import Image
    d = parse_descriptor(desc)
    if missing is None:
        missing = []
    base_read = read

    def read(fid):  # noqa: F811 - tolerant wrapper
        try:
            return base_read(fid)
        except Exception:
            missing.append(fid)
            return None

    d["_loaded_base"] = 0
    canvas = Image.new("RGBA", (d["lw"], d["lh"]), (0, 0, 0, 255))
    cols = max(1, -(-d["lw"] // d["tw"]))
    for i, fid in enumerate(d["base"]):
        blob = read(fid)
        if blob is None:
            continue
        d["_loaded_base"] += 1
        tile = Image.open(io.BytesIO(blob)).convert("RGBA")
        canvas.paste(tile, ((i % cols) * d["tw"], (i // cols) * d["th"]))
    TILE = 256
    for ov in d["overlays"]:
        nw, nh = -(-ov["w"] // TILE), -(-ov["h"] // TILE)
        for j in range(nh):
            for i in range(nw):
                k = j * nw + i
                if k >= len(ov["ids"]) or not ov["ids"][k]:
                    continue
                pw = min(TILE, ov["w"] - i * TILE)
                ph = min(TILE, ov["h"] - j * TILE)
                blob = read(ov["ids"][k])
                if blob is None:
                    continue
                tile = Image.open(io.BytesIO(blob)).convert("RGBA").crop((0, 0, pw, ph))
                canvas.paste(tile, (ov["ox"] + i * TILE, ov["oy"] + j * TILE), tile)
    if d["base"] and d["_loaded_base"] == 0:
        raise RuntimeError("none of the %d base map pictures are in your install (e.g. id %d)" % (len(d["base"]), d["base"][0]))
    return canvas.convert("RGB")


def make_icon(blob):
    from PIL import Image
    im = Image.open(io.BytesIO(blob)).convert("RGBA")
    w, h = im.size
    return im.crop((int(w * 0.07), int(h * 0.07), int(w * 0.93 + 0.5), int(h * 0.93 + 0.5)))


class ArtBuilder:
    """Reads map and icon pictures straight from the game files, in a background thread."""

    RETRY_SECONDS = 30   # incomplete maps are rebuilt after this long
    POLL_SECONDS = 5

    def __init__(self, captures, game_dir, product=None, reader=None, web_fetch=None, web_icons=True):
        self.cap = captures
        self.web_fetch = web_fetch
        self.web_icons = web_icons
        self.failed_icons = set()
        self.game_dir, self.product = game_dir, product
        self.reader = reader
        self.status = "off" if (not game_dir and reader is None) else "loading"
        self.error = None
        self.q = queue.Queue()
        self._chunks = {}
        self._asked = set()
        self._maps_done = {}
        self._failed = set()
        self.last_error = None
        self.warning = None
        self.stats = {"chunks": 0, "descriptors": 0, "maps": 0, "icons": 0}
        if self.status != "off":
            # maps saved by the old on-screen capture lack newer explored areas: do not offer them
            # as current; they are replaced as soon as the game-file version is built
            self.cap.ready = {k for k in self.cap.ready if k[0] != KIND_MAP or k[1] in self.cap.fixed_maps}
            threading.Thread(target=self._run, daemon=True).start()

    # -- fed from the screen reader
    def feed_stream(self, st):
        if not st or st["total"] <= 0 or st["length"] <= 0 or st["map_id"] >= MSG_FIRST:
            return
        self.stats["chunks"] += 1
        key = (st["map_id"], st["total"], st["length"])
        chunks = self._chunks.setdefault(key, {})
        i = st["idx"]
        if i >= st["total"]:
            return
        n = min(STREAM_PAYLOAD, st["length"] - i * STREAM_PAYLOAD)
        if n <= 0:
            return
        chunks[i] = st["payload"][:n]
        if len(self._chunks) > 6:
            self._chunks.pop(next(iter(self._chunks)))
        if len(chunks) == st["total"]:
            data = b"".join(chunks[k] for k in range(st["total"]))
            del self._chunks[key]
            body, crc = data[:-2], int.from_bytes(data[-2:], "big")
            if sum(body) % 65536 != crc:
                return  # a chunk was read mid-update; the addon repeats them, so just wait
            if self._maps_done.get(st["map_id"]) == crc:
                return
            self._maps_done[st["map_id"]] = crc
            self.stats["descriptors"] += 1
            self.q.put(("map", st["map_id"], body, crc))

    def want_icon(self, icon):
        if icon > 0 and icon not in self._asked and not self.cap.has(KIND_ICON, icon):
            self._asked.add(icon)
            self.q.put(("icon", icon))

    # -- worker
    def _run(self):
        try:
            if self.reader is None:
                from casc import CascReader
                self.reader = CascReader(self.game_dir, self.product)
            self.status = "ready"
            print("Game files opened: map and item pictures will be read from them.")
        except Exception as e:
            self.status, self.error = "error", str(e)
            print(f"Could not open the game files ({e}). Use --casc-probe for details, or '/wowxe flash on' in game.")
            return
        read = self.reader.read
        retries = []   # (due time, job): maps built with missing pictures are tried again later
        while True:
            try:
                job = self.q.get(timeout=self.POLL_SECONDS)
            except queue.Empty:
                due = [r for r in retries if r[0] <= time.time()]
                for r in due:
                    retries.remove(r)
                    self.q.put(r[1])
                continue
            try:
                if job[0] == "icon":
                    _, icon = job
                    try:
                        blob = read(icon)
                    except Exception as e:
                        blob = self._icon_from_web(icon, e)   # raises when that fails too
                    make_icon(blob).save(self._tmp(KIND_ICON, icon), format="PNG")
                    self._done(KIND_ICON, icon, 1)
                    self.stats["icons"] += 1
                else:
                    _, map_id, body, crc = job[:4]
                    missing = []
                    img = compose_map(body, read, missing)
                    if map_id in self.cap.fixed_maps or (missing and map_id in self.cap.flash_maps):
                        continue   # keep the complete downloaded / on-screen picture
                    img.save(self._tmp(KIND_MAP, map_id), format="PNG")
                    d = parse_descriptor(body)
                    total = len(d["base"]) + sum(len(o["ids"]) for o in d["overlays"])
                    if missing:
                        tries = job[4] if len(job) > 4 else 0
                        if tries < 20:
                            retries.append((time.time() + self.RETRY_SECONDS, ("map", map_id, body, crc, tries + 1)))
                        self.warning = (f"map {map_id}: {len(missing)} of {total} pictures are not on this PC - "
                                        f"type /wowxe map in game to capture it")
                        print(f"{self.warning} (ids: {sorted(set(missing))[:12]})")
                    else:
                        self.warning = None
                    self._done(KIND_MAP, map_id, crc)
                    self.stats["maps"] += 1
            except Exception as e:
                key = job[:2]
                if job[0] == "icon":
                    self.failed_icons.add(job[1])
                self.last_error = f"{job[0]} {job[1]}: {e}"
                if key not in self._failed:
                    self._failed.add(key)
                    print(f"could not build {job[0]} {job[1]}: {e}")
                if job[0] == "map":
                    self._maps_done.pop(job[1], None)

    def _icon_from_web(self, icon, original):
        """Icons the game streams (not on disk) are fetched from wago.tools, which serves game files by id."""
        if not self.web_icons:
            raise original
        fetch = self.web_fetch or _http_get
        try:
            blob = fetch("https://wago.tools/api/casc/%d?download" % icon)
            from PIL import Image
            Image.open(io.BytesIO(blob)).load()   # must be a readable picture
            print(f"icon {icon}: not on this PC, downloaded from wago.tools")
            return blob
        except Exception as e:
            raise RuntimeError(f"{original}; web fallback failed: {e}")

    def _tmp(self, kind, ident):
        return self.cap.path(kind, ident).with_suffix(".tmp.png")

    def _done(self, kind, ident, ver):
        tmp = self._tmp(kind, ident)
        os.replace(tmp, self.cap.path(kind, ident))
        self.cap.ready.add((kind, ident))
        self.cap.ver[(kind, ident)] = ver


# ---- item tooltips (text sent by the addon through the stream) -----------------
MSG_PROF, MSG_GEAR, MSG_CHAR, MSG_VERSION, MSG_BAG_INDEX, MSG_TIP = 0xFFFA, 0xFFFB, 0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF
MSG_FIRST = MSG_PROF   # stream ids from here up are messages; lower ids are map descriptions


class ChunkAssembler:
    """Puts the stream's chunks back together; returns (message id, body) when a message is complete and valid."""

    def __init__(self):
        self._chunks = {}

    def feed(self, st):
        if not st or st["total"] <= 0 or st["length"] <= 0 or st["idx"] >= st["total"]:
            return None
        key = (st["map_id"], st["total"], st["length"])
        chunks = self._chunks.setdefault(key, {})
        n = min(STREAM_PAYLOAD, st["length"] - st["idx"] * STREAM_PAYLOAD)
        if n <= 0:
            return None
        chunks[st["idx"]] = st["payload"][:n]
        if len(self._chunks) > 6:
            self._chunks.pop(next(iter(self._chunks)))
        if len(chunks) != st["total"]:
            return None
        data = b"".join(chunks[k] for k in range(st["total"]))
        del self._chunks[key]
        body, crc = data[:-2], int.from_bytes(data[-2:], "big")
        if sum(body) % 65536 != crc:
            return None
        return st["map_id"], body


def parse_bag_index(body):
    n = int.from_bytes(body[:2], "big")
    out = []
    for i in range(n):
        o = 2 + i * 8
        if o + 8 > len(body):
            break
        out.append((int.from_bytes(body[o:o + 4], "big"), int.from_bytes(body[o + 4:o + 8], "big")))
    return out


def parse_char(body):
    """'key=value' lines -> dict (numbers as int/float, maxlevel as bool)."""
    out = {}
    for row in body.decode("utf-8", "replace").split("\n"):
        k, _, v = row.partition("=")
        if not k or not v:
            continue
        try:
            out[k] = float(v) if "." in v else int(v)
        except ValueError:
            out[k] = v
    out["maxlevel"] = bool(out.get("maxlevel"))
    return out


def parse_gear(body):
    n = body[0] if body else 0
    out = []
    for i in range(n):
        o = 1 + i * 12
        if o + 12 > len(body):
            break
        out.append({"slot": body[o], "item": int.from_bytes(body[o + 1:o + 5], "big"),
                    "icon": int.from_bytes(body[o + 5:o + 9], "big"), "q": body[o + 9],
                    "ilvl": int.from_bytes(body[o + 10:o + 12], "big")})
    return out


def parse_prof(body):
    out = []
    for row in body.decode("utf-8", "replace").split("\n"):
        f = row.split("|")
        if len(f) >= 5 and f[0] in ("P", "S"):
            try:
                out.append({"kind": "primary" if f[0] == "P" else "secondary", "name": f[1],
                            "cur": int(f[2]), "max": int(f[3]), "icon": int(f[4])})
            except ValueError:
                pass
    return out


def parse_tip(body):
    item = int.from_bytes(body[:4], "big")
    lines = []
    for row in body[4:].decode("utf-8", "replace").split("\n"):
        if len(row) < 7:
            continue
        left, _, right = row[6:].partition("\x1f")
        lines.append({"c": row[:6], "l": left, "r": right})
    return item, lines


class Tips:
    """Tooltips by item id, kept in memory and on disk (cache/tips/<id>.json)."""

    def __init__(self, root):
        self.dir = Path(root) / "tips"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mem = {}
        for f in self.dir.glob("*.json"):
            try:
                self.mem[int(f.stem)] = json.loads(f.read_text("utf-8"))
            except Exception:
                pass

    def put(self, item, lines):
        data = {"id": item, "lines": lines}
        if self.mem.get(item) != data:
            self.mem[item] = data
            tmp = self.dir / f"{item}.tmp"
            tmp.write_text(json.dumps(data), "utf-8")
            os.replace(tmp, self.dir / f"{item}.json")

    def get(self, item):
        return self.mem.get(item)


# ---- complete zone maps from the Warcraft wiki ------------------------------
# uiMapID -> picture name used on warcraft.wiki.gg (WorldMap-<name>_c60.jpg is the WoW Forever version)
WIKI_NAMES = {
    1411: "Durotar", 1412: "Mulgore", 1413: "Barrens", 1416: "Alterac", 1417: "Arathi", 1418: "Badlands",
    1419: "BlastedLands", 1420: "Tirisfal", 1421: "Silverpine", 1422: "WesternPlaguelands",
    1423: "EasternPlaguelands", 1424: "Hillsbrad", 1425: "Hinterlands", 1426: "DunMorogh",
    1427: "SearingGorge", 1428: "BurningSteppes", 1429: "Elwynn", 1430: "DeadwindPass", 1431: "Duskwood",
    1432: "LochModan", 1433: "Redridge", 1434: "Stranglethorn", 1435: "SwampOfSorrows", 1436: "Westfall",
    1437: "Wetlands", 1438: "Teldrassil", 1439: "Darkshore", 1440: "Ashenvale", 1441: "ThousandNeedles",
    1442: "StonetalonMountains", 1443: "Desolace", 1444: "Feralas", 1445: "Dustwallow", 1446: "Tanaris",
    1447: "Azshara", 1448: "Felwood", 1449: "UngoroCrater", 1450: "Moonglade", 1451: "Silithus",
    1452: "Winterspring",
}
WIKI_URL = "https://warcraft.wiki.gg/wiki/Special:FilePath/WorldMap-%s%s.jpg"


def _http_get(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "WoWXE-companion/1.0 (personal second-screen widget)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def wiki_candidates(map_id, zone):
    stems = []
    if map_id in WIKI_NAMES:
        stems.append(WIKI_NAMES[map_id])
    clean = re.sub(r"[^A-Za-z0-9 ]", "", zone or "")
    if clean:
        stems += [clean.replace(" ", ""), clean.split(" ")[0]]
    out = []
    for stem in dict.fromkeys(stems):
        for suffix in ("_c60", "", "-old"):
            out.append(WIKI_URL % (stem, suffix))
    return out


def normalize_map(blob):
    """Wiki pictures may be the full 1024x768 tile grid; the game map is the top-left 1002x668 of it."""
    from PIL import Image
    im = Image.open(io.BytesIO(blob)).convert("RGB")
    if im.size == (1024, 768):
        im = im.crop((0, 0, 1002, 668))
    return im


class WikiMaps:
    """Downloads a complete (fully explored) picture of the current zone once and keeps it."""

    def __init__(self, captures, fetch=None):
        self.cap = captures
        self.fetch = fetch or _http_get
        self.q = queue.Queue()
        self._asked = set()
        self.last_error = None
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, map_id, zone):
        if map_id and map_id not in self._asked and map_id not in self.cap.fixed_maps:
            self._asked.add(map_id)
            self.q.put((map_id, zone))

    def _run(self):
        while True:
            map_id, zone = self.q.get()
            got = False
            for url in wiki_candidates(map_id, zone):
                try:
                    blob = self.fetch(url)
                    im = normalize_map(blob)
                except Exception as e:
                    self.last_error = f"{url.rsplit('/', 1)[-1]}: {e}"
                    continue
                target = self.cap.path(KIND_MAP, map_id)
                tmp = target.with_suffix(".tmp.png")
                im.save(tmp, format="PNG")
                target.with_suffix(".src").write_text("wiki\n")
                os.replace(tmp, target)
                self.cap.fixed_maps.add(map_id)
                self.cap.ready.add((KIND_MAP, map_id))
                self.cap.ver[(KIND_MAP, map_id)] = int(time.time())
                print(f"Map {map_id} ({zone}): complete picture downloaded from the Warcraft wiki.")
                got = True
                break
            if not got:
                print(f"Map {map_id} ({zone}): no complete picture found online ({self.last_error}); using what the game files give.")


# ---- capture + server --------------------------------------------------------
class Tracker:
    """Turns decoded frames into the JSON the widget polls."""

    def __init__(self, captures=None, art=None, wiki=None):
        self.captures = captures
        self.art = art
        self.wiki = wiki
        self.tips = Tips(captures.root) if captures else None
        self._asm = ChunkAssembler()
        self._slot_items = []
        self.addon_version = ""
        self.char, self.gear, self.prof = None, [], []
        self._warned = False
        self._icon_seen = {}
        self._last_status = None
        self.dump_path = None
        self.lock = threading.Lock()
        self.state = {"ok": False, "status": "starting"}
        self._last_beat = None
        self._last_beat_time = 0.0
        self._origin = None
        self.monitor = None

    def update(self, frame):
        now = time.time()
        state, self._origin = decode(frame, self._origin)
        if state is None and self._origin is not None:
            out = {"ok": False, "status": "protocol_mismatch",
                   "hint": "The strip was found but the addon and companion versions differ. Update both."}
        elif state is None:
            out = {"ok": False, "status": "strip_not_found",
                   "hint": "Is WoW running in windowed/borderless mode on this monitor with the addon enabled?"}
        else:
            if state["heartbeat"] != self._last_beat:
                self._last_beat, self._last_beat_time = state["heartbeat"], now
            stale = (now - self._last_beat_time) > STALE_AFTER
            stream = state.pop("_stream", None)
            state["versions"] = {"companion": VERSION, "addon": self.addon_version}
            if stream and stream["map_id"] >= MSG_FIRST:
                try:
                    msg = self._asm.feed(stream)
                    if msg and msg[0] == MSG_VERSION:
                        self.addon_version = msg[1].decode("ascii", "replace")[:16]
                    elif msg and msg[0] == MSG_CHAR:
                        self.char = parse_char(msg[1])
                    elif msg and msg[0] == MSG_GEAR:
                        self.gear = parse_gear(msg[1])
                    elif msg and msg[0] == MSG_PROF:
                        self.prof = parse_prof(msg[1])
                    elif self.tips is None:
                        pass
                    elif msg and msg[0] == MSG_BAG_INDEX:
                        self._slot_items = parse_bag_index(msg[1])
                    elif msg and msg[0] == MSG_TIP:
                        self.tips.put(*parse_tip(msg[1]))
                except Exception as e:
                    print(f"stream message error: {e!r}")
            state["char"] = self.char
            state["gear"] = [dict(g) for g in self.gear]
            state["prof"] = [dict(p) for p in self.prof]
            for g in state["gear"]:
                g["tip"] = bool(self.tips and self.tips.get(g["item"]))
            for entry in state["gear"] + state["prof"]:
                if self.art and entry["icon"] > 0:
                    self.art.want_icon(entry["icon"])
                entry["ready"] = bool(self.captures and entry["icon"] > 0 and self.captures.has(KIND_ICON, entry["icon"]))
            if self.tips is not None:
                for i, it in enumerate(state["bag"]["items"]):
                    item = 0
                    if it["q"] >= 0 and i < len(self._slot_items) and self._slot_items[i][1] == it["icon"]:
                        item = self._slot_items[i][0]   # only trusted while the icon agrees with the strip
                    it["item"] = item
                    it["tip"] = bool(item and self.tips.get(item))
            if self.art:
                try:
                    self.art.feed_stream(stream)
                    ids = {i["icon"] for i in state["bag"]["items"] if 0 < i["icon"] < 0xFFFFFF}
                    self._icon_seen = {i: self._icon_seen.get(i, 0) + 1 for i in ids}
                    for i, n in self._icon_seen.items():
                        if n >= 8:          # same id for ~0.5 s: not a half-drawn frame
                            self.art.want_icon(i)
                except Exception as e:
                    print(f"art error: {e!r}")
            if self.wiki and state["map_id"]:
                self.wiki.request(state["map_id"], state["zone"])
            if self.captures:
                try:
                    self.captures.observe(frame, self._origin, state["capture"])
                except Exception as e:  # never let picture saving break the live data
                    if not self._warned:
                        self._warned = True
                        print(f"WARNING: could not save map/icon art: {e!r}  (is Pillow installed? pip install Pillow)")
                for item in state["bag"]["items"]:
                    item["ready"] = item["icon"] > 0 and self.captures.has(KIND_ICON, item["icon"])
                    item["failed"] = bool(self.art and item["icon"] in self.art.failed_icons and not item["ready"])
                state["map"] = {"id": state["map_id"],
                                "ready": self.captures.has(KIND_MAP, state["map_id"]),
                                "ver": self.captures.ver.get((KIND_MAP, state["map_id"]), 0)}
                state["art_source"] = (self.art.status if self.art else "off")
                if self.art:
                    state["art"] = {"source": self.art.status, "error": self.art.error,
                                    "last_error": self.art.last_error, "warning": self.art.warning,
                                    **self.art.stats}
            out = dict(state)
            out["ok"] = not stale
            out["status"] = "stale" if stale else "ok"
        out["time"] = now
        if out["status"] != self._last_status:
            self._last_status = out["status"]
            print(f"[{time.strftime('%H:%M:%S')}] status: {out['status']}"
                  + (f"  origin={self._origin}  frame={frame.shape[1]}x{frame.shape[0]}" if out["status"] != "ok" else ""))
        if self.dump_path and out["status"] != "ok":
            self._dump_due = getattr(self, "_dump_due", 0)
            if now >= self._dump_due:
                self._dump_due = now + 5
                from PIL import Image
                Image.fromarray(frame).save(self.dump_path)
        with self.lock:
            self.state = out

    def snapshot(self):
        with self.lock:
            snap = dict(self.state)
        snap["monitor"] = self.monitor
        return snap


def make_handler(tracker):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_art(self, kind, ident):
            cap = tracker.captures
            path = cap.path(kind, ident) if cap and cap.has(kind, ident) else None
            if not path or not path.exists():
                self._send(404, {"error": "not captured yet"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            t = re.fullmatch(r"/tip/(\d{1,10})\.json", self.path.split("?")[0])
            if t:
                tip = tracker.tips.get(int(t.group(1))) if tracker.tips else None
                self._send(200 if tip else 404, tip or {"error": "tooltip not received yet"})
                return
            m = re.fullmatch(r"/(icon|map)/(\d{1,10})\.png", self.path.split("?")[0])
            if m:
                self._send_art(KIND_ICON if m.group(1) == "icon" else KIND_MAP, int(m.group(2)))
                return
            if self.path.split("?")[0] in ("/state.json", "/"):
                snap = tracker.snapshot()
                snap["age_ms"] = int((time.time() - snap.get("time", 0)) * 1000)
                self._send(200, snap)
            else:
                self._send(404, {"error": "not found"})

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.end_headers()

        def log_message(self, *args):
            pass

    return Handler


class MonitorFollower:
    """Captures the corner of the monitor where the strip is, and looks for it again when it disappears
    (the game moved, Windows renumbered the monitors, or WoW runs in a window that is not in the corner).
    First the top-left corner of every monitor is checked (cheap); every few seconds the whole of every
    monitor is searched, and the capture box then follows the game window."""
    SEARCH_AFTER = 2.0    # seconds without the strip before the other monitors are checked
    SEARCH_EVERY = 1.5
    FULL_EVERY = 4.0      # whole-monitor search (windowed mode) at most this often
    MARGIN = 8            # pixels kept above/left of the strip in the capture box

    def __init__(self, sct, tracker, preferred, w, h, log=print):
        self.sct, self.tracker, self.w, self.h, self.log = sct, tracker, w, h, log
        count = len(sct.monitors) - 1
        self.cur = preferred if preferred and 1 <= preferred <= count else 1
        self.off = (0, 0)                                    # capture box offset inside the monitor
        self.last_found = time.time() - self.SEARCH_AFTER   # search straight away at start
        self.last_search = 0.0
        self.last_full = 0.0
        self.announced = None

    def _box(self, idx, off=(0, 0), full=False):
        mon = self.sct.monitors[idx]
        if full:
            return {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        ox, oy = off
        return {"left": mon["left"] + ox, "top": mon["top"] + oy,
                "width": max(1, min(self.w, mon["width"] - ox)), "height": max(1, min(self.h, mon["height"] - oy))}

    def _frame(self, idx, off=(0, 0), full=False):
        return np.asarray(self.sct.grab(self._box(idx, off, full)))[:, :, 2::-1].copy()   # BGRA -> RGB

    def _announce(self, why):
        mon = self.sct.monitors[self.cur]
        self.tracker.monitor = self.cur
        key = (self.cur, self.off)
        if self.announced != key:
            self.announced = key
            where = f", game window at +{self.off[0]},+{self.off[1]}" if self.off != (0, 0) else ""
            self.log(f"{why} monitor {self.cur}: {mon['width']}x{mon['height']} at ({mon['left']},{mon['top']}){where}")

    def _found(self, idx, off, now, why="Strip found on"):
        self.cur, self.off = idx, off
        self.tracker._origin = None
        self.last_found = now
        self._announce(why)

    def step(self):
        now = time.time()
        self.tracker.update(self._frame(self.cur, self.off))
        status = (self.tracker.state or {}).get("status")
        if status and status != "strip_not_found":
            self.last_found = now
            self._announce("Strip found on" if self.announced is None else "Capturing")
            return
        if now - self.last_found < self.SEARCH_AFTER or now - self.last_search < self.SEARCH_EVERY:
            return
        self.last_search = now
        count = len(self.sct.monitors) - 1
        order = [self.cur] + [i for i in range(1, count + 1) if i != self.cur]
        for idx in order:                                     # 1) top-left corner of each monitor
            if idx == self.cur and self.off == (0, 0):
                continue
            _, origin = decode(self._frame(idx))
            if origin is not None:
                return self._found(idx, (0, 0), now)
        if now - self.last_full >= self.FULL_EVERY:           # 2) whole monitors: windowed mode
            self.last_full = now
            for idx in order:
                origin = find_origin(self._frame(idx, full=True))
                if origin is not None:
                    off = (max(0, origin[0] - self.MARGIN), max(0, origin[1] - self.MARGIN))
                    return self._found(idx, off, now, "Strip found (windowed game) on")
        self.tracker.monitor = None


def capture_loop(tracker, monitor_index, fps, region_w, region_h, stop):
    import mss

    grabber = mss.MSS if hasattr(mss, "MSS") else mss.mss  # mss.mss was renamed mss.MSS
    with grabber() as sct:
        print(f"Looking for the WoW strip on {len(sct.monitors) - 1} monitor(s)"
              + (f", monitor {monitor_index} first" if monitor_index else " (automatic)")
              + f"; top-left region {region_w}x{region_h}")
        follower = MonitorFollower(sct, tracker, monitor_index, region_w, region_h)
        period = 1.0 / fps
        while not stop.is_set():
            t0 = time.time()
            try:
                follower.step()
            except Exception as e:
                print(f"capture error: {e!r}")
                time.sleep(1)
            time.sleep(max(0.0, period - (time.time() - t0)))


def probe(game_dir, product, ids=None):
    if not game_dir:
        print("Pass --game-dir with the WoW folder, e.g. --game-dir \"C:\\Games\\World of Warcraft\\_forever_\"")
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from casc import CascReader, CascError
    try:
        r = CascReader(game_dir, product)
        for fid, label in ((134400, "an icon (question mark)"), (1000000, "another file")):
            try:
                blob = r.read(fid)
            except CascError as e:
                print(f"FileDataID {fid} ({label}): {e}")
                continue
            from PIL import Image
            try:
                im = Image.open(io.BytesIO(blob))
                print(f"FileDataID {fid} ({label}): OK, {len(blob)} bytes, picture {im.size} {im.format}")
            except Exception as e:
                print(f"FileDataID {fid} ({label}): read {len(blob)} bytes but could not open as a picture: {e}")
        for fid in [int(x) for x in (ids or "").replace(" ", "").split(",") if x]:
            print(f"FileDataID {fid}: {r.describe_id(fid)}")
        print("PROBE FINISHED")
        return 0
    except CascError as e:
        print(f"PROBE FAILED: {e}")
        return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--monitor", default="auto",
                    help="monitor number to try first (1 = primary); 'auto' (default) finds the monitor "
                         "with the WoW strip by itself, also after Windows renumbers them")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--region", default=f"{CAP_W + 40}x{CAP_Y + CAP_H + 20}",
                    help="top-left capture size in pixels, WxH")
    ap.add_argument("--cache", default=str(Path(__file__).resolve().parent / "cache"),
                    help="folder where captured map and item art is kept")
    ap.add_argument("--dump", metavar="FILE", help="while the strip is not found, save what the companion sees "
                    "to this PNG every 5 s (to check the monitor and game mode)")
    ap.add_argument("--game-dir", default=os.environ.get("WOWXE_GAME_DIR"),
                    help="WoW folder (e.g. C:\\Games\\World of Warcraft\\_forever_): map and item pictures are read "
                         "from the game files, so nothing has to flash on screen")
    ap.add_argument("--no-web-icons", action="store_true",
                    help="do not download item icons missing from the game files")
    ap.add_argument("--no-wiki-maps", action="store_true",
                    help="do not download complete zone maps from the Warcraft wiki (use only the game files)")
    ap.add_argument("--product", help="product name from .build.info if the right one is not picked automatically")
    ap.add_argument("--casc-probe", action="store_true", help="test reading the game files and exit")
    ap.add_argument("--probe-ids", help="with --casc-probe: comma-separated file ids to describe, e.g. 8085570,8085571")
    ap.add_argument("--image", help="decode this screenshot instead of capturing the screen")
    ap.add_argument("--once", action="store_true", help="with --image: print JSON and exit")
    ap.add_argument("--version", action="version", version=f"WoWXE companion v{VERSION}")
    args = ap.parse_args()
    print(f"WoWXE companion v{VERSION}")

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # capture real pixels, not scaled ones
        except Exception:
            pass

    if args.casc_probe:
        sys.exit(probe(args.game_dir, args.product, args.probe_ids))
    captures = Captures(args.cache)
    art = ArtBuilder(captures, args.game_dir, args.product, web_icons=not args.no_web_icons) if args.game_dir else None
    wiki = None if args.no_wiki_maps else WikiMaps(captures)
    tracker = Tracker(captures, art, wiki)
    tracker.dump_path = args.dump

    if args.image:
        from PIL import Image
        frame = np.asarray(Image.open(args.image).convert("RGB"))
        state, _ = decode(frame)
        print(json.dumps(state, indent=2) if state else "strip not found")
        if args.once:
            return
        state = state or {"ok": False, "status": "strip_not_found"}
        # a static image never goes stale
        state["ok"], state["status"], state["time"] = bool(state.get("protocol")), "ok", time.time() + 1e9
        tracker.state = state
    else:
        w, h = (int(v) for v in args.region.lower().split("x"))
        monitor = int(args.monitor) if str(args.monitor).isdigit() else None
        stop = threading.Event()
        threading.Thread(target=capture_loop, args=(tracker, monitor, args.fps, w, h, stop),
                         daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(tracker))
    print(f"Serving http://127.0.0.1:{args.port}/state.json  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
