from __future__ import annotations

import json
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import psutil

from . import config_io, constants, discord_webhook, install_ops, players, process_ops, settings, steam, updater
from .backup import backup_saves_now, find_latest_backup
from .paths import ServerPaths
from .settings import ClientInstallSettings, ManagerSettings
from .ui_theme import apply_dark_theme, tk_button


def _app_package_dir() -> Path:
    return Path(__file__).resolve().parent


def _bootstrap_client_settings_path() -> Path:
    # Keep a launcher-level copy so startup can remember server root
    # even before we know which server directory to bind paths to.
    return _app_package_dir().parent / "windrose_client_settings.json"


def _isolated_dedicated_server_env(working_dir: str) -> dict[str, str]:
    """
    A deliberately minimal environment for the dedicated server child process.

    Inheriting the full parent environment can let `WindroseServer-Win64-Shipping.exe` resolve
    MSVC runtimes (e.g. VCRUNTIME140.dll) from the Server Manager's PyInstaller `_internal`
    directory (via PATH / DLL search), which then locks those files and breaks Server Manager
    self-updates while the server is running.
    """
    base = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "SystemDrive", "TEMP", "TMP", "USERPROFILE", "COMPUTERNAME", "PATHEXT", "ComSpec", "PSModulePath", "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE")}
    # Ensure critical Win32 directory vars exist.
    system_root = base.get("SystemRoot") or os.environ.get("SystemRoot", r"C:\Windows")
    windir = base.get("WINDIR") or os.environ.get("WINDIR", system_root)
    base["SystemRoot"] = system_root
    base["WINDIR"] = windir
    # Minimal PATH: shipping exe directory + system dirs (enough for Side-by-side CRT + normal Win32 load).
    sys32 = str(Path(system_root) / "System32")
    sys_wow = str(Path(system_root) / "SysWOW64")
    path_parts = [working_dir, sys32, sys_wow, str(Path(system_root) / "")]
    # De-dupe while preserving order.
    seen: set[str] = set()
    path_clean: list[str] = []
    for p in path_parts:
        np = os.path.normcase(os.path.normpath(p))
        if np in seen:
            continue
        seen.add(np)
        path_clean.append(p)
    base["PATH"] = os.pathsep.join(path_clean)
    return base


def _reset_windows_dll_directory_for_child_launch() -> tuple[bool, object | None]:
    """
    Temporarily reset the Win32 DLL directory to default for child process launch.

    PyInstaller can set a custom DLL directory (typically _MEIPASS/_internal) in the manager
    process. If that state leaks into child load behavior, the dedicated server may bind
    VCRUNTIME140.dll from the manager folder and keep it locked.
    """
    if os.name != "nt":
        return False, None
    try:
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return False, None
    try:
        meipass = getattr(sys, "_MEIPASS", None)
        ok = bool(kernel32.SetDllDirectoryW(None))
        return ok, meipass
    except Exception:
        return False, None


def _restore_windows_dll_directory_after_child_launch(meipass: object | None) -> None:
    if os.name != "nt":
        return
    if not meipass:
        return
    try:
        ctypes.windll.kernel32.SetDllDirectoryW(str(meipass))
    except Exception:
        pass


class HoverToolTip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if not self.text.strip():
            return
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.geometry(f"+{x}+{y}")
        lbl = tk.Label(
            self.tip,
            text=self.text,
            bg="#1A2A3A",
            fg="#C0CDD8",
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=5,
            wraplength=520,
            justify=tk.LEFT,
            font=(None, 10),
        )
        lbl.pack()

    def _hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


