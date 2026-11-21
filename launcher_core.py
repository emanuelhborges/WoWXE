"""
Logic of the WoWXE launcher (no window code, so it can be tested without a screen):
config file, game-folder checks, starting/stopping the companion, reading its status.
"""
import configparser
import json
import os
import subprocess
import sys
import threading
import urllib.request
from collections import deque
from pathlib import Path

LAUNCHER_VERSION = "1.3.0"   # release number shared by addon, companion, widget and launcher
FROZEN = bool(getattr(sys, "frozen", False))      # running as WoWXE.exe (PyInstaller)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache" if FROZEN else HERE / "companion" / "cache"
COMPANION = HERE / "companion" / "wowxe_companion.py"
ADDON_SRC = HERE / "addon" / "WoWXE"
ICON_PNG = HERE / "assets" / "wowxe.png"
ICON_ICO = HERE / "assets" / "wowxe.ico"
CONFIG = HERE / "config.ini"

DEFAULTS = {
    "game_dir": "",
    "monitor": "auto",          # "auto" or a monitor number to try first
    "port": "8765",
    "web_icons": "1",           # download item icons the game files do not have
    "wiki_maps": "1",           # download complete zone maps from the Warcraft wiki
    "tray": "1",                # minimizing hides the window to the tray
    "autostart": "0",           # start the companion as soon as the launcher opens
    "show_log": "0",
}


# ------------------------------------------------------------------ config
def load_config(path=CONFIG):
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    cfg = dict(DEFAULTS)
    if cp.has_section("wowxe"):
        cfg.update({k: v for k, v in cp["wowxe"].items() if k in DEFAULTS})
    if not cfg["game_dir"]:
        found = guess_game_dirs()
        if found:
            cfg["game_dir"] = found[0]
    return cfg


def save_config(cfg, path=CONFIG):
    cp = configparser.ConfigParser()
    cp["wowxe"] = {k: str(cfg.get(k, DEFAULTS[k])) for k in DEFAULTS}
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        cp.write(f)
    os.replace(tmp, path)


def flag(cfg, key):
    return str(cfg.get(key, DEFAULTS[key])).strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ game folder
def _build_info_near(p):
    """True when .build.info and Data are in this folder or one of its parents (Forever keeps them one level up)."""
    for base in [p, *list(p.parents)[:3]]:
        try:
            if (base / ".build.info").exists() and (base / "Data").is_dir():
                return True
        except OSError:
            pass
    for base in [p, *list(p.parents)[:3]]:       # same fallback as the CASC reader: own Data, .build.info one level up
        try:
            if (base / "Data" / "data").is_dir() and ((base / ".build.info").exists() or (base.parent / ".build.info").exists()):
                return True
        except OSError:
            pass
    return False


def validate_game_dir(path):
    """(ok, message) for the _forever_ folder (the one with Interface\\AddOns); .build.info and Data may sit next to it or above."""
    if not path or not str(path).strip():
        return False, "Choose the WoW folder (the _forever_ folder inside World of Warcraft)"
    p = Path(str(path).strip().strip('"'))
    if not p.is_dir():
        return False, "That folder does not exist"
    if _build_info_near(p):
        return True, "WoW game files found"
    for sub in ("_forever_", "_retail_", "_classic_"):
        if (p / sub).is_dir() and _build_info_near(p / sub):
            return False, f"Pick the inner folder: {p / sub}"
    return False, "No .build.info and Data folder found here or above - choose the _forever_ folder inside World of Warcraft"


def guess_game_dirs():
    roots = []
    for drive in "CDEFG":
        for base in (r"Games\World of Warcraft", r"World of Warcraft", r"Program Files (x86)\World of Warcraft",
                     r"Program Files\World of Warcraft", r"Games\WoW"):
            roots.append(Path(f"{drive}:\\") / base)
    out = []
    for r in roots:
        for sub in ("_forever_",):
            try:
                if _build_info_near(r / sub):
                    out.append(str(r / sub))
            except OSError:
                pass
    return out


