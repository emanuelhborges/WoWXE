"""
WoWXE launcher: a small window that starts/stops the companion in the background.
Settings are saved in config.ini next to this file. Minimizing hides the window to the tray
(if pystray is installed and "Minimize to tray" is on); closing it or Quit stops the companion.
"""
import atexit
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import launcher_core as core

try:                                   # optional: pip install pystray (listed in requirements.txt)
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None

COLORS = {"idle": "#8a8f9c", "ok": "#2ecc71", "warn": "#f1c40f", "bad": "#e74c3c"}


def tray_image(level):
    """The WoWXE medallion with a small status dot (grey stopped / yellow waiting / green connected / red problem)."""
    from PIL import Image, ImageDraw
    try:
        im = Image.open(core.ICON_PNG).convert("RGBA").resize((64, 64), Image.LANCZOS)
    except Exception:
        im = Image.new("RGBA", (64, 64), (23, 26, 34, 255))
    d = ImageDraw.Draw(im)
    d.ellipse((38, 38, 62, 62), fill=COLORS.get(level, COLORS["idle"]), outline=(10, 12, 16, 255), width=3)
    return im


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"WoWXE  v{core.LAUNCHER_VERSION}")
        self.resizable(False, False)
        try:
            if sys.platform == "win32":
                self.iconbitmap(str(core.ICON_ICO))
            else:
                self._icon = tk.PhotoImage(file=str(core.ICON_PNG))
                self.iconphoto(True, self._icon)
        except Exception:
            pass
        self.cfg = core.load_config()
        self.comp = core.Companion()
        self.poller = core.StatusPoller(lambda: self.cfg["port"])
        self.poller.start()
        atexit.register(self.comp.stop)       # never leave the companion running behind a crashed launcher
        self.tray = None
        self.tray_level = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Unmap>", self.on_unmap)
        self.after(500, self.tick)
        self.after(900, self.offer_addon)
        if core.flag(self.cfg, "autostart"):
            self.after(300, self.start)

    # ---------------------------------------------------------- window
    def _build(self):
        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.grid(sticky="nsew")

        ttk.Label(frm, text="WoW folder (_forever_)").grid(row=0, column=0, sticky="w", **pad)
        self.var_dir = tk.StringVar(value=self.cfg["game_dir"])
        ttk.Entry(frm, textvariable=self.var_dir, width=52).grid(row=1, column=0, columnspan=2, sticky="we", padx=10)
        row = ttk.Frame(frm)
        row.grid(row=1, column=2, sticky="e", padx=(0, 10))
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left")
        ttk.Button(row, text="Find", command=self.find).pack(side="left", padx=(4, 0))
        self.lbl_dir = ttk.Label(frm, text="")
        self.lbl_dir.grid(row=2, column=0, columnspan=3, sticky="w", padx=10)
        self.var_dir.trace_add("write", lambda *a: self.check_dir())

        arow = ttk.Frame(frm)
        arow.grid(row=2, column=2, sticky="e", padx=(0, 10))
        self.btn_addon = ttk.Button(arow, text="Install addon", command=self.install_addon)
        self.btn_addon.pack()
        self.lbl_addon = ttk.Label(frm, text="")
        self.lbl_addon.grid(row=9, column=0, columnspan=3, sticky="w", padx=10)

        ttk.Label(frm, text="Monitor").grid(row=3, column=0, sticky="w", **pad)
        self.var_mon = tk.StringVar(value=self.cfg["monitor"] if self.cfg["monitor"].isdigit() else "Auto")
        ttk.Combobox(frm, textvariable=self.var_mon, width=8, state="readonly",
                     values=["Auto", "1", "2", "3", "4", "5"]).grid(row=3, column=1, sticky="w", padx=10)

        self.var_icons = tk.BooleanVar(value=core.flag(self.cfg, "web_icons"))
        self.var_maps = tk.BooleanVar(value=core.flag(self.cfg, "wiki_maps"))
        self.var_tray = tk.BooleanVar(value=core.flag(self.cfg, "tray"))
        self.var_auto = tk.BooleanVar(value=core.flag(self.cfg, "autostart"))
        opts = ttk.Frame(frm)
        opts.grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 0))
        ttk.Checkbutton(opts, text="Download missing item icons", variable=self.var_icons).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Download complete zone maps", variable=self.var_maps).grid(row=1, column=0, sticky="w")
        tray_text = "Minimize to tray" + ("" if pystray else "  (needs: pip install pystray)")
        ttk.Checkbutton(opts, text=tray_text, variable=self.var_tray).grid(row=0, column=1, sticky="w", padx=(24, 0))
        ttk.Checkbutton(opts, text="Start the companion when this opens", variable=self.var_auto).grid(
            row=1, column=1, sticky="w", padx=(24, 0))

        bar = ttk.Frame(frm)
        bar.grid(row=5, column=0, columnspan=3, sticky="we", padx=10, pady=(12, 4))
        self.btn = ttk.Button(bar, text="Start", width=14, command=self.toggle)
        self.btn.pack(side="left")
        ttk.Button(bar, text="Quit", width=8, command=self.quit_app).pack(side="right")
        self.dot = tk.Canvas(bar, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="left", padx=(14, 6))
        self.dot_id = self.dot.create_oval(2, 2, 14, 14, fill=COLORS["idle"], outline="")
        self.lbl_status = ttk.Label(bar, text="Stopped", font=("Segoe UI", 10, "bold"))
        self.lbl_status.pack(side="left")
        self.lbl_detail = ttk.Label(frm, text=f"launcher {core.LAUNCHER_VERSION}", foreground="#666", justify="left")
        self.lbl_detail.grid(row=6, column=0, columnspan=3, sticky="w", padx=10)

        self.var_log = tk.BooleanVar(value=core.flag(self.cfg, "show_log"))
        ttk.Checkbutton(frm, text="Show log", variable=self.var_log, command=self.toggle_log).grid(
            row=7, column=0, sticky="w", padx=10, pady=(8, 0))
        self.log = tk.Text(frm, width=76, height=10, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.grid(row=8, column=0, columnspan=3, padx=10, pady=4)
        self.toggle_log()
        self.check_dir()

    def toggle_log(self):
        if self.var_log.get():
            self.log.grid()
        else:
            self.log.grid_remove()

    # ---------------------------------------------------------- settings
    def check_dir(self):
        ok, msg = core.validate_game_dir(self.var_dir.get())
        self.lbl_dir.config(text=("OK - " if ok else "") + msg, foreground="#1a7f37" if ok else "#b3261e")
        if hasattr(self, "lbl_addon"):
            state, text = core.addon_status(self.var_dir.get() if ok else "")
            self.lbl_addon.config(text=text, foreground="#1a7f37" if state == "ok" else "#8a6d00")
            self.btn_addon.config(state="normal" if ok else "disabled",
                                  text="Update addon" if state == "old" else "Install addon")
        return ok

    def browse(self):
        d = filedialog.askdirectory(title="Choose the WoW _forever_ folder", initialdir=self.var_dir.get() or None)
        if d:
            self.var_dir.set(d.replace("/", "\\") if sys.platform == "win32" else d)

    def find(self):
        found = core.guess_game_dirs()
        if found:
            self.var_dir.set(found[0])
        else:
            messagebox.showinfo("WoWXE", "No WoW Forever install found in the usual places. Use Browse...")

    def offer_addon(self):
        """Plug and play: when the WoW folder is known and the addon is missing or older, offer to install it."""
        if not self.check_dir():
            return
        state, text = core.addon_status(self.var_dir.get())
        if state in ("missing", "old"):
            verb = "Install" if state == "missing" else "Update"
            if messagebox.askyesno("WoWXE", f"{text}.\n\n{verb} the WoWXE addon in your WoW folder now?"):
                self.install_addon()

    def install_addon(self):
        if not self.check_dir():
            return
        try:
            target = core.install_addon(self.var_dir.get())
        except Exception as e:
            messagebox.showerror("WoWXE", f"Could not copy the addon:\n{e}")
            return
        self.check_dir()
        messagebox.showinfo("WoWXE", f"Addon copied to:\n{target}\n\nIf WoW is running, type /reload in game.")

    def collect(self):
        mon = self.var_mon.get()
        self.cfg.update(game_dir=self.var_dir.get().strip().strip('"'), monitor=mon.lower() if mon.isdigit() else "auto",
                        web_icons="1" if self.var_icons.get() else "0", wiki_maps="1" if self.var_maps.get() else "0",
                        tray="1" if self.var_tray.get() else "0", autostart="1" if self.var_auto.get() else "0",
                        show_log="1" if self.var_log.get() else "0")
        core.save_config(self.cfg)

    # ---------------------------------------------------------- companion
    def toggle(self):
        if self.comp.running():
            self.stop()
        else:
            self.start()

    def start(self):
        self.collect()
        if not self.check_dir():
            if not messagebox.askyesno("WoWXE", "The WoW folder is not valid, so map and item pictures cannot be "
                                                   "read from the game files. Start anyway?"):
                return
            self.cfg["game_dir"] = ""
        if core.port_in_use(self.cfg["port"]):
            messagebox.showwarning("WoWXE", f"Port {self.cfg['port']} is already in use - is another companion "
                                              "running? Close it first (Stop WoWXE.bat).")
            return
        self.comp.start(core.build_command(self.cfg))

    def stop(self):
        self.comp.stop()

    # ---------------------------------------------------------- refresh
    def tick(self):
        running = self.comp.running()
        level, head, detail = core.describe(self.poller.state if running else None, running)
        self.btn.config(text="Stop" if running else "Start")
        self.lbl_status.config(text=head)
        self.lbl_detail.config(text=(detail + "\n" if detail else "") + f"launcher {core.LAUNCHER_VERSION}")
        self.dot.itemconfig(self.dot_id, fill=COLORS[level])
        if self.var_log.get():
            text = self.comp.log_text()
            if text != getattr(self, "_shown", None):
                self._shown = text
                self.log.config(state="normal")
                self.log.delete("1.0", "end")
                self.log.insert("end", text)
                self.log.see("end")
                self.log.config(state="disabled")
        if self.tray is not None and level != self.tray_level:
            self.tray_level = level
            self.tray.icon = tray_image(level)
            self.tray.title = "WoWXE: " + head
        self.after(700, self.tick)

    # ---------------------------------------------------------- tray / closing
    def on_close(self):
        """The X button closes the launcher and stops the companion."""
        self.collect()
        self.quit_app()

    def on_unmap(self, event):
        """Minimizing hides the window to the tray (when pystray is installed and the option is on)."""
        if event.widget is self and pystray and self.var_tray.get() and self.state() == "iconic":
            self.after(0, self.to_tray)

    def to_tray(self):
        self.withdraw()
        if self.tray is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda: self.after(0, self.from_tray), default=True),
            pystray.MenuItem(lambda item: "Stop companion" if self.comp.running() else "Start companion",
                             lambda: self.after(0, self.toggle)),
            pystray.MenuItem("Quit (stops the companion)", lambda: self.after(0, self.quit_app)),
        )
        self.tray = pystray.Icon("wowxe", tray_image("idle"), "WoWXE", menu)
        self.tray_level = "idle"
        self.tray.run_detached()

    def from_tray(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        if self.tray is not None:
            self.tray.stop()
            self.tray = None

    def quit_app(self):
        try:
            self.comp.stop()
            self.poller.stop()
            if self.tray is not None:
                self.tray.stop()
        finally:
            self.destroy()


def run_companion():
    """WoWXE.exe --companion ... runs the companion itself (the exe has no separate Python)."""
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--companion"]
    sys.path.insert(0, str(core.HERE / "companion"))
    import wowxe_companion
    sys.exit(wowxe_companion.main())


if __name__ == "__main__":
    if "--companion" in sys.argv:
        run_companion()
    else:
        Launcher().mainloop()