class WindroseServerManagerApp:
    def __init__(self, root: tk.Tk, initial_server_dir: Path) -> None:
        self.root = root
        self.paths = ServerPaths(initial_server_dir)
        self.client = ClientInstallSettings()
        self.mgr = ManagerSettings()
        self.c = constants.COLORS

        self.server_popen: subprocess.Popen | None = None
        self.start_time: datetime | None = None
        self.prev_cpu_time: float | None = None
        self.prev_cpu_check: datetime | None = None
        self.max_players = 10
        self.log_position = 0
        self.log_buffer: list[str] = []
        self.log_filter = "All"
        self.online_players: set[str] = set()
        self.account_to_player: dict[str, str] = {}
        self.last_player_snapshot = ""
        self._player_total_seconds: dict[str, float] = {}
        self._player_session_start_totals: dict[str, datetime] = {}
        self._hourly_online_seconds: list[float] = [0.0] * 24
        self._player_session_start_hourly: dict[str, datetime] = {}
        self._insights_last_updated_ts: str | None = None
        self._active_times_chart_points: list[tuple[float, float, int, float]] = []
        self._active_times_tooltip_label: tk.Label | None = None
        self.watchdog_tick = 0
        self.last_schedule_date: date | None = None
        self._poll_invite_after: str | None = None
        self._poll_invite_count = 0
        self._install_thread: threading.Thread | None = None
        self._auto_backup_after: str | None = None
        self._wizard_bodies: list[tk.Frame] = []
        self._step_headers: list[tuple[tk.Label, tk.Label | None]] = []

        self._update_op_thread: threading.Thread | None = None
        self._update_op_result: dict | None = None
        self._install_blocked_by_running = False
        self._restart_pending = False
        self._stop_pending = False
        self._stop_pending_logged = False

        apply_dark_theme(root)
        root.title("Windrose Server Manager")
        # Wide enough for two-column Tools (App/Hosting, Backup/Schedule) without clipping.
        root.minsize(760, 660)
        root.geometry("920x820")
        try:
            candidates: list[Path] = []
            # Source run: project root
            candidates.append(_app_package_dir().parent / "WindroseServerManager.ico")
            # Frozen run: executable directory (onedir)
            if getattr(sys, "frozen", False):
                candidates.append(Path(sys.executable).resolve().parent / "WindroseServerManager.ico")
            # PyInstaller extraction dir (onefile / fallback)
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / "WindroseServerManager.ico")
            for icon_path in candidates:
                if icon_path.is_file():
                    root.iconbitmap(default=str(icon_path))
                    break
        except tk.TclError:
            pass

        self._build_ui()
        self._wire_events()

        self._initial_detect: Path | None = self._initialize_install_and_server_locations()

        self.paths.ensure_backup_dir()
        self._load_all_settings()
        self._post_load_init()
        self._schedule_watchdog()
        self._schedule_log_tail()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- UI ---
    def _build_ui(self) -> None:
        root = self.root
        c = self.c
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # Header
        hdr = tk.Frame(root, bg=c["bg_header"])
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        hdr.columnconfigure(0, weight=1)
        left = tk.Frame(hdr, bg=c["bg_header"])
        left.grid(row=0, column=0, sticky="w")
        self.canvas_status = tk.Canvas(left, width=14, height=14, bg=c["bg_header"], highlightthickness=0)
        self.canvas_status.pack(side=tk.LEFT, padx=(0, 8))
        self._status_dot = self.canvas_status.create_oval(2, 2, 12, 12, fill=c["status_stopped"], outline="")
        row1 = tk.Frame(left, bg=c["bg_header"])
        row1.pack(anchor="w")
        self.lbl_server_title = tk.Label(
            row1, text="Windrose Server", font=(None, 16, "bold"), fg=c["accent"], bg=c["bg_header"]
        )
        self.lbl_server_title.pack(side=tk.LEFT)
        self.lbl_status = tk.Label(
            row1, text="  Stopped", font=(None, 11), fg=c["text_dim"], bg=c["bg_header"]
        )
        self.lbl_status.pack(side=tk.LEFT)
        self.lbl_uptime_hdr = tk.Label(row1, text="", font=(None, 10), fg=c["text_muted"], bg=c["bg_header"])
        self.lbl_uptime_hdr.pack(side=tk.LEFT, padx=(16, 0))
        row2 = tk.Frame(left, bg=c["bg_header"])
        row2.pack(anchor="w", pady=(6, 0))
        tk.Label(row2, text="Code:", fg=c["text_muted"], bg=c["bg_header"], font=(None, 9)).pack(side=tk.LEFT)
        self.lbl_invite = tk.Label(
            row2, text="--", fg="#A0C4E0", bg=c["bg_header"], font=(None, 9), cursor="hand2"
        )
        self.lbl_invite.pack(side=tk.LEFT, padx=(6, 0))

        right = tk.Frame(hdr, bg=c["bg_header"])
        right.grid(row=0, column=1, sticky="e")
        tk_button(right, "Share", self._on_share, bg=c["blue_btn"], small=True).pack()

        # Notebook
        self.nb = ttk.Notebook(root)
        self.nb.configure(padding=0)
        self.nb.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._build_tab_dashboard()
        self._build_tab_insights()
        self._build_tab_config()
        self._build_tab_log()
        self._build_tab_tools()
        self._build_tab_install()
        self._build_tab_help()

        self.lbl_version_corner = tk.Label(
            root,
            text=f"Version: {constants.APP_VERSION}",
            fg=c["text_muted"],
            bg=c["bg"],
            font=(None, 10),
            cursor="hand2",
        )
        self.lbl_version_corner.place(relx=1.0, rely=0.12, anchor="ne", x=-12, y=0)
        self.lbl_version_corner.bind("<Button-1>", lambda e: self._select_tools_tab())

        # Footer buttons
        foot = tk.Frame(root, bg=c["bg"])
        foot.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        for i in range(4):
            foot.columnconfigure(i, weight=1)
        self.btn_start = tk_button(foot, "Start", self._on_start, bg=c["green_btn"])
        self.btn_start.grid(row=0, column=0, padx=2, sticky="ew")
        self.btn_stop = tk_button(foot, "Stop", self._on_stop, bg=c["red_btn"])
        self.btn_stop.grid(row=0, column=1, padx=2, sticky="ew")
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_restart = tk_button(foot, "Restart", self._on_restart, bg=c["navy_btn"])
        self.btn_restart.grid(row=0, column=2, padx=2, sticky="ew")
        self.btn_restart.config(state=tk.DISABLED)
        tk_button(foot, "Open Folder", self._on_open_folder, bg=c["folder_btn"]).grid(
            row=0, column=3, padx=2, sticky="ew"
        )

        # Status bar
        sb = tk.Frame(root, bg="#0A1218")
        sb.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        sb.columnconfigure(0, weight=1)
        self.lbl_footer_log = tk.Label(sb, text="Ready.", fg=c["red"], bg="#0A1218", font=(None, 10))
        self.lbl_footer_log.grid(row=0, column=0, sticky="w")

    def _panel_frame(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=self.c["bg_panel"], highlightbackground=self.c["border"], highlightthickness=1)
        return f

    def _select_tools_tab(self) -> None:
        for tab_id in self.nb.tabs():
            if self.nb.tab(tab_id, "text") == "Tools":
                self.nb.select(tab_id)
                return

    def _bind_mousewheel(self, canvas: tk.Canvas) -> None:
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    def _build_tab_dashboard(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Dashboard")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        stats = self._panel_frame(tab)
        stats.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 10))
        for i in range(5):
            stats.columnconfigure(i, weight=1)
        def stat_cell(col, title, key):
            f = tk.Frame(stats, bg=self.c["bg_panel"])
            f.grid(row=0, column=col, padx=8, pady=10)
            tk.Label(f, text=title, fg=self.c["text_muted"], bg=self.c["bg_panel"], font=(None, 9)).pack()
            lbl = tk.Label(f, text="--", font=(None, 18, "bold"), bg=self.c["bg_panel"])
            lbl.pack()
            return lbl
        self.lbl_cpu = stat_cell(0, "CPU", "cpu")
        self.lbl_cpu.config(fg=self.c["accent"])
        f_ram = tk.Frame(stats, bg=self.c["bg_panel"])
        f_ram.grid(row=0, column=1, padx=8, pady=10)
        tk.Label(f_ram, text="RAM", fg=self.c["text_muted"], bg=self.c["bg_panel"], font=(None, 9)).pack()
        f_ram_row = tk.Frame(f_ram, bg=self.c["bg_panel"])
        f_ram_row.pack()
        self.lbl_ram = tk.Label(
            f_ram_row, text="--", font=(None, 18, "bold"), bg=self.c["bg_panel"], fg="#5BA4CF"
        )
        self.lbl_ram.pack(side=tk.LEFT)
        self.lbl_ram_pct = tk.Label(
            f_ram_row,
            text="",
            font=(None, 10),
            bg=self.c["bg_panel"],
            fg="#5BA4CF",
        )
        self.lbl_ram_pct.pack(side=tk.LEFT, padx=(4, 0))
        self.lbl_players_big = stat_cell(2, "PLAYERS", "pl")
        self.lbl_players_big.config(fg="#70C48A")
        self.lbl_uptime_big = stat_cell(3, "UPTIME", "up")
        self.lbl_uptime_big.config(fg="#A0C4E0")
        self.lbl_crashes_big = stat_cell(4, "CRASHES", "cr")
        self.lbl_crashes_big.config(fg="#CC3333")

        mid = tk.Frame(tab, bg=self.c["bg"])
        mid.grid(row=1, column=0, sticky="nsew", padx=12, pady=0)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)
        mid.rowconfigure(0, weight=1)

        # Players
        plf = tk.Frame(mid, bg=self.c["bg"])
        plf.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        plf.columnconfigure(0, weight=1)
        plf.rowconfigure(1, weight=1)
        gh = tk.Frame(plf, bg=self.c["bg"])
        gh.grid(row=0, column=0, sticky="ew")
        ttk.Label(gh, text="Connected Players", style="Section.TLabel").pack(side=tk.LEFT)
        tk_button(gh, "Refresh", self._on_refresh_players, small=True).pack(side=tk.RIGHT)
        pl_box = self._panel_frame(plf)
        pl_box.grid(row=1, column=0, sticky="nsew", pady=4)
        self.list_players = tk.Listbox(
            pl_box, bg="#111E2A", fg=self.c["text"], selectbackground=self.c["tab_selected"],
            borderwidth=0, highlightthickness=0, font=(None, 11),
        )
        self.list_players.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # History
        hf = tk.Frame(mid, bg=self.c["bg"])
        hf.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        hf.columnconfigure(0, weight=1)
        hf.rowconfigure(1, weight=1)
        hh = tk.Frame(hf, bg=self.c["bg"])
        hh.grid(row=0, column=0, sticky="ew")
        ttk.Label(hh, text="Player Connection History", style="Section.TLabel").pack(side=tk.LEFT)
        tk_button(hh, "Clear", self._on_clear_history, bg=self.c["history_clear"], small=True).pack(side=tk.RIGHT)
        h_box = self._panel_frame(hf)
        h_box.grid(row=1, column=0, sticky="nsew", pady=4)
        self.list_history = tk.Listbox(
            h_box, bg="#111E2A", fg=self.c["text"], borderwidth=0, highlightthickness=0,
            font=("Consolas", 9),
        )
        self.list_history.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        bot = tk.Frame(tab, bg=self.c["bg"])
        bot.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        self.var_auto_restart = tk.BooleanVar(value=False)
        self.chk_auto_restart = ttk.Checkbutton(bot, text="Auto-restart if crashed", variable=self.var_auto_restart)
        self.chk_auto_restart.pack(side=tk.LEFT)
        auto_restart_tip = (
            "This will only Auto-Restart the server if the Server Manager detects an unexpected crash. "
            "If you manually stop the server you will need to manually start it again."
        )
        HoverToolTip(self.chk_auto_restart, auto_restart_tip)

    def _build_tab_insights(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Insights")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        p_wrap = tk.Frame(tab, bg=self.c["bg"])
        p_wrap.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 6))
        p_wrap.columnconfigure(0, weight=1)
        p_wrap.rowconfigure(1, weight=1)
        p_hdr = tk.Frame(p_wrap, bg=self.c["bg"])
        p_hdr.grid(row=0, column=0, sticky="ew")
        ttk.Label(p_hdr, text="Player Activity", style="Section.TLabel").pack(side=tk.LEFT)
        tk_button(
            p_hdr,
            "Clear",
            self._on_clear_player_activity_insights,
            bg=self.c["history_clear"],
            small=True,
        ).pack(side=tk.RIGHT)
        p_box = self._panel_frame(p_wrap)
        p_box.grid(row=1, column=0, sticky="nsew", pady=4)
        self.list_player_activity = tk.Listbox(
            p_box,
            bg="#111E2A",
            fg=self.c["text"],
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 10),
        )
        self.list_player_activity.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.lbl_player_activity_updated = tk.Label(
            p_wrap,
            text="Last updated: --",
            fg=self.c["text_dim"],
            bg=self.c["bg"],
            font=(None, 9),
        )
        self.lbl_player_activity_updated.grid(row=2, column=0, sticky="w", pady=(2, 0))

        h_wrap = tk.Frame(tab, bg=self.c["bg"])
        h_wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))
        h_wrap.columnconfigure(0, weight=1)
        h_wrap.rowconfigure(1, weight=1)
        h_hdr = tk.Frame(h_wrap, bg=self.c["bg"])
        h_hdr.grid(row=0, column=0, sticky="ew")
        ttk.Label(h_hdr, text="Most Active Times", style="Section.TLabel").pack(side=tk.LEFT)
        tk_button(
            h_hdr,
            "Clear",
            self._on_clear_active_times_insights,
            bg=self.c["history_clear"],
            small=True,
        ).pack(side=tk.RIGHT)
        h_box = self._panel_frame(h_wrap)
        h_box.grid(row=1, column=0, sticky="nsew", pady=4)
        h_box.columnconfigure(0, weight=1)
        h_box.rowconfigure(1, weight=1)
        self.canvas_active_times = tk.Canvas(
            h_box,
            height=150,
            bg="#0F1A24",
            highlightthickness=0,
        )
        self.canvas_active_times.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        self.canvas_active_times.bind("<Configure>", lambda _e: self._draw_active_times_chart())
        self.canvas_active_times.bind("<Motion>", self._on_active_times_chart_motion)
        self.canvas_active_times.bind("<Leave>", self._hide_active_times_tooltip)
        self.list_active_times = tk.Listbox(
            h_box,
            bg="#111E2A",
            fg=self.c["text"],
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 10),
        )
        self.list_active_times.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 4))
        self.lbl_active_times_updated = tk.Label(
            h_wrap,
            text="Last updated: --",
            fg=self.c["text_dim"],
            bg=self.c["bg"],
            font=(None, 9),
        )
        self.lbl_active_times_updated.grid(row=2, column=0, sticky="w", pady=(2, 0))

    def _build_tab_config(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Config")
        cv = tk.Canvas(tab, bg=self.c["bg"], highlightthickness=0)
        scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=cv.yview)
        inner = tk.Frame(cv, bg=self.c["bg"])
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=scroll.set)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._bind_mousewheel(cv)

        pad = tk.Frame(inner, bg=self.c["bg"])
        pad.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)
        self._last_world_settings_enabled = False
        self.lbl_config_lock = tk.Label(
            pad,
            text=(
                "The dedicated server is currently running. You cannot edit any configurations "
                "until the server is stopped."
            ),
            fg=self.c["accent"],
            bg=self.c["bg"],
            font=(None, 10),
            wraplength=640,
            justify=tk.LEFT,
        )
        self._hdr_server_settings = ttk.Label(pad, text="Server Settings", style="Section.TLabel")
        self._hdr_server_settings.pack(anchor="w")
        self.lbl_config_lock.pack_forget()

        sf = self._panel_frame(pad)
        sf.pack(fill=tk.X, pady=(0, 8))
        sf_inner = tk.Frame(sf, bg=self.c["bg_panel"])
        sf_inner.pack(fill=tk.X, padx=12, pady=10)
        lbl_server_name = tk.Label(sf_inner, text="Server Name", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_server_name.grid(row=0, column=0, sticky="w")
        self.ent_srv_name = ttk.Entry(sf_inner, width=50)
        self.ent_srv_name.grid(row=0, column=1, sticky="ew", pady=4)
        lbl_invite_code = tk.Label(sf_inner, text="Invite Code", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_invite_code.grid(row=1, column=0, sticky="w")
        self.ent_invite_code = ttk.Entry(sf_inner, width=50)
        self.ent_invite_code.grid(row=1, column=1, sticky="ew", pady=4)
        lbl_max_players = tk.Label(sf_inner, text="Max Players", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_max_players.grid(row=2, column=0, sticky="w")
        mx = tk.Frame(sf_inner, bg=self.c["bg_panel"])
        mx.grid(row=2, column=1, sticky="ew", pady=4)
        self.scale_max = tk.Scale(mx, from_=1, to=20, orient=tk.HORIZONTAL, showvalue=0, bg=self.c["bg_panel"], fg=self.c["accent"], highlightthickness=0, troughcolor=self.c["border_input"])
        self.scale_max.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.lbl_max_val = tk.Label(mx, text="10", fg=self.c["accent"], bg=self.c["bg_panel"], font=(None, 11, "bold"), width=3)
        self.lbl_max_val.pack(side=tk.LEFT)
        lbl_password = tk.Label(sf_inner, text="Password", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_password.grid(row=3, column=0, sticky="w")
        pwf = tk.Frame(sf_inner, bg=self.c["bg_panel"])
        pwf.grid(row=3, column=1, sticky="ew", pady=4)
        self.var_pw_en = tk.BooleanVar(value=False)
        self.chk_pw_en = ttk.Checkbutton(
            pwf, text="Enable", variable=self.var_pw_en, command=self._toggle_pw_entry
        )
        self.chk_pw_en.pack(side=tk.LEFT, padx=(0, 8))
        self.ent_password = ttk.Entry(pwf, width=40, show="*")
        self.ent_password.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.ent_password.config(state=tk.DISABLED)
        self.btn_reveal_password = tk_button(pwf, "Reveal", small=True)
        self.btn_reveal_password.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_reveal_password.bind("<ButtonPress-1>", self._on_pw_reveal_press)
        self.btn_reveal_password.bind("<ButtonRelease-1>", self._on_pw_reveal_release)
        self.btn_reveal_password.bind("<Leave>", self._on_pw_reveal_release)
        self.btn_reveal_password.config(state=tk.DISABLED)
        lbl_proxy = tk.Label(sf_inner, text="Proxy Address", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_proxy.grid(row=4, column=0, sticky="w")
        self.ent_proxy = ttk.Entry(sf_inner, width=50)
        self.ent_proxy.grid(row=4, column=1, sticky="ew", pady=4)
        self.ent_proxy.insert(0, "127.0.0.1")
        lbl_conn_port = tk.Label(sf_inner, text="Connection Port", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_conn_port.grid(row=5, column=0, sticky="w")
        self.ent_direct_port = ttk.Entry(sf_inner, width=12)
        self.ent_direct_port.grid(row=5, column=1, sticky="w", pady=4)
        self.ent_direct_port.insert(0, "7777")
        sf_inner.columnconfigure(1, weight=1)
        HoverToolTip(lbl_server_name, "This is the name of the server that players will see")
        HoverToolTip(lbl_invite_code, "Invite code to find your server. 0-9, a-z and A-Z symbols are allowed. Should contain at least 6 symbols. Case sensitive.")
        HoverToolTip(lbl_max_players, "The amount of players that are allowed to join your server")
        HoverToolTip(lbl_password, "Specify if password is required. Should be toggled on if password specified and toggled off if password field is empty. Otherwise it may cause unexpected behavior.")
        HoverToolTip(lbl_proxy, "The IP that will be used to host the server. Default: 127.0.0.1 (This computer IP)")
        HoverToolTip(lbl_conn_port, "The port that is used to connect to your server. Default: 7777")

        ttk.Label(pad, text="World Settings", style="Section.TLabel").pack(anchor="w", pady=(12, 0))
        wf = self._panel_frame(pad)
        wf.pack(fill=tk.X)
        wi = tk.Frame(wf, bg=self.c["bg_panel"])
        wi.pack(fill=tk.X, padx=12, pady=10)
        self.lbl_world_missing = tk.Label(
            wi,
            text=(
                "World settings are unavailable until the server has generated a world.\n"
                "Start the server once, click Reload Saved Config until this message disappears, and then stop the server."
            ),
            fg=self.c["red"],
            bg=self.c["bg_panel"],
            font=(None, 10),
            justify=tk.LEFT,
            wraplength=520,
        )
        self.lbl_world_missing.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        lbl_preset = tk.Label(wi, text="Difficulty Preset", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_preset.grid(row=1, column=0, sticky="w")
        self.cmb_preset = ttk.Combobox(wi, values=("Easy", "Medium", "Hard", "Custom"), state="readonly", width=20)
        self.cmb_preset.grid(row=1, column=1, sticky="w", pady=4)
        self.cmb_preset.set("Medium")
        self.frame_custom = tk.Frame(wi, bg="#0D1820")
        self.frame_custom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6, padx=10, ipadx=8, ipady=8)
        self._world_sliders: dict[str, tuple[tk.Scale, tk.Label]] = {}
        labels_tooltips = [
            ("mob_health", "Mob Health", 0.2, 5.0, "Enemy creature health multiplier."),
            ("mob_damage", "Mob Damage", 0.2, 5.0, "Damage dealt by enemy creatures."),
            ("ship_health", "Ship Health", 0.4, 5.0, "Enemy ship hull strength."),
            ("ship_damage", "Ship Damage", 0.2, 2.5, "Damage dealt by enemy ships."),
            ("boarding", "Boarding", 0.2, 5.0, "Boarding encounter difficulty."),
            ("coop_stats", "Coop Stats", 0.0, 2.0, "Enemy stat scaling per extra player."),
            ("coop_ship", "Coop Ship", 0.0, 2.0, "Enemy ship scaling per extra player."),
        ]
        for i, (key, title, lo, hi, tip) in enumerate(labels_tooltips):
            lbl_custom = tk.Label(self.frame_custom, text=title, bg="#0D1820", fg=self.c["text_dim"])
            lbl_custom.grid(row=i, column=0, sticky="w", pady=2)
            sc = tk.Scale(self.frame_custom, from_=lo, to=hi, resolution=0.1, orient=tk.HORIZONTAL, length=220, bg="#0D1820", fg=self.c["accent"], highlightthickness=0, troughcolor=self.c["border_input"])
            sc.set(1.0)
            sc.grid(row=i, column=1, sticky="ew", padx=6)
            vl = tk.Label(self.frame_custom, text="1.0", fg=self.c["accent"], bg="#0D1820", width=5)
            vl.grid(row=i, column=2)
            self._world_sliders[key] = (sc, vl)
            custom_tips = {
                "mob_health": "Defines how much Health enemies have; Default: 1.0",
                "mob_damage": "Defines how hard enemies hit; Default: 1.0",
                "ship_health": "Defines how much Ship Health enemy ships have; Default: 1.0",
                "ship_damage": "Defines how much Damage enemy ships deal; Default: 1.0",
                "boarding": "Defines how many enemy sailors must be defeated to win a boarding action; Default: 1.0",
                "coop_stats": "Adjusts enemy Health and how fast enemies lose Posture based on the number of players on the server; Default: 1.0",
                "coop_ship": "Adjusts enemy Ship Health based on the number of players on the server; Default: 0.0",
            }
            HoverToolTip(lbl_custom, custom_tips.get(key, ""))
        self.frame_custom.columnconfigure(1, weight=1)
        self.frame_custom.grid_remove()

        lbl_combat = tk.Label(wi, text="Combat Diff.", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl_combat.grid(row=3, column=0, sticky="w")
        self.cmb_combat = ttk.Combobox(wi, values=("Easy", "Normal", "Hard"), state="readonly", width=18)
        self.cmb_combat.grid(row=3, column=1, sticky="w", pady=4)
        self.cmb_combat.set("Normal")
        self.var_coop_quests = tk.BooleanVar(value=False)
        self.var_easy_explore = tk.BooleanVar(value=False)
        self.chk_coop_quests = ttk.Checkbutton(wi, text="Coop Quests", variable=self.var_coop_quests)
        self.chk_coop_quests.grid(row=4, column=1, sticky="w")
        self.chk_easy_explore = ttk.Checkbutton(wi, text="Easy Exploration", variable=self.var_easy_explore)
        self.chk_easy_explore.grid(row=5, column=1, sticky="w")
        HoverToolTip(lbl_preset, "Adjusts predefined world challenge settings. Use Custom to tune each slider manually.")
        HoverToolTip(lbl_combat, "Defines how difficult boss encounters are and how aggressive enemies are in general; Default: Normal")
        HoverToolTip(self.chk_coop_quests, "If any player on the server completes a quest marked as a co-op quest, it auto-completes for all players who currently have it active; Default: true")
        HoverToolTip(self.chk_easy_explore, "When this option is enabled it disables markers on the map that highlight points of interest making them harder to find; Default: false")

        bf = tk.Frame(pad, bg=self.c["bg"])
        bf.pack(fill=tk.X, pady=12)
        self.btn_cfg_save = tk_button(bf, "Save Config", self._on_save_config, bg=self.c["green_btn"])
        self.btn_cfg_save.pack(side=tk.LEFT, padx=2)
        self.btn_cfg_reload = tk_button(bf, "Reload Saved Config", self._on_reload_config, bg=self.c["gray_btn"])
        self.btn_cfg_reload.pack(side=tk.LEFT, padx=2)
        self.btn_cfg_open_server = tk_button(bf, "Open Server Config", self._on_open_server_config, bg=self.c["blue_btn"])
        self.btn_cfg_open_server.pack(side=tk.LEFT, padx=2)
        self.btn_cfg_open_world = tk_button(bf, "Open World Config", self._on_open_world_json, bg=self.c["blue_btn"])
        self.btn_cfg_open_world.pack(side=tk.LEFT, padx=2)
        self.lbl_cfg_status = tk.Label(pad, text="", fg=self.c["green"], bg=self.c["bg"], font=(None, 10))
        self.lbl_cfg_status.pack(anchor="w", pady=4)

    def _build_tab_log(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Log")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        top = tk.Frame(tab, bg=self.c["bg"])
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=6)
        self.btn_log_all = tk_button(top, "All", lambda: self._set_log_filter("All", self.btn_log_all), bg=self.c["blue_btn"], small=True)
        self.btn_log_all.pack(side=tk.LEFT)
        self.btn_log_pl = tk_button(top, "Players", lambda: self._set_log_filter("Players", self.btn_log_pl), small=True)
        self.btn_log_pl.pack(side=tk.LEFT, padx=4)
        self.btn_log_warn = tk_button(top, "Warnings", lambda: self._set_log_filter("Warn", self.btn_log_warn), small=True)
        self.btn_log_warn.pack(side=tk.LEFT, padx=4)
        self.btn_log_err = tk_button(top, "Errors", lambda: self._set_log_filter("Errors", self.btn_log_err), small=True)
        self.btn_log_err.pack(side=tk.LEFT, padx=4)
        self.var_autoscroll_log = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Auto-scroll", variable=self.var_autoscroll_log).pack(side=tk.LEFT, padx=(16, 0))
        tk_button(top, "Export Logs", self._on_export_logs, small=True).pack(side=tk.LEFT, padx=(16, 0))

        self.txt_log = tk.Text(tab, bg="#111E2A", fg="#708899", font=("Consolas", 10), wrap=tk.NONE, borderwidth=0, highlightthickness=1, highlightbackground=self.c["border"])
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.txt_log.tag_configure("err", foreground="tomato")
        self.txt_log.tag_configure("warn", foreground="orange")
        self.txt_log.tag_configure("join", foreground="light green")
        self.txt_log.tag_configure("leave", foreground="#FA8072")
        self.txt_log.tag_configure("def", foreground="#708899")
        ys = ttk.Scrollbar(tab, command=self.txt_log.yview)
        ys.grid(row=1, column=1, sticky="ns", pady=(0, 10))
        self.txt_log.config(yscrollcommand=ys.set)
        xs = ttk.Scrollbar(tab, orient=tk.HORIZONTAL, command=self.txt_log.xview)
        xs.grid(row=2, column=0, sticky="ew", padx=10)
        self.txt_log.config(xscrollcommand=xs.set)

    def _build_tab_tools(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Tools")
        cv = tk.Canvas(tab, bg=self.c["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=cv.yview)
        inner = tk.Frame(cv, bg=self.c["bg"])
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._bind_mousewheel(cv)

        pad = tk.Frame(inner, bg=self.c["bg"])
        pad.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)
        pad.columnconfigure(0, weight=1, uniform="tools_cols")
        pad.columnconfigure(1, weight=1, uniform="tools_cols")

        _wrap_half = 300

        def tools_section(parent: tk.Frame, title: str) -> tk.Frame:
            ttk.Label(parent, text=title, style="Section.TLabel").pack(anchor="w")
            fr = self._panel_frame(parent)
            fr.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
            box = tk.Frame(fr, bg=self.c["bg_panel"])
            box.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
            return box

        r0a = tk.Frame(pad, bg=self.c["bg"])
        r0a.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 10))
        r0b = tk.Frame(pad, bg=self.c["bg"])
        r0b.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 10))
        r1a = tk.Frame(pad, bg=self.c["bg"])
        r1a.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 10))
        r1b = tk.Frame(pad, bg=self.c["bg"])
        r1b.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 10))

        u = tools_section(r0a, "App Update")
        uf = tk.Frame(u, bg=self.c["bg_panel"])
        uf.pack(fill=tk.X)
        self.lbl_cur_ver = tk.Label(uf, text="Current version: ...", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_cur_ver.pack(side=tk.LEFT, padx=(0, 12))
        tk_button(uf, "Check for Updates", self._on_check_update, small=True).pack(side=tk.LEFT, padx=2)
        tk_button(uf, "Patch Notes", self._on_patch_notes, small=True).pack(side=tk.LEFT, padx=2)
        self.lbl_update_status = tk.Label(
            u, text="", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10), wraplength=_wrap_half, justify=tk.LEFT
        )
        self.lbl_update_status.pack(anchor="w", pady=6)

        h = tools_section(r0b, "Hosting Client")
        hf = tk.Frame(h, bg=self.c["bg_panel"])
        hf.pack(fill=tk.X)
        self.lbl_client_mode = tk.Label(hf, text="Current mode: Steam", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_client_mode.pack(side=tk.LEFT, padx=(0, 12))
        tk_button(hf, "Switch Client Mode", self._on_switch_client, small=True).pack(side=tk.LEFT)
        tk.Label(
            h,
            text="Use this to switch between Steam and SteamCMD setup without deleting settings.",
            fg=self.c["text_dim"],
            bg=self.c["bg_panel"],
            font=(None, 10),
            wraplength=_wrap_half,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=4)

        b = tools_section(r1a, "Backup")
        bf = tk.Frame(b, bg=self.c["bg_panel"])
        bf.pack(fill=tk.X)
        tk_button(bf, "Backup Server Now", self._on_backup, bg=self.c["green_btn"]).pack(side=tk.LEFT, padx=2)
        tk_button(bf, "Open Backup Folder", self._on_open_backups, bg=self.c["blue_btn"]).pack(side=tk.LEFT, padx=2)
        self.lbl_last_backup = tk.Label(b, text="Last backup: none", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_last_backup.pack(anchor="w", pady=4)
        bf2 = tk.Frame(b, bg=self.c["bg_panel"])
        bf2.pack(anchor="w", pady=4)
        self.var_auto_backup = tk.BooleanVar(value=False)
        ttk.Checkbutton(bf2, text="Auto-backup every", variable=self.var_auto_backup, command=self._on_auto_backup_toggle).pack(side=tk.LEFT)
        self.ent_backup_interval = ttk.Entry(bf2, width=6)
        self.ent_backup_interval.pack(side=tk.LEFT, padx=6)
        self.ent_backup_interval.insert(0, "4")
        self.cmb_backup_unit = ttk.Combobox(
            bf2, values=("Hours", "Minutes"), state="readonly", width=9
        )
        self.cmb_backup_unit.pack(side=tk.LEFT)
        self.cmb_backup_unit.set("Hours")
        self.ent_backup_interval.bind("<FocusOut>", lambda e: self._reschedule_auto_backup())
        self.ent_backup_interval.bind("<Return>", lambda e: self._reschedule_auto_backup())
        self.cmb_backup_unit.bind("<<ComboboxSelected>>", lambda e: self._reschedule_auto_backup())
        self.lbl_next_backup = tk.Label(b, text="", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_next_backup.pack(anchor="w")

        s = tools_section(r1b, "Scheduled Restart")
        sf = tk.Frame(s, bg=self.c["bg_panel"])
        sf.pack(anchor="w")
        self.var_schedule = tk.BooleanVar(value=False)
        ttk.Checkbutton(sf, text="Enable daily restart at", variable=self.var_schedule).pack(side=tk.LEFT)
        self.ent_schedule_time = ttk.Entry(sf, width=8)
        self.ent_schedule_time.insert(0, "04:00")
        self.ent_schedule_time.pack(side=tk.LEFT, padx=6)
        auto_restart_tip = (
            "This will only Auto-Restart the server if the Server Manager detects an unexpected crash. "
            "If you manually stop the server you will need to manually start it again."
        )

        discord_row = tk.Frame(pad, bg=self.c["bg"])
        discord_row.grid(row=2, column=0, columnspan=2, sticky="ew")
        d_hdr = tk.Frame(discord_row, bg=self.c["bg"])
        d_hdr.pack(anchor="w", fill=tk.X)
        ttk.Label(d_hdr, text="Discord Notifications", style="Section.TLabel").pack(side=tk.LEFT, anchor="w")
        _info_sz = 18
        _ica = self.c["accent"]
        canvas_d_discord_info = tk.Canvas(
            d_hdr, width=_info_sz, height=_info_sz, bg=self.c["bg"], highlightthickness=0, cursor="hand2"
        )
        _m = 1.0
        canvas_d_discord_info.create_oval(_m, _m, _info_sz - _m, _info_sz - _m, outline=_ica, width=1.5)
        canvas_d_discord_info.create_text(
            _info_sz / 2,
            _info_sz / 2 + 0.5,
            text="i",
            fill=_ica,
            font=(None, 9, "bold"),
            anchor=tk.CENTER,
        )
        canvas_d_discord_info.pack(side=tk.LEFT, padx=(4, 0), pady=0, anchor="w")
        HoverToolTip(
            canvas_d_discord_info,
            "Post to a channel using a webhook URL from Discord (Server Settings → Integrations → Webhooks).\n\n"
            "Only official discord.com webhook URLs are accepted.\n\n"
            "To ping a person, use a real mention: <@user_id> (enable Advanced → Developer Mode in Discord, "
            "then right-click the user → Copy User ID).\n"
            "A plain @DisplayName in the text is not a mention.\nFor a role, use <@&role_id>.",
        )
        d_fr = self._panel_frame(discord_row)
        d_fr.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        d = tk.Frame(d_fr, bg=self.c["bg_panel"])
        d.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        self.var_discord_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            d,
            text="Send notifications for stop, restart, scheduled restart, and unexpected shutdown",
            variable=self.var_discord_enabled,
        ).pack(anchor="w")
        tk.Label(d, text="Webhook URL", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10)).pack(
            anchor="w", pady=(8, 2)
        )
        self.ent_discord_url = ttk.Entry(d, width=72)
        self.ent_discord_url.pack(anchor="w", fill=tk.X)

        def _discord_msg_row(lbl: str) -> ttk.Entry:
            tk.Label(d, text=lbl, fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10)).pack(
                anchor="w", pady=(8, 2)
            )
            e = ttk.Entry(d, width=72)
            e.pack(anchor="w", fill=tk.X)
            return e

        self.ent_discord_msg_stop = _discord_msg_row("Message when the server is stopped:")
        self.ent_discord_msg_restart = _discord_msg_row("Message when the server is restarted (manual or toolbar):")
        self.ent_discord_msg_schedule = _discord_msg_row("Message when a daily scheduled restart begins:")
        lbl_crash_msg = tk.Label(
            d,
            text="Message when the server process ends unexpectedly:",
            fg=self.c["text_dim"],
            bg=self.c["bg_panel"],
            font=(None, 10),
        )
        lbl_crash_msg.pack(anchor="w", pady=(8, 2))
        self.ent_discord_msg_crash = ttk.Entry(d, width=72)
        self.ent_discord_msg_crash.pack(anchor="w", fill=tk.X)
        HoverToolTip(lbl_crash_msg, auto_restart_tip)
        HoverToolTip(self.ent_discord_msg_crash, auto_restart_tip)
        df = tk.Frame(d, bg=self.c["bg_panel"])
        df.pack(anchor="w", pady=(10, 0))
        tk_button(df, "Send test message", self._on_discord_test, small=True).pack(side=tk.LEFT, padx=(0, 8))

    def _build_tab_install(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Install")
        cv = tk.Canvas(tab, bg=self.c["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=cv.yview)
        inner = tk.Frame(cv, bg=self.c["bg"])
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._bind_mousewheel(cv)
        pad = tk.Frame(inner, bg=self.c["bg"])
        pad.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        ban = self._panel_frame(pad)
        ban.pack(fill=tk.X, pady=(0, 10))
        bi = tk.Frame(ban, bg=self.c["bg_panel"])
        bi.pack(fill=tk.X, padx=12, pady=10)
        tk.Label(bi, text="Server Setup", font=(None, 12, "bold"), fg=self.c["accent"], bg=self.c["bg_panel"]).pack(anchor="w")
        tk.Label(bi, text="Follow these steps to get your Windrose dedicated server running.", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10), wraplength=520, justify=tk.LEFT).pack(anchor="w", pady=4)
        st = tk.Frame(bi, bg=self.c["bg_panel"])
        st.pack(anchor="e")
        self.canvas_install = tk.Canvas(st, width=14, height=14, bg=self.c["bg_panel"], highlightthickness=0)
        self.canvas_install.pack(side=tk.LEFT, padx=(0, 6))
        self._install_dot = self.canvas_install.create_oval(2, 2, 12, 12, fill=self.c["red"], outline="")
        self.lbl_install_status = tk.Label(st, text="Not installed", fg=self.c["red"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_install_status.pack(side=tk.LEFT)

        for sn, title in [
            (1, "Check Requirements"),
            (2, "Install Server Files"),
        ]:
            self._add_wizard_step(pad, sn, title)

        # Step 1 body
        s1 = self._wizard_bodies[0]
        self.lbl_req_help = tk.Label(
            s1,
            text="Windrose must be installed via Steam (App ID 4129620). The dedicated server files are bundled inside the game - no separate download needed.",
            fg=self.c["text_dim"],
            bg=self.c["bg_panel"],
            font=(None, 10),
            wraplength=520,
            justify=tk.LEFT,
        )
        self.lbl_req_help.pack(anchor="w", pady=4)
        self.lbl_req_steam = tk.Label(s1, text="Checking...", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_req_steam.pack(anchor="w", pady=4)
        tk_button(s1, "Re-check", self._on_check_reqs, small=True).pack(anchor="w")

        # Step 2
        s2 = self._wizard_bodies[1]
        self.lbl_steam_source = tk.Label(s2, text="Steam Source", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_steam_source.pack(anchor="w")
        g2 = tk.Frame(s2, bg=self.c["bg_panel"])
        g2.pack(fill=tk.X, pady=4)
        g2.columnconfigure(0, weight=1)
        self.ent_steam_src = ttk.Entry(g2)
        self.ent_steam_src.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        tk_button(g2, "Auto-Detect", self._on_detect_steam, small=True).grid(row=0, column=1, padx=2)
        tk_button(g2, "Browse...", self._on_browse_source, small=True).grid(row=0, column=2)
        tk.Label(s2, text="Server Location", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10)).pack(anchor="w")
        g3 = tk.Frame(s2, bg=self.c["bg_panel"])
        g3.pack(fill=tk.X, pady=4)
        g3.columnconfigure(0, weight=1)
        self.ent_install_dest = ttk.Entry(g3)
        self.ent_install_dest.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        tk_button(g3, "Browse...", self._on_browse_dest, small=True).grid(row=0, column=1)
        self.txt_install_log = tk.Text(s2, height=6, font=("Consolas", 9), bg="#0A1218", fg="#90A8B8", borderwidth=1, relief=tk.FLAT)
        self.txt_install_log.pack(fill=tk.X, pady=6)
        ib = tk.Frame(s2, bg=self.c["bg_panel"])
        ib.pack(anchor="w")
        self.btn_install_server = tk_button(ib, "Install Server", self._on_install_server, bg=self.c["green_btn"])
        self.btn_install_server.pack(side=tk.LEFT)
        self.tip_install_server = HoverToolTip(self.btn_install_server, "")
        self.lbl_install_warn = tk.Label(ib, text="", fg=self.c["accent"], bg=self.c["bg_panel"], font=(None, 10))
        self.lbl_install_warn.pack(side=tk.LEFT, padx=12)

        tk.Label(
            pad,
            text="Additional setup like server name and world options is available in the Config tab.",
            fg=self.c["text_dim"],
            bg=self.c["bg"],
            font=(None, 10),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

    def _build_tab_help(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Help")
        tab.columnconfigure(0, weight=1)

        about_panel = self._panel_frame(tab)
        about_panel.pack(fill=tk.X, padx=14, pady=12)
        about = tk.Frame(about_panel, bg=self.c["bg_panel"])
        about.pack(fill=tk.X, padx=12, pady=10)

        tk.Label(
            about,
            text="About",
            font=(None, 12, "bold"),
            fg=self.c["accent"],
            bg=self.c["bg_panel"],
        ).pack(anchor="w")

        tk.Label(
            about,
            text="Useful project links for updates, documentation, and support.",
            fg=self.c["text_dim"],
            bg=self.c["bg_panel"],
            font=(None, 10),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 8))

        actions = tk.Frame(about, bg=self.c["bg_panel"])
        actions.pack(anchor="w")
        tk_button(
            actions,
            "GitHub",
            lambda: webbrowser.open(constants.GITHUB_REPO_URL),
            small=True,
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk_button(
            actions,
            "Report Issue or Feature Request",
            lambda: webbrowser.open(constants.GITHUB_ISSUES_NEW_URL),
            small=True,
        ).pack(side=tk.LEFT)

        donation_panel = self._panel_frame(tab)
        donation_panel.pack(fill=tk.X, padx=14, pady=(0, 12))
        donation = tk.Frame(donation_panel, bg=self.c["bg_panel"])
        donation.pack(fill=tk.X, padx=12, pady=10)

        tk.Label(
            donation,
            text="Donation",
            font=(None, 12, "bold"),
            fg=self.c["accent"],
            bg=self.c["bg_panel"],
        ).pack(anchor="w")

        tk.Label(
            donation,
            text="If this Server Manager helps you, any small donation is greatly appreciated!",
            fg=self.c["text_dim"],
            bg=self.c["bg_panel"],
            font=(None, 10),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 8))

        tk_button(
            donation,
            "Buy me a Beer",
            lambda: webbrowser.open(constants.DONATE_URL),
            bg=self.c["green_btn"],
            small=True,
        ).pack(anchor="w")

    def _add_wizard_step(self, pad, num: int, title: str) -> None:
        fr = self._panel_frame(pad)
        fr.pack(fill=tk.X, pady=(0, 6))
        hd = tk.Frame(fr, bg=self.c["bg_panel"])
        hd.pack(fill=tk.X, padx=12, pady=8)
        hd.columnconfigure(1, weight=1)
        badge = tk.Label(
            hd, text=str(num), width=2, font=(None, 11, "bold"), fg="white", bg=self.c["gray_btn"]
        )
        badge.grid(row=0, column=0, padx=(0, 10))
        tk.Label(hd, text=title, font=(None, 11, "bold"), fg=self.c["text"], bg=self.c["bg_panel"]).grid(
            row=0, column=1, sticky="w"
        )
        st: tk.Label | None = None
        if num <= 2:
            st = tk.Label(hd, text="", font=(None, 10), fg=self.c["text_dim"], bg=self.c["bg_panel"])
            st.grid(row=0, column=2, sticky="e")
        body = tk.Frame(fr, bg=self.c["bg_panel"])
        body.pack(fill=tk.X, padx=(36, 12), pady=(0, 12))
        self._wizard_bodies.append(body)
        self._step_headers.append((badge, st))

    def _wire_events(self) -> None:
        self.lbl_invite.bind("<Button-1>", lambda e: self._copy_invite())
        self.scale_max.config(command=self._on_max_players_slide)
        self.cmb_preset.bind("<<ComboboxSelected>>", lambda e: self._on_preset_change())
        for key, (sc, lbl) in self._world_sliders.items():
            sc.config(command=lambda v, l=lbl, s=sc: l.config(text=f"{float(s.get()):.1f}"))

    def _toggle_pw_entry(self) -> None:
        if self.var_pw_en.get():
            self.ent_password.config(state=tk.NORMAL)
            self.btn_reveal_password.config(state=tk.NORMAL)
        else:
            # Keep masked text visible when disabled, but prevent editing.
            self.ent_password.config(state="readonly")
            self.btn_reveal_password.config(state=tk.DISABLED)
            self.ent_password.config(show="*")

    def _on_pw_reveal_press(self, _event=None) -> None:
        if str(self.ent_password.cget("state")) == "normal":
            self.ent_password.config(show="")

    def _on_pw_reveal_release(self, _event=None) -> None:
        self.ent_password.config(show="*")

    def _on_preset_change(self) -> None:
        if self.cmb_preset.get() == "Custom":
            self.frame_custom.grid()
        else:
            self.frame_custom.grid_remove()

    def _on_max_players_slide(self, v) -> None:
        n = int(round(float(v)))
        self.lbl_max_val.config(text=str(n))
        self.max_players = n

    def log(self, msg: str) -> None:
        self.lbl_footer_log.config(text=msg)

    def _schedule_watchdog(self) -> None:
        self._watchdog_tick()
        self.root.after(3000, self._schedule_watchdog)

    def _watchdog_tick(self) -> None:
        self.watchdog_tick += 1
        proc = process_ops.get_server_process()
        ui_running = "Running" in self.lbl_status.cget("text")
        if self._stop_pending and not proc:
            # Intentional stop completed; do not treat as crash.
            self._stop_pending = False
            self._stop_pending_logged = False
        if proc and not ui_running:
            if self._stop_pending:
                # Stop was requested; ignore this short shutdown window.
                if not self._stop_pending_logged:
                    self.log("Manual stop in progress...")
                    self._stop_pending_logged = True
            else:
                self._set_ui_running()
                if self.start_time is None:
                    self.start_time = datetime.now()
        elif proc and ui_running:
            self._update_stats(proc)
            if self.watchdog_tick % 10 == 0:
                self._refresh_player_list()
        elif not proc and ui_running:
            if self._restart_pending:
                pass
            else:
                self._set_ui_stopped()
                self.log("Server process ended unexpectedly.")
                self.mgr.crash_count += 1
                self._update_crash_stat()
                settings.save_manager_settings(self.paths, self.mgr, self.client)
                self._discord_notify_crash()
                self.server_popen = None
                if self.var_auto_restart.get():
                    self.log("Auto-restarting...")
                    self._do_restart("crash")
        if self.var_schedule.get():
            now_hm = datetime.now().strftime("%H:%M")
            target = self.ent_schedule_time.get().strip()
            today = date.today()
            if now_hm == target and self.last_schedule_date != today:
                self.last_schedule_date = today
                self.log("Scheduled daily restart.")
                self._do_restart("schedule")
        if self.paths.server_exe.is_file():
            self.canvas_install.itemconfig(self._install_dot, fill="#00FF00")
            self.lbl_install_status.config(text="Server installed.", fg=self.c["green"])
        # Keep Config tab lock state in sync with real process state, including
        # the short shutdown window right after pressing Stop.
        self._apply_config_tab_state()
        self._apply_install_update_button_state()

    def _schedule_log_tail(self) -> None:
        self._update_log_viewer()
        self.root.after(3000, self._schedule_log_tail)

    def _set_ui_running(self) -> None:
        self.canvas_status.itemconfig(self._status_dot, fill="#00FF00")
        self.lbl_status.config(text="  Running", fg=self.c["green"])
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_restart.config(state=tk.NORMAL)
        code = config_io.read_invite_code(self.paths)
        self.lbl_invite.config(text=code if code else "(pending...)")
        self._apply_config_tab_state()

    def _set_ui_stopped(self) -> None:
        self._close_open_insight_sessions()
        self.canvas_status.itemconfig(self._status_dot, fill=self.c["status_stopped"])
        self.lbl_status.config(text="  Stopped", fg=self.c["text_dim"])
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_restart.config(state=tk.DISABLED)
        self.lbl_invite.config(text="--")
        self._reset_stats()
        self._apply_config_tab_state()

    def _apply_config_tab_state(self) -> None:
        """While the dedicated server is running, block all Config tab changes."""
        if process_ops.get_server_process():
            self.lbl_config_lock.pack(anchor="w", pady=(0, 8), before=self._hdr_server_settings)
            for w in (self.ent_srv_name, self.ent_invite_code, self.ent_proxy, self.ent_direct_port):
                w.config(state="disabled")
            self.scale_max.config(state=tk.DISABLED)
            self.chk_pw_en.state(["disabled"])
            self.ent_password.config(state="disabled")
            self.btn_reveal_password.config(state=tk.DISABLED)
            self.cmb_preset.config(state="disabled")
            self.cmb_combat.config(state="disabled")
            for sc, _vl in self._world_sliders.values():
                sc.config(state=tk.DISABLED)
            self.chk_coop_quests.state(["disabled"])
            self.chk_easy_explore.state(["disabled"])
            for b in (
                self.btn_cfg_save,
                self.btn_cfg_reload,
                self.btn_cfg_open_server,
                self.btn_cfg_open_world,
            ):
                b.config(state=tk.DISABLED)
            return
        self.lbl_config_lock.pack_forget()
        for w in (self.ent_srv_name, self.ent_invite_code, self.ent_proxy, self.ent_direct_port):
            w.config(state=tk.NORMAL)
        self.scale_max.config(state=tk.NORMAL)
        self.chk_pw_en.state(["!disabled"])
        self._toggle_pw_entry()
        for b in (
            self.btn_cfg_save,
            self.btn_cfg_reload,
            self.btn_cfg_open_server,
            self.btn_cfg_open_world,
        ):
            b.config(state=tk.NORMAL)
        self._set_world_settings_enabled(self._last_world_settings_enabled)

    def _reset_stats(self) -> None:
        self.lbl_cpu.config(text="--")
        self.lbl_ram.config(text="--")
        self.lbl_ram_pct.config(text="")
        self.lbl_players_big.config(text="--")
        self.lbl_uptime_big.config(text="--")
        self.lbl_uptime_hdr.config(text="")
        self._update_crash_stat()
        self.list_players.delete(0, tk.END)

    def _update_crash_stat(self) -> None:
        self.lbl_crashes_big.config(text=str(max(0, int(self.mgr.crash_count))))

    def _update_stats(self, proc: psutil.Process) -> None:
        try:
            now = datetime.now()
            cpu_pct = 0.0
            if self.prev_cpu_time is not None and self.prev_cpu_check is not None:
                elapsed = (now - self.prev_cpu_check).total_seconds()
                if elapsed > 0:
                    cpu_now = proc.cpu_times().user + proc.cpu_times().system
                    delta = cpu_now - self.prev_cpu_time
                    cpu_pct = round((delta / elapsed / max(psutil.cpu_count() or 1, 1)) * 100, 1)
            self.prev_cpu_time = proc.cpu_times().user + proc.cpu_times().system
            self.prev_cpu_check = now
            self.lbl_cpu.config(text=f"{cpu_pct}%")
            rss = proc.memory_info().rss
            ram_mb = round(rss / (1024 * 1024), 1)
            total_ram = psutil.virtual_memory().total or 1
            ram_pct = round((rss / total_ram) * 100, 1)
            size_str = f"{ram_mb / 1024:.1f} GB" if ram_mb >= 1024 else f"{ram_mb} MB"
            self.lbl_ram.config(text=size_str)
            self.lbl_ram_pct.config(text=f"({ram_pct}%)")
            snap = ",".join(sorted(self.online_players))
            if snap != self.last_player_snapshot:
                self.last_player_snapshot = snap
                self.list_players.delete(0, tk.END)
                for p in sorted(self.online_players):
                    self.list_players.insert(tk.END, p)
            self.lbl_players_big.config(text=f"{len(self.online_players)} / {self.max_players}")
            if self.start_time:
                up = datetime.now() - self.start_time
                total_s = int(up.total_seconds())
                if total_s >= 3600:
                    # Minutes are within the current hour, not total minutes (up.seconds//60 is wrong).
                    up_str = f"{total_s // 3600}h {(total_s % 3600) // 60}m"
                else:
                    up_str = f"{total_s // 60}m {total_s % 60}s"
                self.lbl_uptime_big.config(text=up_str)
                self.lbl_uptime_hdr.config(text=f"Up: {up_str}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def _test_log_filter(self, line: str, flt: str) -> bool:
        low = line.lower()
        if flt == "All":
            return True
        if flt == "Players":
            return bool(
                re.search(r"join succeeded|leave:|saidfarewell|disconnectaccount", low)
            )
        if flt == "Warn":
            return "warning" in low
        if flt == "Errors":
            return "error" in low or "fatal" in low
        return True

    def _log_line_tag(self, line: str) -> str:
        low = line.lower()
        if "error" in low or "fatal" in low:
            return "err"
        if "warning" in low:
            return "warn"
        if "join succeeded" in low:
            return "join"
        if re.search(r"leave:|saidfarewell|disconnectaccount", low):
            return "leave"
        return "def"

    def _update_log_viewer(self) -> None:
        lp = self.paths.log_path
        if not lp.is_file():
            return
        try:
            with open(lp, "rb") as f:
                f.seek(self.log_position)
                chunk = f.read()
                self.log_position = f.tell()
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            new_lines = [ln for ln in text.splitlines() if ln]
            if not new_lines and text.endswith("\n"):
                pass
            for line in new_lines:
                self.log_buffer.append(line)

                def hist_join(n: str) -> None:
                    self._on_player_join(n)
                    self._add_history(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] JOINED: {n}"
                    )

                def hist_leave(n: str, sfx: str) -> None:
                    self._on_player_leave(n)
                    self._add_history(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] LEFT: {n}{sfx}"
                    )

                players.process_log_line_for_players(
                    line,
                    online=self.online_players,
                    account_to_player=self.account_to_player,
                    on_join_history=hist_join,
                    on_leave_history=hist_leave,
                )
                if self._test_log_filter(line, self.log_filter):
                    self.txt_log.insert(tk.END, line + "\n", self._log_line_tag(line))
            while len(self.log_buffer) > 1000:
                self.log_buffer.pop(0)
            while int(self.txt_log.index("end-1c").split(".")[0]) > 1000:
                self.txt_log.delete("1.0", "2.0")
            if self.var_autoscroll_log.get():
                self.txt_log.see(tk.END)
        except OSError:
            pass

    def _refresh_log_filter(self) -> None:
        self.txt_log.delete("1.0", tk.END)
        for line in self.log_buffer:
            if self._test_log_filter(line, self.log_filter):
                self.txt_log.insert(tk.END, line + "\n", self._log_line_tag(line))
        if self.var_autoscroll_log.get():
            self.txt_log.see(tk.END)

    def _set_log_filter(self, name: str, active_btn: tk.Button) -> None:
        self.log_filter = name
        for b in (self.btn_log_all, self.btn_log_pl, self.btn_log_warn, self.btn_log_err):
            b.config(bg=self.c["blue_btn"] if b is active_btn else self.c["gray_btn"])
        self._refresh_log_filter()

    def _refresh_player_list(self) -> None:
        online, acct = players.replay_full_log(self.paths.log_path)
        self.online_players = online
        self.account_to_player = acct
        self.list_players.delete(0, tk.END)
        for p in sorted(self.online_players):
            self.list_players.insert(tk.END, p)
        self.lbl_players_big.config(text=f"{len(self.online_players)} / {self.max_players}")

    def _add_history(self, entry: str) -> None:
        self.list_history.insert(tk.END, entry)
        try:
            with open(self.paths.history_file, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except OSError:
            pass

    def _load_history(self) -> None:
        if not self.paths.history_file.is_file():
            return
        try:
            lines = self.paths.history_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for ln in lines[-100:]:
                self.list_history.insert(tk.END, ln)
        except OSError:
            pass

    def _accumulate_hourly_seconds(self, start: datetime, end: datetime) -> None:
        if end <= start:
            return
        cur = start
        while cur < end:
            next_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            seg_end = next_hour if next_hour < end else end
            self._hourly_online_seconds[cur.hour] += (seg_end - cur).total_seconds()
            cur = seg_end

    def _on_player_join(self, name: str, when: datetime | None = None) -> None:
        ts = when or datetime.now()
        if name not in self._player_session_start_totals:
            self._player_session_start_totals[name] = ts
        if name not in self._player_session_start_hourly:
            self._player_session_start_hourly[name] = ts

    def _on_player_leave(self, name: str, when: datetime | None = None) -> None:
        ts = when or datetime.now()
        started_total = self._player_session_start_totals.pop(name, None)
        if started_total and ts > started_total:
            self._player_total_seconds[name] = self._player_total_seconds.get(name, 0.0) + (
                ts - started_total
            ).total_seconds()

        started_hour = self._player_session_start_hourly.pop(name, None)
        if started_hour and ts > started_hour:
            self._accumulate_hourly_seconds(started_hour, ts)

        self._save_insights_data()
        self._refresh_insights_ui()

    def _refresh_insights_ui(self) -> None:
        self.list_player_activity.delete(0, tk.END)
        if not self._player_total_seconds:
            self.list_player_activity.insert(tk.END, "No player session history yet.")
        else:
            ordered = sorted(self._player_total_seconds.items(), key=lambda kv: kv[1], reverse=True)
            for name, seconds in ordered:
                self.list_player_activity.insert(tk.END, f"{name} - {seconds / 3600.0:.2f} hours")

        self._draw_active_times_chart()
        self.list_active_times.delete(0, tk.END)
        ranked = sorted(
            [(hour, secs) for hour, secs in enumerate(self._hourly_online_seconds)],
            key=lambda x: x[1],
            reverse=True,
        )
        if not any(secs > 0 for _, secs in ranked):
            self.list_active_times.insert(tk.END, "No activity data yet.")
            self._refresh_insights_last_updated_labels()
            return
        for hour, secs in ranked:
            if secs <= 0:
                continue
            self.list_active_times.insert(
                tk.END,
                f"{hour:02d}:00 - {hour:02d}:59 - {secs / 3600.0:.2f} player-hours",
            )
        self._refresh_insights_last_updated_labels()

    def _draw_active_times_chart(self) -> None:
        cv = self.canvas_active_times
        cv.delete("all")
        self._active_times_chart_points = []
        self._hide_active_times_tooltip()
        width = max(cv.winfo_width(), 1)
        height = max(cv.winfo_height(), 1)
        left, right, top, bottom = 34, 10, 10, 24
        plot_w = width - left - right
        plot_h = height - top - bottom
        if plot_w <= 10 or plot_h <= 10:
            return

        vals = [max(0.0, s / 3600.0) for s in self._hourly_online_seconds]
        vmax = max(vals) if vals else 0.0

        # Axes
        axis = self.c["border_input"]
        cv.create_line(left, top, left, top + plot_h, fill=axis, width=1)
        cv.create_line(left, top + plot_h, left + plot_w, top + plot_h, fill=axis, width=1)

        # X labels (every 3 hours)
        for h in range(0, 24, 3):
            x = left + (h / 23.0) * plot_w
            cv.create_line(x, top + plot_h, x, top + plot_h + 4, fill=axis)
            cv.create_text(x, top + plot_h + 12, text=f"{h:02d}", fill=self.c["text_dim"], font=(None, 8))

        if vmax <= 0:
            cv.create_text(
                left + (plot_w / 2),
                top + (plot_h / 2),
                text="No activity data yet",
                fill=self.c["text_dim"],
                font=(None, 10),
            )
            return

        # Y max label
        cv.create_text(left - 4, top, text=f"{vmax:.1f}h", anchor="e", fill=self.c["text_dim"], font=(None, 8))

        pts: list[float] = []
        for i, val in enumerate(vals):
            x = left + (i / 23.0) * plot_w
            y = top + plot_h - ((val / vmax) * plot_h)
            pts.extend([x, y])
            self._active_times_chart_points.append((x, y, i, val))

        fill_poly = [left, top + plot_h] + pts + [left + plot_w, top + plot_h]
        cv.create_polygon(*fill_poly, fill="#1A3A5A", outline="")
        cv.create_line(*pts, fill="#5BA4CF", width=2, smooth=True)
        for x, y, _hour, _val in self._active_times_chart_points:
            cv.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#7CC3E8", outline="")

    def _on_active_times_chart_motion(self, event) -> None:
        if not self._active_times_chart_points:
            self._hide_active_times_tooltip()
            return
        nearest = min(
            self._active_times_chart_points,
            key=lambda p: ((p[0] - event.x) ** 2 + (p[1] - event.y) ** 2),
        )
        x, y, hour, val = nearest
        if ((x - event.x) ** 2 + (y - event.y) ** 2) > (14 * 14):
            self._hide_active_times_tooltip()
            return
        if self._active_times_tooltip_label is None:
            self._active_times_tooltip_label = tk.Label(
                self.canvas_active_times,
                bg="#1A2A3A",
                fg="#C0CDD8",
                bd=1,
                relief=tk.SOLID,
                padx=6,
                pady=3,
                font=(None, 9),
                justify=tk.LEFT,
            )
        self._active_times_tooltip_label.config(
            text=f"{hour:02d}:00 - {hour:02d}:59\n{val:.2f} player-hours"
        )
        tx = min(max(event.x + 12, 4), max(self.canvas_active_times.winfo_width() - 150, 4))
        ty = min(max(event.y - 34, 4), max(self.canvas_active_times.winfo_height() - 40, 4))
        self._active_times_tooltip_label.place(x=tx, y=ty)
        self._active_times_tooltip_label.lift()

    def _hide_active_times_tooltip(self, _event=None) -> None:
        if self._active_times_tooltip_label is not None:
            self._active_times_tooltip_label.place_forget()

    def _close_open_insight_sessions(self, when: datetime | None = None) -> None:
        ts = when or datetime.now()
        for name in list(self._player_session_start_totals.keys()):
            self._on_player_leave(name, ts)

    def _save_insights_data(self) -> None:
        self._insights_last_updated_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "PlayerTotalsSeconds": self._player_total_seconds,
            "HourlyOnlineSeconds": self._hourly_online_seconds,
            "LastUpdated": self._insights_last_updated_ts,
        }
        try:
            self.paths.insights_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _load_insights_data(self) -> None:
        self._player_total_seconds = {}
        self._hourly_online_seconds = [0.0] * 24
        self._insights_last_updated_ts = None
        if not self.paths.insights_file.is_file():
            self._refresh_insights_ui()
            return
        try:
            raw = json.loads(self.paths.insights_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            self._refresh_insights_ui()
            return

        totals = raw.get("PlayerTotalsSeconds") if isinstance(raw, dict) else None
        if isinstance(totals, dict):
            clean_totals: dict[str, float] = {}
            for name, secs in totals.items():
                if not str(name).strip():
                    continue
                try:
                    sec_val = max(0.0, float(secs))
                except (TypeError, ValueError):
                    continue
                clean_totals[str(name)] = sec_val
            self._player_total_seconds = clean_totals

        hours = raw.get("HourlyOnlineSeconds") if isinstance(raw, dict) else None
        if isinstance(hours, list):
            vals: list[float] = []
            for idx in range(24):
                try:
                    vals.append(max(0.0, float(hours[idx])))
                except (IndexError, TypeError, ValueError):
                    vals.append(0.0)
            self._hourly_online_seconds = vals
        last_updated = raw.get("LastUpdated") if isinstance(raw, dict) else None
        if isinstance(last_updated, str) and last_updated.strip():
            self._insights_last_updated_ts = last_updated.strip()

        self._refresh_insights_ui()

    def _refresh_insights_last_updated_labels(self) -> None:
        txt = f"Last updated: {self._insights_last_updated_ts}" if self._insights_last_updated_ts else "Last updated: --"
        self.lbl_player_activity_updated.config(text=txt)
        self.lbl_active_times_updated.config(text=txt)

    def _on_clear_player_activity_insights(self) -> None:
        if not messagebox.askyesno(
            "Clear Player Activity",
            "Are you sure you want to clear Player Activity totals?",
        ):
            self.log("Clear Player Activity canceled.")
            return
        self._player_total_seconds = {}
        now = datetime.now()
        self._player_session_start_totals = {p: now for p in self.online_players}
        self._save_insights_data()
        self._refresh_insights_ui()
        self.log("Player Activity cleared.")

    def _on_clear_active_times_insights(self) -> None:
        if not messagebox.askyesno(
            "Clear Most Active Times",
            "Are you sure you want to clear Most Active Times data?",
        ):
            self.log("Clear Most Active Times canceled.")
            return
        self._hourly_online_seconds = [0.0] * 24
        now = datetime.now()
        self._player_session_start_hourly = {p: now for p in self.online_players}
        self._save_insights_data()
        self._refresh_insights_ui()
        self.log("Most Active Times cleared.")

    def _save_settings(self) -> None:
        self.mgr.auto_restart = self.var_auto_restart.get()
        self.mgr.auto_backup = self.var_auto_backup.get()
        interval_value, interval_unit = self._read_backup_interval_from_ui()
        self.mgr.backup_interval_value = interval_value
        self.mgr.backup_interval_unit = interval_unit
        self.mgr.schedule_enabled = self.var_schedule.get()
        self.mgr.schedule_time = self.ent_schedule_time.get().strip()
        self._sync_discord_mgr_from_ui()
        settings.save_manager_settings(self.paths, self.mgr, self.client)

    def _sync_discord_mgr_from_ui(self) -> None:
        self.mgr.discord_webhook_enabled = self.var_discord_enabled.get()
        self.mgr.discord_webhook_url = self.ent_discord_url.get().strip()
        self.mgr.discord_msg_stop = (
            self.ent_discord_msg_stop.get().strip() or settings.DEFAULT_DISCORD_MSG_STOP
        )
        self.mgr.discord_msg_restart = (
            self.ent_discord_msg_restart.get().strip() or settings.DEFAULT_DISCORD_MSG_RESTART
        )
        self.mgr.discord_msg_schedule = (
            self.ent_discord_msg_schedule.get().strip() or settings.DEFAULT_DISCORD_MSG_SCHEDULE
        )
        self.mgr.discord_msg_crash = (
            self.ent_discord_msg_crash.get().strip() or settings.DEFAULT_DISCORD_MSG_CRASH
        )

    def _discord_maybe_send(self, content: str) -> None:
        self._sync_discord_mgr_from_ui()
        if not self.mgr.discord_webhook_enabled:
            return
        url = (self.mgr.discord_webhook_url or "").strip()
        if not discord_webhook.is_valid_discord_webhook_url(url):
            return
        msg = (content or "").strip()
        if not msg:
            return

        def worker() -> None:
            ok, err = discord_webhook.send_discord_webhook(url, msg)
            if not ok:
                self.root.after(0, lambda e=err: self.log(f"Discord webhook failed: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _discord_notify_crash(self) -> None:
        self._discord_maybe_send(
            self.ent_discord_msg_crash.get().strip() or settings.DEFAULT_DISCORD_MSG_CRASH
        )

    def _on_discord_test(self) -> None:
        self._sync_discord_mgr_from_ui()
        settings.save_manager_settings(self.paths, self.mgr, self.client)
        url = (self.mgr.discord_webhook_url or "").strip()
        if not discord_webhook.is_valid_discord_webhook_url(url):
            self.log("Discord: enter a valid https://discord.com/api/webhooks/... URL.")
            return

        def worker() -> None:
            ok, err = discord_webhook.send_discord_webhook(
                url, "Windrose Server Manager: **test** notification (webhook OK)."
            )
            if ok:
                self.root.after(0, lambda: self.log("Discord test message sent."))
            else:
                self.root.after(0, lambda e=err: self.log(f"Discord webhook failed: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _load_all_settings(self) -> None:
        self.mgr = settings.load_manager_settings(self.paths)
        self._update_crash_stat()
        self.var_auto_restart.set(self.mgr.auto_restart)
        self.var_auto_backup.set(self.mgr.auto_backup)
        self.ent_backup_interval.delete(0, tk.END)
        self.ent_backup_interval.insert(0, str(self.mgr.backup_interval_value))
        self.cmb_backup_unit.set("Minutes" if self.mgr.backup_interval_unit == "minutes" else "Hours")
        self.var_schedule.set(self.mgr.schedule_enabled)
        self.ent_schedule_time.delete(0, tk.END)
        self.ent_schedule_time.insert(0, self.mgr.schedule_time)
        self.var_discord_enabled.set(self.mgr.discord_webhook_enabled)
        self.ent_discord_url.delete(0, tk.END)
        self.ent_discord_url.insert(0, self.mgr.discord_webhook_url)
        for ent, val in (
            (self.ent_discord_msg_stop, self.mgr.discord_msg_stop),
            (self.ent_discord_msg_restart, self.mgr.discord_msg_restart),
            (self.ent_discord_msg_schedule, self.mgr.discord_msg_schedule),
            (self.ent_discord_msg_crash, self.mgr.discord_msg_crash),
        ):
            ent.delete(0, tk.END)
            ent.insert(0, val)
        if self.mgr.steamcmd_force_install_dir:
            self.client.steamcmd_force_install_dir = self.mgr.steamcmd_force_install_dir
            settings.sync_steamcmd_sidecar(
                self.client.install_client,
                self.client.steamcmd_force_install_dir,
                self.paths.steamcmd_sidecar,
            )

    def _post_load_init(self) -> None:
        self.lbl_cur_ver.config(text=f"Current version: {constants.APP_VERSION}")
        self.lbl_version_corner.config(text=f"Version: {constants.APP_VERSION}")
        self.ent_install_dest.delete(0, tk.END)
        self.ent_install_dest.insert(0, str(self.paths.server_dir))
        if self.client.install_client == "SteamCMD" and self.client.steamcmd_force_install_dir:
            self.ent_install_dest.delete(0, tk.END)
            self.ent_install_dest.insert(0, self.client.steamcmd_force_install_dir)
        z = find_latest_backup(self.paths)
        if z:
            stamp = z.stem.replace("Backup_", "")
            self.lbl_last_backup.config(text=f"Last backup: {stamp}")
        self._read_server_config_ui()
        self._read_world_config_ui()
        self._load_history()
        self._load_insights_data()
        self._update_setup_wizard()
        if self.client.install_client != "SteamCMD":
            det = self._find_steam_windrose()
            if not det and self._initial_detect:
                det = self._initial_detect
            if det:
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, str(det))
        ex = process_ops.get_server_process()
        if ex:
            self.start_time = datetime.fromtimestamp(ex.create_time())
            try:
                ct = ex.cpu_times()
                self.prev_cpu_time = ct.user + ct.system
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.prev_cpu_time = None
            self.prev_cpu_check = datetime.now()
            self._set_ui_running()
            self._update_log_viewer()
            self.log("Attached to running server.")
        else:
            self._set_ui_stopped()
        if self.var_auto_backup.get():
            self._start_auto_backup_timer()

    def _on_close(self) -> None:
        self._close_open_insight_sessions()
        self._save_settings()
        self.root.destroy()

    def _steamcmd_force_path(self) -> Path | None:
        raw = self.ent_install_dest.get().strip()
        return Path(raw) if raw else None

    def _find_steam_windrose(self):
        fd = self.client.steamcmd_force_install_dir
        force = Path(fd) if fd else None
        return steam.find_steam_windrose(
            self.client.install_client,
            steam_install_root=Path(self.client.steam_install_root) if self.client.steam_install_root else None,
            steamcmd_install_root=Path(self.client.steamcmd_install_root) if self.client.steamcmd_install_root else None,
            steamcmd_force_install_dir=force,
        )

    def _initialize_install_and_server_locations(self) -> Path | None:
        # 1) Prefer launcher-level settings first.
        bootstrap_file = _bootstrap_client_settings_path()
        if bootstrap_file.is_file():
            try:
                raw = json.loads(bootstrap_file.read_text(encoding="utf-8"))
                if raw.get("InstallClientChoiceSaved") and raw.get("InstallClient") in ("Steam", "SteamCMD"):
                    self.client.install_client_choice_saved = True
                    self.client.install_client = str(raw.get("InstallClient"))
                    self.client.steam_install_root = raw.get("SteamInstallRoot")
                    self.client.steamcmd_install_root = raw.get("SteamCmdInstallRoot")
                    self.client.steamcmd_force_install_dir = raw.get("SteamCmdForceInstallDir")
                    self.client.server_root = raw.get("ServerRoot")
            except (OSError, json.JSONDecodeError, TypeError):
                pass

        # 2) Merge with per-server settings if present.
        cs = settings.read_client_settings(self.paths)
        if cs:
            self.client = cs
            if self.client.server_root:
                sr = Path(self.client.server_root)
                if (sr / "WindroseServer.exe").is_file():
                    self.paths.set_root(sr)
                    self.paths.ensure_backup_dir()
                    return sr
        else:
            r = messagebox.askyesnocancel(
                "Choose Client Type",
                "Which client are you using to host your Windrose server?\n\n"
                "Yes = Steam\nNo = SteamCMD\nCancel = Skip setup",
            )
            if r is True:
                self.client.install_client = "Steam"
                self.client.install_client_choice_saved = True
            elif r is False:
                self.client.install_client = "SteamCMD"
                self.client.install_client_choice_saved = True
            else:
                self.client.install_client = "Steam"
                self.client.install_client_choice_saved = False
            settings.save_client_settings(self.paths, self.client)
            self._save_bootstrap_client_settings()

        if self.client.install_client == "Steam":
            self.client.steamcmd_install_root = None
            if not self.client.steam_install_root:
                sr = steam.get_steam_install_root()
                if sr:
                    self.client.steam_install_root = str(sr)
            if not self.client.steam_install_root:
                d = filedialog.askdirectory(title="Select Steam install folder (contains steam.exe)")
                if d:
                    self.client.steam_install_root = d
        else:
            self.client.steam_install_root = None
            if not self.client.steamcmd_install_root:
                cr = steam.get_steamcmd_install_root(_app_package_dir().parent)
                if cr:
                    self.client.steamcmd_install_root = str(cr)
            if not self.client.steamcmd_install_root:
                r = messagebox.askyesno(
                    "SteamCMD Setup",
                    "Do you already have SteamCMD installed?\n\n"
                    "Yes = select folder\nNo = download SteamCMD now",
                )
                if r:
                    d = filedialog.askdirectory(title="Select SteamCMD folder (contains steamcmd.exe)")
                    if d and (Path(d) / "steamcmd.exe").is_file():
                        self.client.steamcmd_install_root = d
                        settings.save_client_settings(self.paths, self.client)
                        self._save_bootstrap_client_settings()
                else:
                    d = filedialog.askdirectory(title="Where should SteamCMD be installed?")
                    if d:
                        inst = install_ops.install_steamcmd_from_official_zip(Path(d))
                        if inst:
                            self.client.steamcmd_install_root = str(inst)
                            settings.save_client_settings(self.paths, self.client)
                            self._save_bootstrap_client_settings()

        side = settings.import_steamcmd_force_from_sidecar(self.paths.steamcmd_sidecar)
        if side:
            self.client.steamcmd_force_install_dir = side

        found = self._find_any_windrose()
        if not found and self.paths.server_exe.is_file():
            found = self.paths.server_dir
        if not found:
            if self.client.install_client == "Steam":
                if messagebox.askyesno(
                    "Server Files Not Found",
                    "Could not auto-detect Windrose server files. Do you already have server files installed?",
                ):
                    d = filedialog.askdirectory(title="Select WindowsServer folder (contains WindroseServer.exe)")
                    if d and (Path(d) / "WindroseServer.exe").is_file():
                        found = Path(d)
            else:
                # SteamCMD-specific first-run guidance.
                has_existing = messagebox.askyesno(
                    "SteamCMD Server Files",
                    "Do you already have the Windrose server files installed?\n\n"
                    "Yes - Select your existing Windrose Server folder (contains WindroseServer.exe).\n"
                    "No - Select the folder where you want Windrose Server files installed via SteamCMD.",
                )
                if has_existing:
                    d = filedialog.askdirectory(
                        title="Select Windrose Server folder (contains WindroseServer.exe)"
                    )
                    if d and (Path(d) / "WindroseServer.exe").is_file():
                        found = Path(d)
                else:
                    d = filedialog.askdirectory(
                        title="Select destination for Windrose server files (SteamCMD)"
                    )
                    if d:
                        self.client.steamcmd_force_install_dir = d
                        settings.save_client_settings(self.paths, self.client)
                        self._save_bootstrap_client_settings()
        if found:
            self.paths.set_root(found)
            self.paths.ensure_backup_dir()
            self.client.server_root = str(found)
            settings.save_client_settings(self.paths, self.client)
            self._save_bootstrap_client_settings()
            return found
        return None

    def _save_bootstrap_client_settings(self) -> None:
        p = _bootstrap_client_settings_path()
        payload = {
            "InstallClientChoiceSaved": self.client.install_client_choice_saved,
            "InstallClient": self.client.install_client,
            "SteamInstallRoot": self.client.steam_install_root,
            "SteamCmdInstallRoot": self.client.steamcmd_install_root,
            "SteamCmdForceInstallDir": self.client.steamcmd_force_install_dir,
            "ServerRoot": self.client.server_root,
        }
        try:
            p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _find_any_windrose(self) -> Path | None:
        if self.paths.server_exe.is_file():
            return self.paths.server_dir
        w = self._find_steam_windrose()
        return w

    def _update_setup_wizard(self) -> None:
        if self.client.install_client == "SteamCMD":
            self.lbl_req_help.config(
                text=(
                    "Windrose must be installed via SteamCMD (App ID 4129620). "
                    "Be sure to specify your SteamCMD Location and Server Location below. "
                    "Then click on \"Install Server.\" After it completes click on \"Re-check\""
                )
            )
            self.lbl_steam_source.config(text="SteamCMD Location")
            cmd = self.client.steamcmd_install_root or str(
                steam.get_steamcmd_install_root(_app_package_dir().parent) or ""
            )
            if cmd:
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, cmd)
        else:
            self.lbl_req_help.config(
                text=(
                    "Windrose must be installed via Steam (App ID 4129620). "
                    "The dedicated server files are bundled inside the game - no separate download needed."
                )
            )
            self.lbl_steam_source.config(text="Steam Source")
        self.lbl_client_mode.config(text=f"Current mode: {self.client.install_client}")

        steam_found = self._find_steam_windrose() is not None or self.paths.server_exe.is_file()
        server_ready = self.paths.server_exe.is_file()
        def set_step(i: int, badge_bg: str, badge_txt: str, status: str | None, status_fg: str | None):
            badge, st = self._step_headers[i]
            badge.config(bg=badge_bg, text=badge_txt)
            if st:
                st.config(text=status or "", fg=status_fg or self.c["text_dim"])

        g, b, gr, r_ = self.c["green_btn"], self.c["blue_btn"], self.c["gray_btn"], self.c["red"]
        fg_ok, fg_muted = self.c["green"], self.c["text_dim"]

        if steam_found:
            set_step(0, g, "\u2713", "Ready", fg_ok)
            self.lbl_req_steam.config(
                text=f"\u2713 Windrose found ({self.client.install_client})", fg=fg_ok
            )
        else:
            set_step(0, b, "1", "Action needed", r_)
            msg = (
                "\u2717 Windrose not found - install/update with SteamCMD app_update 4129620"
                if self.client.install_client == "SteamCMD"
                else "\u2717 Windrose not found - install it via Steam first (App ID 4129620)"
            )
            self.lbl_req_steam.config(text=msg, fg=r_)

        if server_ready:
            set_step(1, g, "\u2713", "Installed", fg_ok)
            self.btn_install_server.config(
                text=(
                    "Update Server with SteamCMD"
                    if self.client.install_client == "SteamCMD"
                    else "Update Server"
                )
            )
            self.lbl_install_warn.config(text="")
        elif steam_found:
            set_step(1, b, "2", "Ready to install", fg_muted)
            self.btn_install_server.config(text="Install Server")
            self.lbl_install_warn.config(text="")
        else:
            set_step(1, gr, "2", "Complete step 1 first", fg_muted)
            self.btn_install_server.config(text="Install Server")
            self.lbl_install_warn.config(text="")

        if not self.paths.server_exe.is_file():
            self.canvas_install.itemconfig(self._install_dot, fill=self.c["red"])
            self.lbl_install_status.config(text="Not installed - see Install tab", fg=self.c["red"])
            self.nb.select(self.nb.tabs()[4])
        self._apply_install_update_button_state()

    def _apply_install_update_button_state(self) -> None:
        if process_ops.get_server_process():
            self.btn_install_server.config(state=tk.DISABLED)
            self._install_blocked_by_running = True
            self.tip_install_server.text = (
                "You cannot update the server while it's running. Be sure to stop the server first."
            )
            return
        if self._install_blocked_by_running:
            self.btn_install_server.config(state=tk.NORMAL)
            self._install_blocked_by_running = False
        self.tip_install_server.text = ""

    def _read_server_config_ui(self) -> None:
        data = config_io.read_server_config_dict(self.paths)
        if not data:
            return
        inner = data.get("ServerDescription_Persistent") or {}
        if inner.get("ServerName"):
            self.ent_srv_name.delete(0, tk.END)
            self.ent_srv_name.insert(0, str(inner["ServerName"]))
            self.lbl_server_title.config(text=str(inner["ServerName"]))
        if inner.get("MaxPlayerCount") is not None:
            try:
                val = int(inner["MaxPlayerCount"])
                val = max(1, min(20, val))
                self.scale_max.set(val)
                self.lbl_max_val.config(text=str(val))
                self.max_players = val
            except (TypeError, ValueError):
                pass
        prot = bool(inner.get("IsPasswordProtected"))
        self.var_pw_en.set(prot)
        self.ent_invite_code.delete(0, tk.END)
        self.ent_invite_code.insert(0, str(inner.get("InviteCode", "")))
        # Entry may be readonly/disabled at startup; temporarily enable to load text.
        prev_state = str(self.ent_password.cget("state"))
        self.ent_password.config(state=tk.NORMAL)
        self.ent_password.delete(0, tk.END)
        if inner.get("Password"):
            # Keep password populated across restarts; Entry remains masked.
            self.ent_password.insert(0, str(inner["Password"]))
        if prev_state in ("disabled", "readonly"):
            self.ent_password.config(state=prev_state)
        if inner.get("P2pProxyAddress"):
            self.ent_proxy.delete(0, tk.END)
            self.ent_proxy.insert(0, str(inner["P2pProxyAddress"]))
        self.ent_direct_port.delete(0, tk.END)
        self.ent_direct_port.insert(0, str(inner.get("DirectConnectionServerPort", 7777)))
        self._toggle_pw_entry()

    def _read_world_config_ui(self) -> None:
        wp = config_io.find_world_config(self.paths)
        if not wp:
            self._set_world_settings_enabled(False)
            return
        j = config_io.read_world_config_dict(wp)
        if not j:
            self._set_world_settings_enabled(False)
            return
        self._set_world_settings_enabled(True)
        wd = j.get("WorldDescription") or {}
        preset = wd.get("WorldPresetType") or "Custom"
        if preset in self.cmb_preset["values"]:
            self.cmb_preset.set(preset)
        if preset == "Custom":
            self.frame_custom.grid()
        ws = wd.get("WorldSettings") or {}
        floats = config_io.extract_floats_from_world(ws)
        for k, (sc, lbl) in self._world_sliders.items():
            if k in floats:
                sc.set(floats[k])
                lbl.config(text=f"{floats[k]:.1f}")
        bools = config_io.extract_bools_from_world(ws)
        self.var_coop_quests.set(bools.get("coop_quests", False))
        self.var_easy_explore.set(bools.get("easy_explore", False))
        cd = config_io.parse_combat_from_world(wd)
        if cd in self.cmb_combat["values"]:
            self.cmb_combat.set(cd)

    def _set_world_settings_enabled(self, enabled: bool) -> None:
        self._last_world_settings_enabled = enabled
        preset_state = "readonly" if enabled else "disabled"
        combat_state = "readonly" if enabled else "disabled"
        self.cmb_preset.config(state=preset_state)
        self.cmb_combat.config(state=combat_state)
        for slider, _lbl in self._world_sliders.values():
            slider.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self.chk_coop_quests.state(["!disabled"] if enabled else ["disabled"])
        self.chk_easy_explore.state(["!disabled"] if enabled else ["disabled"])
        self.lbl_world_missing.grid() if not enabled else self.lbl_world_missing.grid_remove()

    def _start_server_process(self) -> None:
        exe = (
            self.paths.server_exe_direct
            if self.paths.server_exe_direct.is_file()
            else self.paths.server_exe
        )
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Use the shipping exe folder as the working directory when possible.
        # If cwd is the manager install tree (or any folder that contains unrelated MSVC
        # runtimes), Windows DLL search can pick up *_internal\\VCRUNTIME140.dll from the
        # Server Manager and lock it — which breaks in-place self-updates while the server runs.
        cwd = str(exe.parent if exe.is_file() else self.paths.server_dir)
        switched, meipass = _reset_windows_dll_directory_for_child_launch()
        try:
            self.server_popen = subprocess.Popen(
                [str(exe)],
                cwd=cwd,
                env=_isolated_dedicated_server_env(cwd),
                creationflags=creation,
            )
        finally:
            if switched:
                _restore_windows_dll_directory_after_child_launch(meipass)

    def _poll_invite_code(self) -> None:
        self._poll_invite_count += 1
        code = config_io.read_invite_code(self.paths)
        if code:
            self.lbl_invite.config(text=code)
            return
        if self._poll_invite_count < 24:
            self.root.after(5000, self._poll_invite_code)

    def _on_start(self) -> None:
        self._stop_pending = False
        self._stop_pending_logged = False
        if not self.paths.server_exe.is_file():
            self.log("Server not installed.")
            return
        try:
            self.log_position = 0
            self.log_buffer.clear()
            self.online_players.clear()
            self.account_to_player.clear()
            self._start_server_process()
            self.start_time = datetime.now()
            if self.server_popen:
                try:
                    p = psutil.Process(self.server_popen.pid)
                    ct = p.cpu_times()
                    self.prev_cpu_time = ct.user + ct.system
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self.prev_cpu_time = None
            self.prev_cpu_check = datetime.now()
            self._set_ui_running()
            self.log("Server started.")
            self._poll_invite_count = 0
            self._poll_invite_code()
        except OSError as e:
            self.log(f"Failed to start: {e}")

    def _on_stop(self) -> None:
        self._stop_pending = True
        self._stop_pending_logged = False
        self._restart_pending = False
        self._discord_maybe_send(
            self.ent_discord_msg_stop.get().strip() or settings.DEFAULT_DISCORD_MSG_STOP
        )
        process_ops.stop_all_server_processes()
        self.server_popen = None
        self.start_time = None
        self._set_ui_stopped()
        self._read_world_config_ui()
        self.log("Server stopped.")

    def _do_restart(self, reason: str = "manual") -> None:
        self._restart_pending = True
        self._stop_pending = False
        self._stop_pending_logged = False
        if reason == "manual":
            self._discord_maybe_send(
                self.ent_discord_msg_restart.get().strip() or settings.DEFAULT_DISCORD_MSG_RESTART
            )
        elif reason == "schedule":
            self._discord_maybe_send(
                self.ent_discord_msg_schedule.get().strip() or settings.DEFAULT_DISCORD_MSG_SCHEDULE
            )
        process_ops.stop_all_server_processes()
        self.root.after(1500, self._restart_after_kill)

    def _restart_after_kill(self) -> None:
        try:
            self._on_start()
            self.log_position = 0
            self.log_buffer.clear()
            self.online_players.clear()
            self.log("Server restarted.")
        finally:
            self._restart_pending = False

    def _on_restart(self) -> None:
        self._do_restart("manual")

    def _on_open_folder(self) -> None:
        subprocess.Popen(["explorer", str(self.paths.server_dir)])

    def _copy_invite(self) -> None:
        code = self.lbl_invite.cget("text")
        if code not in ("--", "(pending...)"):
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.log("Invite code copied to clipboard.")

    def _on_share(self) -> None:
        code = self.lbl_invite.cget("text")
        name = self.lbl_server_title.cget("text")
        if code not in ("--", "(pending...)"):
            msg = f"Join my Windrose server '{name}'! Invite code: {code}"
            self.root.clipboard_clear()
            self.root.clipboard_append(msg)
            self.log("Share message copied to clipboard.")
        else:
            self.log("No invite code available yet.")

    def _on_save_config(self) -> None:
        if process_ops.get_server_process():
            self.lbl_cfg_status.config(text="Stop the server before saving config.", fg="tomato")
            return
        try:
            existing = config_io.read_server_config_dict(self.paths) or {}
            inner_old = existing.get("ServerDescription_Persistent") or {}
            inner: dict = {}
            for field in (
                "PersistentServerId",
                "WorldIslandId",
                "UserSelectedRegion",
                "UseDirectConnection",
                "DirectConnectionServerAddress",
                "DirectConnectionProxyAddress",
            ):
                if field in inner_old:
                    inner[field] = inner_old[field]
            try:
                direct_port = int((self.ent_direct_port.get() or "7777").strip())
            except ValueError:
                direct_port = 7777
            pw_text = self.ent_password.get()
            # If a password is specified, force protection on to avoid mismatched config state.
            pw_enabled = bool(pw_text.strip())
            inner["IsPasswordProtected"] = pw_enabled
            inner["Password"] = pw_text
            inner["ServerName"] = self.ent_srv_name.get()
            inner["InviteCode"] = self.ent_invite_code.get().strip()
            inner["MaxPlayerCount"] = int(round(self.scale_max.get()))
            inner["P2pProxyAddress"] = self.ent_proxy.get()
            inner["DirectConnectionServerPort"] = max(1, min(65535, direct_port))
            root = {
                "Version": existing.get("Version", 1),
                "DeploymentId": existing.get("DeploymentId", ""),
                "ServerDescription_Persistent": inner,
            }
            self.paths.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.paths.config_path.write_text(json.dumps(root, indent=2), encoding="utf-8")
            self.lbl_server_title.config(text=self.ent_srv_name.get())
            self.var_pw_en.set(pw_enabled)
            self._toggle_pw_entry()
            wpath = config_io.find_world_config(self.paths)
            if wpath:
                existing_world = config_io.read_world_config_dict(wpath)
                floats = {k: float(sl.get()) for k, (sl, _) in self._world_sliders.items()}
                bools = {"coop_quests": self.var_coop_quests.get(), "easy_explore": self.var_easy_explore.get()}
                payload = config_io.build_world_save_payload(
                    paths=self.paths,
                    preset=self.cmb_preset.get(),
                    combat_short=self.cmb_combat.get(),
                    floats=floats,
                    bools=bools,
                    existing_world=existing_world,
                )
                wpath.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            else:
                self.lbl_cfg_status.config(
                    text=(
                        "Server Settings saved.\nWorld settings were not saved because WorldDescription.json "
                        "does not exist yet."
                    ),
                    fg=self.c["accent"],
                )
                return
            ts = datetime.now().strftime("%H:%M:%S")
            self.lbl_cfg_status.config(text=f"Config saved at {ts}.", fg=self.c["green"])
        except OSError as e:
            self.lbl_cfg_status.config(text=f"Error: {e}", fg="tomato")

    def _on_reload_config(self) -> None:
        self._read_server_config_ui()
        self._read_world_config_ui()
        self.lbl_cfg_status.config(text="Config reloaded from disk.", fg=self.c["text_dim"])

    def _on_open_world_json(self) -> None:
        wp = config_io.find_world_config(self.paths)
        if wp and wp.is_file():
            subprocess.Popen(["notepad", str(wp)])
        else:
            self.log("WorldDescription.json not found.")

    def _on_open_server_config(self) -> None:
        if self.paths.config_path.is_file():
            subprocess.Popen(["notepad", str(self.paths.config_path)])
        else:
            self.log("ServerDescription.json not found.")

    def _on_export_logs(self) -> None:
        p = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log", "*.log"), ("Text", "*.txt"), ("All", "*.*")],
            initialfile=f"Windrose-Log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log",
        )
        if not p:
            return
        try:
            if self.paths.log_path.is_file():
                shutil.copy(self.paths.log_path, p)
            else:
                Path(p).write_text("\n".join(self.log_buffer), encoding="utf-8")
            self.log(f"Logs exported to: {p}")
        except OSError as e:
            self.log(f"Export error: {e}")

    def _read_backup_interval_from_ui(self) -> tuple[int, str]:
        raw = (self.ent_backup_interval.get() or "").strip()
        try:
            n = int(raw)
        except ValueError:
            n = 4
        n = max(1, n)
        unit = "minutes" if self.cmb_backup_unit.get().strip().lower().startswith("minute") else "hours"
        return n, unit

    def _backup_interval_seconds(self) -> int:
        n, unit = self._read_backup_interval_from_ui()
        return n * 60 if unit == "minutes" else n * 3600

    def _backup_interval_display(self) -> str:
        n, unit = self._read_backup_interval_from_ui()
        label = "minute" if unit == "minutes" else "hour"
        return f"{n} {label}" + ("" if n == 1 else "s")

    def _on_backup(self) -> None:
        if not self.paths.saves_base.is_dir():
            self.log(f"Saves folder not found: {self.paths.saves_base}")
            return
        try:
            stamp, zp = backup_saves_now(self.paths)
            self.lbl_last_backup.config(text=f"Last backup: {stamp}")
            self.log(f"Backup created: {zp}")
        except OSError as e:
            self.log(f"Backup error: {e}")

    def _on_open_backups(self) -> None:
        subprocess.Popen(["explorer", str(self.paths.backup_dir)])

    def _on_auto_backup_toggle(self) -> None:
        if self.var_auto_backup.get():
            self._start_auto_backup_timer()
        else:
            self._stop_auto_backup_timer()

    def _reschedule_auto_backup(self) -> None:
        if self.var_auto_backup.get():
            self._start_auto_backup_timer()

    def _start_auto_backup_timer(self) -> None:
        if self._auto_backup_after:
            self.root.after_cancel(self._auto_backup_after)
            self._auto_backup_after = None
        seconds = self._backup_interval_seconds()
        ms = int(seconds * 1000)

        def tick():
            if not self.paths.saves_base.is_dir():
                self.log("Auto-backup skipped: saves folder not found.")
            else:
                try:
                    stamp, zp = backup_saves_now(self.paths)
                    self.lbl_last_backup.config(text=f"Last backup: {stamp} (auto)")
                    self.log(f"Auto-backup created: {zp}")
                except OSError as e:
                    self.log(f"Auto-backup error: {e}")
            nxt = datetime.now() + timedelta(seconds=self._backup_interval_seconds())
            self.lbl_next_backup.config(text=f"Next auto-backup: {nxt.strftime('%I:%M %p')}")
            self._auto_backup_after = self.root.after(ms, tick)

        self._auto_backup_after = self.root.after(ms, tick)
        nxt = datetime.now() + timedelta(seconds=seconds)
        self.lbl_next_backup.config(text=f"Next auto-backup: {nxt.strftime('%I:%M %p')}")
        self.log(f"Auto-backup enabled: every {self._backup_interval_display()}")

    def _stop_auto_backup_timer(self) -> None:
        if self._auto_backup_after:
            self.root.after_cancel(self._auto_backup_after)
            self._auto_backup_after = None
        self.lbl_next_backup.config(text="")
        self.log("Auto-backup disabled.")

    def _on_clear_history(self) -> None:
        if not messagebox.askyesno(
            "Clear Player Connection History",
            "Are you sure you want to clear Player Connection History?"
        ):
            self.log("Clear history canceled.")
            return
        self.list_history.delete(0, tk.END)
        try:
            if self.paths.history_file.is_file():
                self.paths.history_file.unlink()
        except OSError:
            pass
        self.log("History cleared.")

    def _on_refresh_players(self) -> None:
        if process_ops.get_server_process():
            self._refresh_player_list()
        else:
            self.list_players.delete(0, tk.END)
            self.lbl_players_big.config(text=f"0 / {self.max_players}")

    def _on_check_update(self) -> None:
        self.lbl_update_status.config(text="Checking for updates...", fg=self.c["text_dim"])
        self._update_op_result = None

        def work() -> None:
            def report(msg: str) -> None:
                self.root.after(
                    0,
                    lambda m=msg: self.lbl_update_status.config(text=m, fg=self.c["text_dim"]),
                )

            self._update_op_result = updater.run_update_pipeline(constants.APP_VERSION, report)

        self._update_op_thread = threading.Thread(target=work, daemon=True)
        self._update_op_thread.start()
        self.root.after(500, self._poll_update_apply)

    def _poll_update_apply(self) -> None:
        if self._update_op_thread and self._update_op_thread.is_alive():
            self.root.after(500, self._poll_update_apply)
            return
        self._update_op_thread = None
        res = self._update_op_result
        self._update_op_result = None
        if not res:
            self.lbl_update_status.config(text="Error: No result from update check.", fg="tomato")
            return
        if not res.get("ok"):
            self.lbl_update_status.config(text=f"Error: {res.get('error', 'Unknown error')}", fg="tomato")
            return
        if res.get("action") == "uptodate":
            remote = res.get("remote", "")
            self.lbl_update_status.config(
                text=f"You are up to date. (Latest release is v{remote})",
                fg=self.c["green"],
            )
            return
        if res.get("action") == "ready":
            remote = str(res.get("remote", ""))
            payload: Path = res["payload"]
            work: Path = res["work"]
            self.lbl_update_status.config(
                text=f"Version {remote} downloaded. The application will restart to finish the update.",
                fg=self.c["green"],
            )
            messagebox.showinfo(
                "Update",
                f"Version {remote} will be installed when this application closes.\n\n"
                "The Windrose Server Manager will restart automatically.",
            )
            ok, err = updater.spawn_deferred_update(payload, work)
            if not ok:
                messagebox.showerror("Update failed", err or "Could not start the update helper.")
                self.lbl_update_status.config(text=f"Error: {err}", fg="tomato")
                shutil.rmtree(work, ignore_errors=True)
                return
            self.root.destroy()
            return
        self.lbl_update_status.config(text="Error: Unexpected update response.", fg="tomato")

    def _on_patch_notes(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("Patch Notes")
        top.geometry("520x520")
        top.configure(bg=self.c["bg"])
        cv = tk.Canvas(top, bg=self.c["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(top, command=cv.yview)
        fr = tk.Frame(cv, bg=self.c["bg"])
        fr.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=fr, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        pad = tk.Frame(fr, bg=self.c["bg"])
        pad.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)
        for ver in constants.PATCH_NOTES:
            box = self._panel_frame(pad)
            box.pack(fill=tk.X, pady=(0, 8))
            inner = tk.Frame(box, bg=self.c["bg_panel"])
            inner.pack(fill=tk.X, padx=12, pady=10)
            tk.Label(inner, text=f"Version {ver}", font=(None, 11, "bold"), fg=self.c["accent"], bg=self.c["bg_panel"]).pack(anchor="w")
            for line in constants.PATCH_NOTES[ver]:
                tk.Label(
                    inner, text=f"  - {line}", fg=self.c["text"], bg=self.c["bg_panel"], font=(None, 10), wraplength=460, justify=tk.LEFT
                ).pack(anchor="w", pady=2)

    def _on_switch_client(self) -> None:
        r = messagebox.askyesnocancel(
            "Switch Client Mode",
            "Switch hosting client mode?\n\nYes = Steam\nNo = SteamCMD\nCancel = keep current",
        )
        if r is None:
            return
        if r:
            self.client.install_client = "Steam"
            if not self.client.steam_install_root:
                sr = steam.get_steam_install_root()
                if sr:
                    self.client.steam_install_root = str(sr)
            if not self.client.steam_install_root:
                d = filedialog.askdirectory(title="Select Steam install folder")
                if d:
                    self.client.steam_install_root = d
        else:
            self.client.install_client = "SteamCMD"
            if not self.client.steamcmd_install_root:
                cr = steam.get_steamcmd_install_root(_app_package_dir().parent)
                if cr:
                    self.client.steamcmd_install_root = str(cr)
            if not self.client.steamcmd_install_root:
                d = filedialog.askdirectory(title="Select SteamCMD folder")
                if d:
                    self.client.steamcmd_install_root = d
        self.client.install_client_choice_saved = True
        settings.save_client_settings(self.paths, self.client)
        self._save_bootstrap_client_settings()
        self._update_setup_wizard()

    def _on_detect_steam(self) -> None:
        if self.client.install_client == "SteamCMD":
            td = self.ent_install_dest.get().strip()
            if td:
                self.client.steamcmd_force_install_dir = td
                settings.save_client_settings(self.paths, self.client)
                self._save_bootstrap_client_settings()
        found = self._find_steam_windrose()
        if found:
            if self.client.install_client == "SteamCMD":
                cmd = self.client.steamcmd_install_root or str(
                    steam.get_steamcmd_install_root(_app_package_dir().parent) or ""
                )
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, cmd)
            else:
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, str(found))
            self.txt_install_log.delete("1.0", tk.END)
            self.txt_install_log.insert(tk.END, f"Found: {found}")
        else:
            self.txt_install_log.delete("1.0", tk.END)
            self.txt_install_log.insert(
                tk.END,
                "Could not auto-detect Windrose in SteamCMD libraries."
                if self.client.install_client == "SteamCMD"
                else "Could not auto-detect Windrose in Steam libraries.",
            )

    def _on_browse_source(self) -> None:
        if self.client.install_client == "SteamCMD":
            d = filedialog.askdirectory(title="Select SteamCMD folder (steamcmd.exe)")
            if d and (Path(d) / "steamcmd.exe").is_file():
                self.client.steamcmd_install_root = d
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, d)
                settings.save_client_settings(self.paths, self.client)
                self._save_bootstrap_client_settings()
            elif d:
                messagebox.showwarning("Invalid", "steamcmd.exe was not found in that folder.")
        else:
            d = filedialog.askdirectory(title="Select WindowsServer folder (WindroseServer.exe)")
            if d:
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, d)

    def _on_browse_dest(self) -> None:
        d = filedialog.askdirectory(title="Install destination")
        if d:
            self.ent_install_dest.delete(0, tk.END)
            self.ent_install_dest.insert(0, d)
            if self.client.install_client == "SteamCMD":
                self.client.steamcmd_force_install_dir = d
                settings.save_client_settings(self.paths, self.client)
                self._save_bootstrap_client_settings()

    def _on_install_server(self) -> None:
        if process_ops.get_server_process():
            self._apply_install_update_button_state()
            self.txt_install_log.delete("1.0", tk.END)
            self.txt_install_log.insert(
                tk.END,
                "ERROR: You cannot update the server while it's running. "
                "Be sure to stop the server first.",
            )
            return
        self._run_install_continue()

    def _run_install_continue(self) -> None:
        dst = self.ent_install_dest.get().strip() or str(self.paths.server_dir)
        if self.client.install_client == "SteamCMD":
            cmd_root = self.client.steamcmd_install_root
            if not cmd_root or not (Path(cmd_root) / "steamcmd.exe").is_file():
                self.txt_install_log.delete("1.0", tk.END)
                self.txt_install_log.insert(tk.END, "ERROR: steamcmd.exe not found.")
                return
            self.client.steamcmd_force_install_dir = dst
            settings.save_client_settings(self.paths, self.client)
            self._save_bootstrap_client_settings()
            Path(dst).mkdir(parents=True, exist_ok=True)
            steamcmd_exe = Path(cmd_root) / "steamcmd.exe"
            args = [
                str(steamcmd_exe),
                "+@ShutdownOnFailedCommand",
                "1",
                "+@NoPromptForPassword",
                "1",
                "+force_install_dir",
                dst,
                "+login",
                "anonymous",
                "+app_update",
                constants.WINDROSE_STEAM_APP_ID,
                "validate",
                "+quit",
            ]
            self.txt_install_log.delete("1.0", tk.END)
            self.txt_install_log.insert(
                tk.END,
                f"Starting SteamCMD...\n{steamcmd_exe}\n" + " ".join(args) + "\n\nA separate SteamCMD window will open.",
            )
            creationflags = 0
            for flag_name in ("CREATE_NEW_CONSOLE", "CREATE_NEW_PROCESS_GROUP"):
                creationflags |= getattr(subprocess, flag_name, 0)
            subprocess.Popen(args, cwd=cmd_root, creationflags=creationflags, close_fds=True)
            return

        src = self.ent_steam_src.get().strip()
        if not src or not (Path(src) / "WindroseServer.exe").is_file():
            self.txt_install_log.delete("1.0", tk.END)
            self.txt_install_log.insert(tk.END, f"ERROR: Invalid source:\n{src}")
            return
        Path(dst).mkdir(parents=True, exist_ok=True)
        self.btn_install_server.config(state=tk.DISABLED)
        self.txt_install_log.delete("1.0", tk.END)
        self.txt_install_log.insert(tk.END, f"Installing from:\n{src}\nTo:\n{dst}\n\nPlease wait...")
        log_path = Path(dst) / "install.log"

        def worker():
            try:
                install_ops.robocopy_install(Path(src), Path(dst), log_path)
            finally:
                self.root.after(0, lambda: self._install_finished(dst))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_install_log(log_path)

    def _poll_install_log(self, log_path: Path) -> None:
        if self.btn_install_server.cget("state") == tk.NORMAL:
            return
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
                self.txt_install_log.delete("1.0", tk.END)
                self.txt_install_log.insert(tk.END, "Installing...\n" + "\n".join(lines))
            except OSError:
                pass
        self.root.after(2000, lambda: self._poll_install_log(log_path))

    def _install_finished(self, dst: str) -> None:
        self.btn_install_server.config(state=tk.NORMAL)
        dst_exe = Path(dst) / "WindroseServer.exe"
        if dst_exe.is_file():
            self.paths.set_root(dst)
            self.paths.ensure_backup_dir()
            self.client.server_root = str(self.paths.server_dir)
            settings.save_client_settings(self.paths, self.client)
            self._save_bootstrap_client_settings()
            self.canvas_install.itemconfig(self._install_dot, fill="#00FF00")
            self.lbl_install_status.config(text="Server installed successfully.", fg=self.c["green"])
            self.txt_install_log.insert(tk.END, "\n\nInstall complete!")
            if not self.paths.config_path.is_file():
                config_io.write_minimal_server_config(self.paths)
                self._read_server_config_ui()
            self._update_setup_wizard()
        else:
            self.txt_install_log.insert(tk.END, "\n\nWARNING: WindroseServer.exe not found at destination.")

    def _on_check_reqs(self) -> None:
        if self.client.install_client == "SteamCMD":
            td = self.ent_install_dest.get().strip()
            if td:
                self.client.steamcmd_force_install_dir = td
                settings.save_client_settings(self.paths, self.client)
                self._save_bootstrap_client_settings()
        found = self._find_steam_windrose()
        if found:
            self.paths.set_root(found)
            self.paths.ensure_backup_dir()
            self.client.server_root = str(found)
            settings.save_client_settings(self.paths, self.client)
            self._save_bootstrap_client_settings()
            if self.client.install_client == "SteamCMD":
                cmd = self.client.steamcmd_install_root or str(
                    steam.get_steamcmd_install_root(_app_package_dir().parent) or ""
                )
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, cmd)
            else:
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, str(found))
        elif self.paths.server_exe.is_file():
            if self.client.install_client == "SteamCMD":
                cmd = self.client.steamcmd_install_root or str(
                    steam.get_steamcmd_install_root(_app_package_dir().parent) or ""
                )
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, cmd)
            else:
                self.ent_steam_src.delete(0, tk.END)
                self.ent_steam_src.insert(0, str(self.paths.server_dir))
        self._update_setup_wizard()
