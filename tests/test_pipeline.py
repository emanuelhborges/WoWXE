#!/usr/bin/env python3
"""
End-to-end check without WoW: run the real addon Lua under a stubbed WoW API, render the
strip it describes to pixels (on top of random noise, with decoys), decode it with the
companion, and compare with what the stubbed game reported.

    pip install lupa numpy
    python tests/test_pipeline.py
"""
import io
import math
import pathlib
import sys
import tempfile
import time
import traceback

import numpy as np
from lupa import LuaRuntime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "companion"))
sys.path.insert(0, str(ROOT / "tests"))
import wowxe_companion as comp  # noqa: E402


def _img(path):
    """Open a picture without keeping the file open (Windows can't delete open files)."""
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        return im.copy()

PHYS_W, PHYS_H = 2560, 1440
CANVAS_W, CANVAS_H = 900, 660


def new_game():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute((ROOT / "tests" / "wow_stub.lua").read_text())
    lua.execute((ROOT / "addon" / "WoWXE" / "WoWXE.lua").read_text())
    return lua


def tick(lua, events=("PLAYER_ENTERING_WORLD",)):
    f = lua.globals().WoWXEFrame
    for e in events:
        f.scripts["OnEvent"](f, e)
    f.scripts["OnUpdate"](f, 0.2)
    return f