# ------------------------------------------------------------------ addon install
def toc_version(path):
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("## version:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def addon_target(game_dir):
    return Path(str(game_dir).strip().strip('"')) / "Interface" / "AddOns" / "WoWXE"


def addon_status(game_dir, src=ADDON_SRC):
    """(state, text): state is 'missing', 'old', 'ok' or 'nogame'."""
    ours = toc_version(Path(src) / "WoWXE.toc")
    if not game_dir or not Path(str(game_dir).strip().strip('"')).is_dir():
        return "nogame", f"Addon v{ours or '?'} ready to install"
    theirs = toc_version(addon_target(game_dir) / "WoWXE.toc")
    if theirs is None:
        return "missing", f"Addon not installed (this package has v{ours})"
    if theirs != ours:
        return "old", f"Addon v{theirs} installed - update to v{ours}"
    return "ok", f"Addon v{theirs} installed"


def install_addon(game_dir, src=ADDON_SRC):
    """Copies the bundled addon into Interface/AddOns/WoWXE (replacing an older copy). Returns the target path."""
    import shutil
    target = addon_target(game_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src, target)
    return target


# ------------------------------------------------------------------ companion process
def python_for_child():
    """The console python (so stdout can be read), run without a window."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.with_name("python.exe")).exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def build_command(cfg, python=None, script=COMPANION, frozen=None):
    frozen = FROZEN if frozen is None else frozen
    if frozen:   # the exe starts a second copy of itself in companion mode
        cmd = [python or sys.executable, "--companion", "--port", str(cfg["port"])]
    else:
        cmd = [python or python_for_child(), "-u", str(script), "--port", str(cfg["port"])]
    cmd += ["--cache", str(CACHE_DIR)]
    mon = str(cfg.get("monitor", "auto")).strip().lower()
    cmd += ["--monitor", mon if mon.isdigit() else "auto"]
    if cfg.get("game_dir"):
        cmd += ["--game-dir", str(cfg["game_dir"]).strip().strip('"')]
    if not flag(cfg, "web_icons"):
        cmd.append("--no-web-icons")
    if not flag(cfg, "wiki_maps"):
        cmd.append("--no-wiki-maps")
    return cmd


class Companion:
    """Runs wowxe_companion.py in the background and keeps its console output."""

    def __init__(self, log_lines=300):
        self.proc = None
        self.log = deque(maxlen=log_lines)
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self):
        return None if self.proc is None else self.proc.poll()

    def start(self, cmd):
        if self.running():
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.log.append("> " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=flags, env=env, cwd=str(HERE))
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                with self.lock:
                    self.log.append(line)
        with self.lock:
            self.log.append(f"[companion stopped, exit code {proc.wait()}]")

    def stop(self, timeout=4.0):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(2)

    def log_text(self):
        with self.lock:
            return "\n".join(self.log)


# ------------------------------------------------------------------ status
def poll_state(port, timeout=0.8):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state.json", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def port_in_use(port):
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def describe(state, running):
    """(level, headline, details) for the status area. level: idle / warn / bad / ok."""
    if not running:
        return "idle", "Stopped", ""
    if not state:
        return "warn", "Starting...", ""
    status = state.get("status")
    v = state.get("versions") or {}
    details = "companion {}  |  addon {}".format(v.get("companion") or "?", v.get("addon") or "?")
    if state.get("monitor"):
        details += "  |  monitor {}".format(state["monitor"])
    if status == "protocol_mismatch":
        return "bad", "Addon and companion differ - update both", details
    if status == "strip_not_found":
        return "warn", "Waiting for WoW (strip not found on any monitor)", details
    if status == "stale":
        return "warn", "WoW is paused or loading", details
    if state.get("ok"):
        notes = state.get("art") or {}
        extra = notes.get("last_error") or notes.get("warning") or notes.get("error")
        return "ok", "Connected", details + (f"\n{extra}" if extra else "")
    return "warn", "Starting...", details


class StatusPoller(threading.Thread):
    """Reads /state.json in the background so the window never waits for the network."""

    def __init__(self, get_port, period=1.0):
        super().__init__(daemon=True)
        self.get_port, self.period = get_port, period
        self.state = None
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            self.state = poll_state(self.get_port())
            self._halt.wait(self.period)

    def stop(self):
        self._halt.set()