def render(lua, seed=0, jitter=False):
    rng = np.random.default_rng(seed)
    canvas = rng.integers(0, 256, (CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    # decoys: lone magenta block and a magenta+cyan pair that is not the full sync pattern
    canvas[150:155, 600:605] = (255, 0, 255)
    canvas[200:205, 300:305] = (255, 0, 255)
    canvas[200:205, 305:310] = (0, 255, 255)

    frame = lua.globals().WoWXEFrame
    ppu = frame.scale * PHYS_H / 768.0
    regions = [lua.globals().REGIONS[i] for i in range(1, len(lua.globals().REGIONS) + 1)]

    def visible(r):
        while r is not None and r is not frame:
            if not r.shown:
                return False
            r = r.parent
        return True

    def offset(r):
        """Top-left of a region in pixels, summing the offsets of its parents up to the strip."""
        x = y = 0.0
        while r is not None and r is not frame:
            x += r.x * ppu
            y += (-r.y) * ppu
            r = r.parent
        return x, y

    def draw(r):
        return offset(r)

    rng2 = np.random.default_rng(seed + 100)
    # jitter: the game may snap shared cell edges by up to one pixel
    bx = [c * comp.CELL + (0 if c == 0 else int(rng2.integers(-1, 2))) for c in range(80)]
    by = [r_ * comp.CELL + (0 if r_ == 0 else int(rng2.integers(-1, 2))) for r_ in range(16)]
    for r in regions:
        if r.kind == "tex" and visible(r):
            if jitter and r.parent.w == frame.w and r.parent.h == frame.h and not r.allPoints and r.w == comp.CELL and r.h == comp.CELL:
                col, row = int(round(r.x / comp.CELL)), int(round(-r.y / comp.CELL))
                c = tuple(int(round(v * 255)) for v in (r.color[1], r.color[2], r.color[3]))
                canvas[by[row]:by[row + 1], bx[col]:bx[col + 1]] = c
                continue
            if r.allPoints:
                px, py = offset(r.parent)
                x0, y0, w, h = px, py, r.parent.w * ppu, r.parent.h * ppu
            else:
                x0, y0 = draw(r)
                w, h = r.w * ppu, r.h * ppu
            if r.file is not None:   # art texture: a flat color derived from its file id
                fid = int(r.file)
                c = (30 + fid * 37 % 220, 30 + fid * 91 % 220, 30 + fid * 53 % 220)
            else:
                c = tuple(int(round(v * 255)) for v in (r.color[1], r.color[2], r.color[3]))
            canvas[int(round(y0)):int(round(y0 + h)), int(round(x0)):int(round(x0 + w))] = c
    for r in regions:
        if r.kind == "bar":
            x0, y0 = draw(r)
            w, h = r.w * ppu, r.h * ppu
            value, mx = r.value, r.max
            if value is None or mx is None:
                continue
            value = value["val"] if hasattr(value, "keys") else value
            mx = mx["val"] if hasattr(mx, "keys") else mx
            frac = max(0.0, min(1.0, value / mx)) if mx else 0.0
            fill = frac * w
            full = int(fill)
            ys = slice(int(round(y0)), int(round(y0 + h)))
            canvas[ys, int(round(x0)):int(round(x0)) + full] = (255, 255, 255)
            part = int(round((fill - full) * 255))
            if part and full < w:
                canvas[ys, int(round(x0)) + full] = (part, part, part)
    return canvas


def file_color(fid):
    return (30 + fid * 37 % 220, 30 + fid * 91 % 220, 30 + fid * 53 % 220)


def run(setup_lua, events=("PLAYER_ENTERING_WORLD",)):
    lua = new_game()
    lua.execute(setup_lua)
    tick(lua, events)
    state, origin = comp.decode(render(lua))
    return lua, state, origin


BASE = """
WORLD.health, WORLD.healthMax = 123456, 200000
WORLD.power, WORLD.powerMax, WORLD.ptype = 4000, 10000, 0
WORLD.xp, WORLD.xpMax, WORLD.level = 1234567, 2000000, 80
WORLD.mapID, WORLD.pos, WORLD.facing = 2022, { x = 0.4321, y = 0.7654 }, 1.0
WORLD.zone, WORLD.classID = "Elwynn Forest", 8
WORLD.bags = {
  [0] = { size = 16, items = { [1] = 1, [2] = 2, [3] = 3, [4] = 4, [5] = 5, [9] = 0, [16] = 1 } },
  [1] = { size = 8,  items = { [1] = 2, [8] = 4 } },
  [2] = { size = 12, items = {} },
}
"""

results = []


def case(fn):
    def wrapper():
        try:
            fn()
            results.append((fn.__name__, True, ""))
        except Exception:
            results.append((fn.__name__, False, traceback.format_exc(limit=3)))
    return wrapper


@case
def plain_values_out_of_combat():
    lua, s, origin = run(BASE)
    assert origin == (0, 0), origin
    assert s["health"]["cur"] == 123456 and s["health"]["max"] == 200000, s["health"]
    assert s["health"]["source"] == "value"
    assert abs(s["health"]["pct"] - 61.728) < 0.01
    assert s["power"]["cur"] == 4000 and s["power"]["type_name"] == "Mana"
    assert s["xp"]["cur"] == 1234567 and s["xp"]["max"] == 2000000
    assert abs(s["xp"]["pct"] - 61.72835) < 0.01
    assert s["level"] == 80 and s["map_id"] == 2022 and s["class_name"] == "Mage"
    assert abs(s["pos"]["x"] - 0.4321) < 1e-4 and abs(s["pos"]["y"] - 0.7654) < 1e-4, s["pos"]
    assert abs(s["facing"] - 1.0) < 1e-3, s["facing"]
    assert s["zone"] == "Elwynn Forest"
    assert not s["flags"]["in_combat"]
    assert s["bag"]["total"] == 36 and s["bag"]["free"] == 36 - 9, s["bag"]
    slots = s["bag"]["slots"]
    assert slots[0:5] == [1, 2, 3, 4, 5] and slots[8] == 0 and slots[15] == 1, slots[:16]
    assert slots[5] == -1 and slots[16] == 2 and slots[23] == 4 and slots[24] == -1


@case
def secret_values_in_combat():
    lua, s, _ = run(BASE + """
    WORLD.combat = true
    WORLD.health, WORLD.healthMax = Secret(70000), Secret(200000)
    WORLD.power, WORLD.powerMax = Secret(2500), Secret(10000)
    """)
    f = s["flags"]
    assert f["in_combat"] and f["health_cur_secret"] and f["health_max_secret"]
    assert f["power_cur_secret"] and f["power_max_secret"] and not f["bar_mirror_failed"]
    assert s["health"]["cur"] is None and s["health"]["max"] is None
    assert s["health"]["source"] == "bar"
    assert abs(s["health"]["pct"] - 35.0) <= 0.6, s["health"]
    assert abs(s["power"]["pct"] - 25.0) <= 0.6, s["power"]
    # non-secret data keeps flowing in combat
    assert s["zone"] == "Elwynn Forest" and s["level"] == 80


@case
def only_current_value_secret():
    lua, s, _ = run(BASE + "WORLD.health = Secret(50000)")
    assert s["flags"]["health_cur_secret"] and not s["flags"]["health_max_secret"]
    assert s["health"]["cur"] is None and s["health"]["max"] == 200000
    assert abs(s["health"]["cur_est"] - 50000) <= 200, s["health"]  # estimated from the bar, ~max/480
    assert s["health"]["source"] == "bar" and abs(s["health"]["pct"] - 25.0) <= 0.6


@case
def bar_mirror_rejects_secret():
    lua, s, _ = run("STUB.failSecretBarRange = true\n" + BASE +
                    "WORLD.health, WORLD.healthMax = Secret(70000), Secret(200000)")
    assert s["flags"]["bar_mirror_failed"]
    assert s["health"]["source"] == "unavailable" and s["health"]["pct"] is None, s["health"]


@case
def no_position_in_instance():
    lua, s, _ = run(BASE + "WORLD.mapID, WORLD.pos, WORLD.facing = 1234, nil, nil")
    assert s["pos"] is None and s["facing"] is None and s["map_id"] == 1234


@case
def long_and_accented_zone_names():
    lua, s, _ = run(BASE + 'WORLD.zone = "Vale de Terraço"')
    assert s["zone"] == "Vale de Terraço", repr(s["zone"])
    long_name = "The Veiled Stair of the Eternal Spring Terraces"
    lua, s, _ = run(BASE + f'WORLD.zone = "{long_name}"')
    assert s["zone"] == long_name[:36], repr(s["zone"])


@case
def other_power_types():
    lua, s, _ = run(BASE + "WORLD.ptype, WORLD.power, WORLD.powerMax = 1, 60, 100")
    assert s["power"]["type_name"] == "Rage" and s["power"]["cur"] == 60


@case
def strip_missing_returns_none():
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 256, (CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    state, origin = comp.decode(noise)
    assert state is None and origin is None


@case
def origin_found_when_offset():
    lua = new_game()
    lua.execute(BASE)
    tick(lua)
    strip = render(lua)[:comp.STRIP_H, :comp.STRIP_W]
    big = np.random.default_rng(3).integers(0, 256, (300, 900, 3), dtype=np.uint8)
    big[37:37 + comp.STRIP_H, 111:111 + comp.STRIP_W] = strip
    state, origin = comp.decode(big)
    assert origin == (111, 37) and state["zone"] == "Elwynn Forest"


@case
def heartbeat_stale_detection():
    lua = new_game()
    lua.execute(BASE)
    tick(lua)
    frame = render(lua)
    tr = comp.Tracker()
    tr.update(frame)
    assert tr.snapshot()["ok"] is True
    tr._last_beat_time -= comp.STALE_AFTER + 1  # same heartbeat for too long
    tr.update(frame)
    snap = tr.snapshot()
    assert snap["ok"] is False and snap["status"] == "stale", snap["status"]
    tick(lua)  # addon ticks again -> heartbeat changes -> fresh
    tr.update(render(lua))
    assert tr.snapshot()["ok"] is True


@case
def works_without_optional_apis():
    # Some clients (e.g. WoW Forever) may lack C_Map or GetPhysicalScreenSize: must still load and tick
    def game_without(names):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute((ROOT / "tests" / "wow_stub.lua").read_text())
        lua.execute(names + " = nil")
        lua.execute((ROOT / "addon" / "WoWXE" / "WoWXE.lua").read_text())
        lua.execute(BASE)
        tick(lua)
        return lua

    lua = game_without("C_Map")
    state, _ = comp.decode(render(lua))
    assert state["health"]["cur"] == 123456 and state["pos"] is None and state["map_id"] == 0, state

    lua = game_without("GetPhysicalScreenSize")  # loads and ticks; user gets a one-time chat warning
    printed = [lua.globals().PRINTED[i] for i in range(1, len(lua.globals().PRINTED) + 1)]
    assert any("GetPhysicalScreenSize is unavailable" in p for p in printed), printed


@case
def identity_and_bag_details():
    lua, s, _ = run(BASE + '''
    WORLD.name, WORLD.race, WORLD.faction = "Arthas", "Human", "Alliance"
    WORLD.bags = { [0] = { size = 6, items = {
      [1] = { quality = 1, iconFileID = 134400, stackCount = 5 },
      [2] = { quality = 3, iconFileID = 135300, stackCount = 1 } } } }
    ''')
    assert s["name"] == "Arthas" and s["race"] == "Human" and s["faction"] == "Alliance", s["name"]
    items = s["bag"]["items"]
    assert items[0] == {"q": 1, "icon": 134400, "count": 5}, items[0]
    assert items[1]["icon"] == 135300 and items[1]["q"] == 3 and items[2]["q"] == -1
    lua, s, _ = run(BASE + 'WORLD.fullName = "Max Njuu"')
    assert s["name"] == "Max Njuu", s["name"]
    lua, s, _ = run(BASE + 'WORLD.name, WORLD.race, WORLD.faction = "Zoë-Ñandú", "Blood Elf", "Horde"')
    assert s["name"] == "Zoë-Ñandú" and s["faction"] == "Horde" and s["race"] == "Blood Elf"


MAP_BASE = BASE + '''
WORLD.mapID = 37
WORLD.explored = { [37] = { { textureWidth = 256, textureHeight = 128, offsetX = 100, offsetY = 50, fileDataIDs = { 5001 } } } }
WORLD.art = { [37] = { w = 1002, h = 668, tw = 256, th = 256,
  tiles = { 1001,1002,1003,1004, 1005,1006,1007,1008, 1009,1010,1011,1012 } } }
WORLD.bags = { [0] = { size = 4, items = {
  [1] = { quality = 1, iconFileID = 2001, stackCount = 3 },
  [2] = { quality = 2, iconFileID = 2002, stackCount = 1 } } } }
'''
MAP_SETUP = MAP_BASE + "WoWXEDB = { flash = true }\n"   # flash capture is opt-in


@case
def captures_save_map_and_icons():
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        lua = new_game()
        lua.execute(MAP_SETUP)
        tracker = comp.Tracker(comp.Captures(tmp))
        seen_kinds = set()
        f = lua.globals().WoWXEFrame
        f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")
        for _ in range(60):
            f.scripts["OnUpdate"](f, 0.2)
            frame = render(lua)
            tracker.update(frame)
            seen_kinds.add(tracker.snapshot()["capture"]["kind"])
        snap = tracker.snapshot()
        assert {1, 2} <= seen_kinds, seen_kinds
        assert snap["capture"]["kind"] == 0  # nothing flashing once done
        assert snap["map"]["id"] == 37 and snap["map"]["ready"] and snap["map"]["ver"] > 0, snap["map"]
        assert [i["ready"] for i in snap["bag"]["items"][:3]] == [True, True, False]
        m = _img(pathlib.Path(tmp) / "maps" / "37.png")
        assert m.size == (804, 536), m.size  # 1002x668 scaled to fit 804x536
        arr = np.asarray(m.convert("RGB"))
        assert tuple(arr[10, 10]) == file_color(1001) and tuple(arr[500, 700]) == file_color(1012), (arr[10, 10], arr[500, 700])
        # explored-area overlay sits on top of the base map at its offset (scaled by 804/1002)
        assert tuple(arr[60, 100]) == file_color(5001), arr[60, 100]
        assert tuple(arr[300, 400]) != file_color(5001)
        ic = np.asarray(_img(pathlib.Path(tmp) / "icons" / "2001.png").convert("RGB"))
        assert ic.shape[:2] == (64, 64) and tuple(ic[30, 30]) == file_color(2001)
        # a second run with a warm cache flashes nothing new from the companion's point of view
        again = comp.Captures(tmp)
        assert again.has(2, 37) and again.has(1, 2002)


@case
def no_flash_in_combat_and_no_map_art_api():
    lua = new_game()
    lua.execute(MAP_SETUP + "WORLD.combat = true")
    f = lua.globals().WoWXEFrame
    f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")
    for _ in range(10):
        f.scripts["OnUpdate"](f, 0.2)
        state, _ = comp.decode(render(lua))
        assert state["capture"]["kind"] == 0
    lua = new_game()
    lua.execute(MAP_SETUP + "WORLD.art = {}")  # client without map art for this map
    f = lua.globals().WoWXEFrame
    f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")
    kinds = set()
    for _ in range(40):
        f.scripts["OnUpdate"](f, 0.2)
        state, _ = comp.decode(render(lua))
        kinds.add(state["capture"]["kind"])
    assert 2 not in kinds and 1 in kinds  # icons still work, no endless map retries


def _png(color, size):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


ART_FILES = {
    **{1001 + i: _png(file_color(1001 + i), (256, 256)) for i in range(12)},
    5001: _png((250, 10, 10), (256, 128)),
    2001: _png((10, 250, 10), (64, 64)),
    2002: _png((10, 10, 250), (64, 64)),
}


@case
def casc_reader_reads_files():
    import casc, casc_fixture
    for version in (2, 1):
        with tempfile.TemporaryDirectory() as tmp:
            files = {**ART_FILES, 134400: bytes(range(256)) * 3, 999999: b"x"}
            game = casc_fixture.build(tmp, files, root_version=version)
            r = casc.CascReader(game, log=lambda *_: None)
            for fid, content in files.items():
                assert r.read(fid) == content, (version, fid)
            assert r.has(2001) and not r.has(3)
            if version == 2:
                with tempfile.TemporaryDirectory() as tmp2:   # files with several stored copies, first one absent
                    g2 = casc_fixture.build(tmp2, files, decoy_first=True)
                    r2 = casc.CascReader(g2, log=lambda *_: None)
                    for fid, content in files.items():
                        assert r2.read(fid) == content, fid
            try:
                r.read(3)
                assert False, "missing id should raise"
            except casc.CascError:
                pass


@case
def map_and_icons_without_flashing():
    import casc, casc_fixture
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
        game = casc_fixture.build(tmp, ART_FILES)
        lua = new_game()
        lua.execute(MAP_BASE)                       # flash NOT enabled
        captures = comp.Captures(cache)
        art = comp.ArtBuilder(captures, str(game), reader=casc.CascReader(game, log=lambda *_: None))
        tracker = comp.Tracker(captures, art)
        f = lua.globals().WoWXEFrame
        f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")
        kinds = set()
        deadline = time.time() + 20
        for n in range(400):
            f.scripts["OnUpdate"](f, 0.2)
            tracker.update(render(lua))
            kinds.add(tracker.snapshot()["capture"]["kind"])
            snap = tracker.snapshot()
            if n > 30 and snap["map"]["ready"] and snap["bag"]["items"][0]["ready"] and snap["bag"]["items"][1]["ready"]:
                break
            time.sleep(0.01)
            assert time.time() < deadline
        assert kinds == {0}, kinds                  # nothing was ever drawn on screen
        snap = tracker.snapshot()
        assert snap["art_source"] == "ready" and snap["map"]["ver"] != 0
        m = np.asarray(_img(pathlib.Path(cache) / "maps" / "37.png").convert("RGB"))
        assert m.shape[:2] == (668, 1002), m.shape
        assert tuple(m[10, 10]) == file_color(1001) and tuple(m[600, 800]) == file_color(1012)
        assert tuple(m[60, 110]) == (250, 10, 10), m[60, 110]       # explored overlay at offset (100, 50)
        assert tuple(m[300, 600]) != (250, 10, 10)
        ic = _img(pathlib.Path(cache) / "icons" / "2001.png")
        assert ic.size == (56, 56) and ic.convert("RGB").getpixel((5, 5)) == (10, 250, 10)


@case
def map_with_missing_pictures_is_partial_not_failed():
    import casc, casc_fixture
    from PIL import Image
    files = {k: v for k, v in ART_FILES.items() if k not in (1002, 5001)}   # two pictures not in the install
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
        game = casc_fixture.build(tmp, files)
        lua = new_game()
        lua.execute(MAP_BASE)
        captures = comp.Captures(cache)
        art = comp.ArtBuilder(captures, str(game), reader=casc.CascReader(game, log=lambda *_: None))
        tracker = comp.Tracker(captures, art)
        f = lua.globals().WoWXEFrame
        f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")
        for n in range(400):
            f.scripts["OnUpdate"](f, 0.2)
            tracker.update(render(lua))
            if tracker.snapshot()["map"]["ready"]:
                break
            time.sleep(0.01)
        snap = tracker.snapshot()
        assert snap["map"]["ready"] and "pictures are not on this PC" in snap["art"]["warning"], snap["art"]
        m = np.asarray(_img(pathlib.Path(cache) / "maps" / "37.png").convert("RGB"))
        assert tuple(m[10, 10]) == file_color(1001) and tuple(m[10, 300]) == (0, 0, 0)  # blank where missing


@case
def incomplete_map_is_retried_without_errors():
    import casc, casc_fixture
    files = {k: v for k, v in ART_FILES.items() if k != 1002}
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
        game = casc_fixture.build(tmp, files)
        lua = new_game()
        lua.execute(MAP_BASE)
        captures = comp.Captures(cache)
        comp.ArtBuilder.RETRY_SECONDS, comp.ArtBuilder.POLL_SECONDS = 0.1, 0.1
        try:
            art = comp.ArtBuilder(captures, str(game), reader=casc.CascReader(game, log=lambda *_: None))
            tracker = comp.Tracker(captures, art)
            f = lua.globals().WoWXEFrame
            f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")
            deadline = time.time() + 20
            while art.stats["maps"] < 3 and time.time() < deadline:
                f.scripts["OnUpdate"](f, 0.2)
                tracker.update(render(lua))
                time.sleep(0.01)
        finally:
            comp.ArtBuilder.RETRY_SECONDS, comp.ArtBuilder.POLL_SECONDS = 30, 5
        assert art.stats["maps"] >= 3 and art.last_error is None, (art.stats, art.last_error)


@case
def manual_map_capture_beats_incomplete_game_files():
    import casc, casc_fixture
    from PIL import Image
    files = {k: v for k, v in ART_FILES.items() if k != 1002}
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
        game = casc_fixture.build(tmp, files)
        lua = new_game()
        lua.execute(MAP_BASE)                         # flash capture is off ...
        captures = comp.Captures(cache)
        art = comp.ArtBuilder(captures, str(game), reader=casc.CascReader(game, log=lambda *_: None))
        tracker = comp.Tracker(captures, art)
        f = lua.globals().WoWXEFrame
        f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")
        for _ in range(60):
            f.scripts["OnUpdate"](f, 0.2)
            tracker.update(render(lua))
        lua.globals().SlashCmdList["WOWXE"]("map")    # ... but the user asks for this map once
        kinds = set()
        for _ in range(80):
            f.scripts["OnUpdate"](f, 0.2)
            tracker.update(render(lua))
            kinds.add(tracker.snapshot()["capture"]["kind"])
        assert 2 in kinds and 37 in captures.flash_maps
        time.sleep(0.3)
        for _ in range(5):
            f.scripts["OnUpdate"](f, 0.2)
            tracker.update(render(lua))
        im = _img(pathlib.Path(cache) / "maps" / "37.png")
        assert im.size == (804, 536), im.size           # the complete on-screen capture is kept


@case
def complete_zone_map_from_wiki():
    from PIL import Image
    asked = []

    def fake(url):
        asked.append(url)
        if "WorldMap-ElwynnForest_c60.jpg" not in url:
            raise IOError("HTTP 404")
        return _png((12, 200, 40), (1024, 768))

    with tempfile.TemporaryDirectory() as cache:
        captures = comp.Captures(cache)
        wiki = comp.WikiMaps(captures, fetch=fake)
        tracker = comp.Tracker(captures, None, wiki)
        lua = new_game()
        lua.execute(BASE)                                 # zone "Elwynn Forest", map 2022 (not in the table)
        tick(lua)
        deadline = time.time() + 10
        while 2022 not in captures.fixed_maps and time.time() < deadline:
            tracker.update(render(lua))
            time.sleep(0.01)
        assert 2022 in captures.fixed_maps, asked
        assert any("Elwynn_c60" in u for u in asked) is False   # table has no entry for 2022: name-based candidates only
        im = _img(pathlib.Path(cache) / "maps" / "2022.png")
        assert im.size == (1002, 668), im.size              # 1024x768 grid cropped to the game's map area
        assert comp.Captures(cache).fixed_maps == {2022}    # remembered next time
        n = len(asked)
        for _ in range(5):
            tracker.update(render(lua))
        time.sleep(0.2)
        assert len(asked) == n                              # downloaded only once


@case
def icon_missing_from_game_files_comes_from_web():
    from PIL import Image
    class Reader:
        def read(self, i):
            raise RuntimeError("FileDataID %d: none of its 1 stored copies is in the local archives" % i)
    asked = []

    def web(url):
        asked.append(url)
        if "133884" in url:
            return _png((200, 30, 30), (64, 64))
        raise IOError("HTTP 404")

    with tempfile.TemporaryDirectory() as cache:
        captures = comp.Captures(cache)
        art = comp.ArtBuilder(captures, "x", reader=Reader(), web_fetch=web)
        art.want_icon(133884)
        art.want_icon(999)
        deadline = time.time() + 10
        while time.time() < deadline and not (captures.has(comp.KIND_ICON, 133884) and 999 in art.failed_icons):
            time.sleep(0.02)
        assert captures.has(comp.KIND_ICON, 133884), asked
        assert 999 in art.failed_icons and 133884 not in art.failed_icons
        assert "web fallback failed" in art.last_error


@case
def item_tooltips_reach_the_companion():
    with tempfile.TemporaryDirectory() as cache:
        captures = comp.Captures(cache)
        tracker = comp.Tracker(captures)
        lua = new_game()
        lua.execute(BASE + """
        WORLD.bags = { [0] = { size = 4, items = {
          [1] = { quality = 3, iconFileID = 135300, stackCount = 1, itemID = 1234,
                  tip = { { "Thunderfury, Blessed Blade", 0, 0.44, 0.87 }, { "Binds when picked up" },
                          { "Main Hand", 1, 1, 1, right = "Sword" }, { "|cff1eff00Equip: Test|r", 0.1, 1, 0 } } },
          [2] = { quality = 1, iconFileID = 134400, stackCount = 5, itemID = 777,
                  tip = { { "Linen Cloth" } } },
          [3] = { quality = 1, iconFileID = 134400, stackCount = 2, itemID = 777 } } } }
        """)
        f = tick(lua)
        for _ in range(120):
            f.scripts["OnUpdate"](f, 0.2)
            tracker.update(render(lua))
        snap = tracker.snapshot()
        items = snap["bag"]["items"]
        assert items[0]["item"] == 1234 and items[1]["item"] == 777 and items[2]["item"] == 777, items
        assert items[0]["tip"] and items[1]["tip"]
        assert snap["versions"] == {"companion": comp.VERSION, "addon": comp.VERSION}, snap["versions"]   # one release number everywhere
        tip = tracker.tips.get(1234)
        assert tip["lines"][0] == {"c": "0070dd", "l": "Thunderfury, Blessed Blade", "r": ""}, tip
        assert tip["lines"][2] == {"c": "ffffff", "l": "Main Hand", "r": "Sword"}, tip
        assert tip["lines"][3]["l"] == "Equip: Test"                    # colour codes stripped
        assert comp.Tips(cache).get(777)["lines"][0]["l"] == "Linen Cloth"   # kept on disk
        # an item that moved: the strip's icon no longer matches the old index -> no wrong tooltip
        tracker._slot_items[0] = (999, 1)
        tracker.update(render(lua))
        assert tracker.snapshot()["bag"]["items"][0]["item"] in (0, 1234)


@case
def strip_is_found_on_whichever_monitor_has_it():
    lua = new_game()
    lua.execute(BASE)
    tick(lua)
    strip = render(lua)
    h, w = strip.shape[:2]
    blank = np.zeros_like(strip)

    class Sct:
        def __init__(self, with_strip):
            self.with_strip = with_strip                       # which monitor (1-based) shows the game
            self.monitors = [{"left": 0, "top": 0, "width": 7000, "height": 1440}] + [
                {"left": i * 2000, "top": 0, "width": 1920, "height": 1080} for i in range(3)]

        def grab(self, box):
            idx = box["left"] // 2000 + 1
            img = strip if idx == self.with_strip else blank
            return np.dstack([img[:, :, 2], img[:, :, 1], img[:, :, 0], np.full(img.shape[:2], 255, np.uint8)])

    logs = []
    sct = Sct(3)
    tracker = comp.Tracker()
    f = comp.MonitorFollower(sct, tracker, None, w, h, log=logs.append)
    f.SEARCH_EVERY = 0
    for _ in range(4):
        f.step()
    assert f.cur == 3 and tracker.snapshot()["monitor"] == 3 and tracker.snapshot()["ok"], (f.cur, tracker.state)
    assert any("monitor 3" in l for l in logs), logs
    sct.with_strip = 1                                          # Windows renumbers / the game moves
    f.last_found -= 10
    for _ in range(4):
        f.step()
    assert f.cur == 1 and tracker.snapshot()["monitor"] == 1
    # a monitor given by the user is tried first
    f2 = comp.MonitorFollower(Sct(2), comp.Tracker(), 2, w, h, log=logs.append)
    assert f2.cur == 2
    f2.step()
    assert f2.tracker.snapshot()["monitor"] == 2


@case
def strip_is_found_when_the_game_runs_in_a_window():
    lua = new_game()
    lua.execute(BASE)
    tick(lua)
    strip = render(lua)
    screen = np.zeros((1600, 2400, 3), np.uint8)
    screen[700:700 + strip.shape[0], 1200:1200 + strip.shape[1]] = strip   # window far from the corner

    class Sct:
        monitors = [{"left": 0, "top": 0, "width": 2400, "height": 1600}, {"left": 0, "top": 0, "width": 2400, "height": 1600}]

        def grab(self, box):
            img = screen[box["top"]:box["top"] + box["height"], box["left"]:box["left"] + box["width"]]
            return np.dstack([img[:, :, 2], img[:, :, 1], img[:, :, 0], np.full(img.shape[:2], 255, np.uint8)])

    logs = []
    tracker = comp.Tracker()
    f = comp.MonitorFollower(Sct(), tracker, None, 900, 660, log=logs.append)
    f.SEARCH_EVERY = 0
    for _ in range(4):
        f.step()
    assert f.off == (1192, 692) and tracker.snapshot()["ok"], (f.off, tracker.state.get("status"))
    assert any("windowed" in l for l in logs), logs


def _launcher_core():
    import importlib
    sys.path.insert(0, str(ROOT / "companion"))
    sys.path.insert(0, str(ROOT))
    return importlib.import_module("launcher_core")


@case
def launcher_config_and_folder_checks():
    core = _launcher_core()
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        cfg_path = d / "config.ini"
        cfg = core.load_config(cfg_path)
        assert cfg["monitor"] == "auto" and core.flag(cfg, "web_icons") and not core.flag(cfg, "autostart")
        cfg.update(game_dir=r"C:\Games\World of Warcraft\_forever_", monitor="3", web_icons="0")
        core.save_config(cfg, cfg_path)
        again = core.load_config(cfg_path)
        assert again["game_dir"] == cfg["game_dir"] and again["monitor"] == "3" and not core.flag(again, "web_icons")
        game = d / "World of Warcraft"
        (game / "_forever_" / "Data").mkdir(parents=True)
        (game / "_forever_" / ".build.info").write_text("x")
        assert core.validate_game_dir(str(game / "_forever_"))[0]
        ok, msg = core.validate_game_dir(str(game))
        assert not ok and "_forever_" in msg, msg
        # Forever layout: .build.info and Data one level above the _forever_ folder
        wow = d / "WoW2"
        (wow / "Data").mkdir(parents=True)
        (wow / ".build.info").write_text("x")
        (wow / "_forever_" / "Interface").mkdir(parents=True)
        assert core.validate_game_dir(str(wow / "_forever_"))[0]
        alt = d / "WoW3"                                 # own Data folder, .build.info one level up
        (alt / "_forever_" / "Data" / "data").mkdir(parents=True)
        (alt / ".build.info").write_text("x")
        assert core.validate_game_dir(str(alt / "_forever_"))[0]
        assert not core.validate_game_dir("")[0] and not core.validate_game_dir(str(d / "nope"))[0]
        cmd = core.build_command(cfg, python="py", script="c.py")
        assert cmd[:3] == ["py", "-u", "c.py"] and "--no-web-icons" in cmd and "--no-wiki-maps" not in cmd
        assert cmd[cmd.index("--monitor") + 1] == "3"
        exe = core.build_command(cfg, python="WoWXE.exe", frozen=True)
        assert exe[:2] == ["WoWXE.exe", "--companion"] and "--cache" in exe, exe
        auto = core.build_command(dict(cfg, monitor="auto"), python="py", script="c.py")
        assert auto[auto.index("--monitor") + 1] == "auto"


@case
def launcher_finds_battlenet_and_watches_the_game():
    core = _launcher_core()
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        bnet = d / "Games" / "Battle.net"
        bnet.mkdir(parents=True)
        (bnet / "Battle.net Launcher.exe").write_bytes(b"MZ")
        assert core.find_battlenet({"bnet": ""}, extra_dirs=[d / "nothing", bnet]) == bnet / "Battle.net Launcher.exe"
        assert core.find_battlenet({"bnet": str(bnet / "Battle.net Launcher.exe")}) == bnet / "Battle.net Launcher.exe"
    assert core.launch_args("B.exe", "WoWF") == ["B.exe", "--exec=launch WoWF"]
    assert core.DEFAULTS["game_code"] == "WoWF"
    w = core.GameWatch()
    assert not w.update(False, 0)                      # never seen: nothing to do
    assert not w.update(True, 1)                       # game running
    assert not w.update(False, 2) and not w.update(False, 5)   # closed briefly (e.g. restarting)
    assert not w.update(True, 6)                       # back
    assert not w.update(False, 7)
    assert w.update(False, 17.5)                       # gone for 10 s: stop the companion
    assert not w.update(False, 30)                     # only once


@case
def launcher_installs_and_checks_the_addon():
    core = _launcher_core()
    assert (core.ADDON_SRC / "WoWXE.toc").exists() and (core.ADDON_SRC / "WoWXE.lua").exists()
    ours = core.toc_version(core.ADDON_SRC / "WoWXE.toc")
    assert ours == comp.VERSION, (ours, comp.VERSION)           # same release number everywhere
    with tempfile.TemporaryDirectory() as d:
        game = pathlib.Path(d) / "_forever_"
        game.mkdir()
        assert core.addon_status(str(game))[0] == "missing"
        target = core.install_addon(str(game))
        assert target == game / "Interface" / "AddOns" / "WoWXE" and (target / "WoWXE.lua").exists()
        assert core.addon_status(str(game))[0] == "ok"
        (target / "WoWXE.toc").write_text("## Version: 0.1\n")
        assert core.addon_status(str(game))[0] == "old"
        core.install_addon(str(game))                              # replaces the old copy
        assert core.addon_status(str(game))[0] == "ok"
    assert core.addon_status("")[0] == "nogame"


@case
def launcher_starts_and_stops_companion_and_reads_status():
    import http.server
    import json
    import threading
    core = _launcher_core()
    comp = core.Companion()
    code = "import time\nprint('hello from companion', flush=True)\nwhile True: time.sleep(0.2)"
    comp.start([sys.executable, "-c", code])
    deadline = time.time() + 10
    while time.time() < deadline and "hello from companion" not in comp.log_text():
        time.sleep(0.05)
    assert comp.running() and "hello from companion" in comp.log_text()
    comp.stop()
    assert not comp.running()
    time.sleep(0.5)
    assert "companion stopped" in comp.log_text()

    payload = {"ok": True, "status": "ok", "monitor": 2, "versions": {"companion": "1.0.0", "addon": "1.0.0"}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    assert core.port_in_use(port)
    level, head, detail = core.describe(core.poll_state(port), True)
    assert (level, head) == ("ok", "Connected") and "monitor 2" in detail and "companion 1.0.0" in detail
    assert core.describe(None, False)[0] == "idle" and core.describe(None, True)[1] == "Starting..."
    assert core.describe({"ok": False, "status": "strip_not_found"}, True)[0] == "warn"
    assert core.describe({"ok": False, "status": "protocol_mismatch"}, True)[0] == "bad"
    srv.shutdown()
    srv.server_close()
    assert core.poll_state(port) is None


@case
def launcher_window_code_runs_with_a_fake_tk():
    """tkinter is not installed in the build sandbox: run the window code against a do-nothing stand-in
    so typos and wrong attribute names show up here instead of on the PC."""
    import importlib.machinery
    import importlib.util
    import types

    class Any:
        def __init__(self, *a, **k):
            self._v = k.get("value", "")

        def __getattr__(self, name):
            return lambda *a, **k: Any()

        def get(self):
            return self._v

        def set(self, v):
            self._v = v

    tk = types.ModuleType("tkinter")
    for n in ("Tk", "Canvas", "Text", "StringVar"):
        setattr(tk, n, type(n, (Any,), {}))
    tk.BooleanVar = type("BooleanVar", (Any,), {})
    ttk = types.ModuleType("tkinter.ttk")
    for n in ("Frame", "Label", "Entry", "Button", "Combobox", "Checkbutton"):
        setattr(ttk, n, type(n, (Any,), {}))
    tk.ttk = ttk
    tk.filedialog = types.ModuleType("tkinter.filedialog")
    tk.messagebox = types.ModuleType("tkinter.messagebox")
    names = ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox")
    saved = {k: sys.modules.get(k) for k in names}
    sys.modules.update(dict(zip(names, (tk, ttk, tk.filedialog, tk.messagebox))))
    try:
        sys.path.insert(0, str(ROOT / "companion"))
        sys.path.insert(0, str(ROOT))
        loader = importlib.machinery.SourceFileLoader("wowhud_launcher", str(ROOT / "WoWXE.pyw"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wowhud_launcher", loader))
        loader.exec_module(mod)
        app = mod.Launcher()
        app.cfg["port"] = "1"
        app.tick()
        app.check_dir()
        app.on_unmap(types.SimpleNamespace(widget=app))
        app.offer_addon()   # minimize -> tray path (no-op with the fake)
        app.on_close_checked = True
        app.comp.stop()
        app.poller.stop()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


WORLD_LEVEL = 80


@case
def character_stats_gear_and_professions_reach_the_companion():
    with tempfile.TemporaryDirectory() as cache:
        captures = comp.Captures(cache)
        tracker = comp.Tracker(captures)
        lua = new_game()
        lua.execute(BASE + """
        WORLD.stats = { 51, 77, 66, 40, 58 }
        WORLD.armor, WORLD.ap, WORLD.crit, WORLD.ilvl, WORLD.maxLevel = 812, 144, 7.25, 23.4, true
        WORLD.mh, WORLD.hit, WORLD.spdmg, WORLD.spheal, WORLD.def, WORLD.dodge = { 22.6, 31.2 }, 2, 6, 6, 85, 7.3
        WORLD.gear = {
          [1] = { id = 4001, icon = 133071, q = 3, ilvl = 25, tip = { { "Helm of Tests", 0, 0.44, 0.87 }, { "Head", right = "Leather" } } },
          [16] = { id = 4002, icon = 135145, q = 2, ilvl = 21, tip = { { "Staff of Tests", 0.12, 1, 0 } } },
        }
        WORLD.profs = { { "Herbalism", 120, 150, 136246 }, { "Alchemy", 95, 150, 136240 }, nil, { "Fishing", 40, 75, 136245 }, { "Cooking", 1, 75, 133971 } }
        """)
        f = tick(lua)
        for _ in range(160):
            f.scripts["OnUpdate"](f, 0.2)
            tracker.update(render(lua))
        snap = tracker.snapshot()
        ch = snap["char"]
        assert ch["maxlevel"] is True and ch["str"] == 51 and ch["spi"] == 58 and ch["armor"] == 812, ch
        assert ch["ap"] == 144 and abs(ch["crit"] - 7.2) < 0.11 and abs(ch["ilvl"] - 23.4) < 0.01, ch
        assert (ch["mh_min"], ch["mh_max"], ch["spdmg"], ch["spheal"], ch["def"], ch["defmax"]) == (22, 32, 6, 6, 85, 5 * WORLD_LEVEL), ch
        assert ch["speed"] == 102 and ch["hit"] == 2.0 and ch["dodge"] == 7.3 and "parry" not in ch, ch
        assert "int:+" in ch["buffs"], ch
        gear = {g["slot"]: g for g in snap["gear"]}
        assert gear[1]["item"] == 4001 and gear[1]["icon"] == 133071 and gear[1]["q"] == 3 and gear[1]["ilvl"] == 25, gear
        assert gear[16]["item"] == 4002 and gear[1]["tip"] and gear[16]["tip"], gear
        assert tracker.tips.get(4001)["lines"][1] == {"c": "ffffff", "l": "Head", "r": "Leather"}
        prof = snap["prof"]
        assert [(p["kind"], p["name"], p["cur"], p["max"]) for p in prof] == [
            ("primary", "Herbalism", 120, 150), ("primary", "Alchemy", 95, 150),
            ("secondary", "Fishing", 40, 75), ("secondary", "Cooking", 1, 75)], prof
        assert prof[0]["icon"] == 136246


@case
def exploration_update_resends_map():
    import casc, casc_fixture
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
        game = casc_fixture.build(tmp, {**ART_FILES, 5002: _png((10, 10, 250), (256, 256))})
        lua = new_game()
        lua.execute(MAP_BASE)
        captures = comp.Captures(cache)
        art = comp.ArtBuilder(captures, str(game), reader=casc.CascReader(game, log=lambda *_: None))
        tracker = comp.Tracker(captures, art)
        f = lua.globals().WoWXEFrame
        f.scripts["OnEvent"](f, "PLAYER_ENTERING_WORLD")

        def pump(until):
            for n in range(400):
                f.scripts["OnUpdate"](f, 0.2)
                tracker.update(render(lua))
                if until(tracker.snapshot()):
                    return
                time.sleep(0.01)
            raise AssertionError("timed out")

        pump(lambda s: s["map"]["ready"])
        v1 = tracker.snapshot()["map"]["ver"]
        lua.execute("""WORLD.explored[37][2] = { textureWidth = 256, textureHeight = 256, offsetX = 600, offsetY = 300, fileDataIDs = { 5002 } }""")
        f.scripts["OnEvent"](f, "MAP_EXPLORATION_UPDATED")
        pump(lambda s: s["map"]["ver"] != v1)
        m = np.asarray(_img(pathlib.Path(cache) / "maps" / "37.png").convert("RGB"))
        assert tuple(m[350, 650]) == (10, 10, 250), m[350, 650]


@case
def decodes_when_cell_edges_are_snapped_by_a_pixel():
    for seed in range(20):
        lua = new_game()
        lua.execute(BASE + 'WORLD.name, WORLD.race, WORLD.zone = "Jaina Proudmoore", "Night Elf", "Teldrassil"')
        tick(lua)
        state, _ = comp.decode(render(lua, seed=seed, jitter=True))
        assert state is not None, seed
        assert state["name"] == "Jaina Proudmoore" and state["race"] == "Night Elf" and state["zone"] == "Teldrassil", (seed, state["name"], state["race"], state["zone"])
        assert state["map_id"] == 2022 and state["level"] == 80 and state["bag"]["total"] == 36, seed


@case
def slash_test_never_does_math_on_secrets():
    lua, _, _ = run(BASE + "WORLD.health = Secret(1)")
    lua.globals().SlashCmdList["WOWXE"]("test")
    printed = [lua.globals().PRINTED[i] for i in range(1, len(lua.globals().PRINTED) + 1)]
    assert any("health secret:" in p and "true" in p for p in printed), printed


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "") == "wrapper":
            fn()
    ok = sum(1 for _, passed, _ in results if passed)
    for name, passed, detail in results:
        print(("PASS " if passed else "FAIL ") + name)
        if not passed:
            print(detail)
    print(f"\n{ok}/{len(results)} passed")
    sys.exit(0 if ok == len(results) else 1)
