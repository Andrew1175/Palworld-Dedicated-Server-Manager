from __future__ import annotations

import json
import ctypes
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Empty

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import psutil

from . import config_io, config_form, constants, discord_webhook, install_ops, players, process_ops, rest_api, settings, steam, updater
from .backup import backup_saves_now, find_latest_backup
from .paths import ServerPaths
from .settings import ClientInstallSettings, ManagerSettings
from .ui_theme import apply_dark_theme, HoverToolTip, tk_button

_CONFIG_REQUIRED_MSG = (
    "PalWorldSettings.ini is missing or empty. Open the Config tab and click "
    "Save Config once to generate it before starting the server."
)


def _app_package_dir() -> Path:
    return Path(__file__).resolve().parent


def _bootstrap_client_settings_path() -> Path:
    # Keep a launcher-level copy so startup can remember server root
    # even before we know which server directory to bind paths to.
    return _app_package_dir().parent / "palworld_client_settings.json"


def _isolated_dedicated_server_env(working_dir: str) -> dict[str, str]:
    """
    A deliberately minimal environment for the dedicated server child process.

    Inheriting the full parent environment can let `PalServer-Win64-Shipping-Cmd.exe` resolve
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


class PalworldServerManagerApp:
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
        self.max_players = 32
        self.log_position = 0
        self.log_buffer: list[str] = []
        self.log_filter = "All"
        self._console_log_queue: queue.Queue[str] = queue.Queue()
        self.online_players: set[str] = set()
        self.account_to_player: dict[str, str] = {}
        self.last_player_snapshot = ""
        self._player_total_seconds: dict[str, float] = {}
        self._player_session_start_totals: dict[str, datetime] = {}
        self._hourly_online_seconds: list[float] = [0.0] * 24
        self._player_session_start_hourly: dict[str, datetime] = {}
        self._insights_last_updated_ts: str | None = None
        self._insights_after: str | None = None
        self._active_times_chart_points: list[tuple[float, float, int, float]] = []
        self._active_times_tooltip_label: tk.Label | None = None
        self.watchdog_tick = 0
        self.last_schedule_date: date | None = None
        self._schedule_warn_sent: set[int] = set()
        self._schedule_prev_secs: float | None = None
        self._rest_api_poll_count = 0
        self._server_version_label = "--"
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
        self._start_blocked_reason: str | None = None

        apply_dark_theme(root)
        root.title("Palworld Server Manager")
        # Wide enough for two-column Tools (App/Hosting, Backup/Schedule) without clipping.
        root.minsize(760, 660)
        root.geometry("920x820")
        try:
            candidates: list[Path] = []
            # Source run: project root
            candidates.append(_app_package_dir().parent / "palworld_logo.ico")
            # Frozen run: executable directory (onedir)
            if getattr(sys, "frozen", False):
                candidates.append(Path(sys.executable).resolve().parent / "palworld_logo.ico")
            # PyInstaller extraction dir (onefile / fallback)
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / "palworld_logo.ico")
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
            row1, text="Palworld Server", font=(None, 16, "bold"), fg=c["accent"], bg=c["bg_header"]
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
        tk.Label(row2, text="Game Version:", fg=c["text_muted"], bg=c["bg_header"], font=(None, 9)).pack(side=tk.LEFT)
        self.lbl_server_info = tk.Label(
            row2, text="--", fg="#A0C4E0", bg=c["bg_header"], font=(None, 9), cursor="hand2"
        )
        self.lbl_server_info.pack(side=tk.LEFT, padx=(6, 0))

        right = tk.Frame(hdr, bg=c["bg_header"])
        right.grid(row=0, column=1, sticky="e")
        tk_button(right, "Invite", self._on_share, bg=c["blue_btn"], small=True).pack()

        # Notebook
        self.nb = ttk.Notebook(root)
        self.nb.configure(padding=0)
        self.nb.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._build_tab_dashboard()
        self._build_tab_insights()
        self._build_tab_config()
        self._build_tab_log()
        self._build_tab_commands()
        self._build_tab_tools()
        self._build_tab_install()
        self._build_tab_help()

        self.lbl_version_corner = tk.Label(
            root,
            text=f"Server Manager Version: {constants.APP_VERSION}",
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
        self.tip_start = HoverToolTip(self.btn_start, "")
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

    def _select_install_tab(self) -> None:
        for tab_id in self.nb.tabs():
            if self.nb.tab(tab_id, "text") == "Install":
                self.nb.select(tab_id)
                return

    def _select_config_tab(self) -> None:
        for tab_id in self.nb.tabs():
            if self.nb.tab(tab_id, "text") == "Config":
                self.nb.select(tab_id)
                return

    def _apply_start_button_state(self) -> None:
        if "Running" in self.lbl_status.cget("text"):
            return
        if not self.paths.server_installed():
            self.btn_start.config(state=tk.DISABLED)
            self.tip_start.text = ""
            if self._start_blocked_reason == "config":
                self._start_blocked_reason = None
            return
        if not config_io.is_server_config_ready(self.paths):
            self.btn_start.config(state=tk.DISABLED)
            self.tip_start.text = _CONFIG_REQUIRED_MSG
            self.lbl_cfg_status.config(text=_CONFIG_REQUIRED_MSG, fg=self.c["accent"])
            self.log(_CONFIG_REQUIRED_MSG)
            if self._start_blocked_reason != "config":
                self._start_blocked_reason = "config"
                self._select_config_tab()
            return
        self.btn_start.config(state=tk.NORMAL)
        self.tip_start.text = ""
        if self._start_blocked_reason == "config":
            self._start_blocked_reason = None
        if self.lbl_cfg_status.cget("text") == _CONFIG_REQUIRED_MSG:
            self.lbl_cfg_status.config(text="", fg=self.c["green"])
        if self.lbl_footer_log.cget("text") == _CONFIG_REQUIRED_MSG:
            self.log("Ready.")

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
        self.lbl_config_lock.pack_forget()

        self.config_form = config_form.ConfigForm(self)
        self.config_form.set_player_max_callback(self._on_max_players_slide_value)
        self.config_form.build(pad)
        self._hdr_server_settings = self.config_form.first_section_header

        self.ent_srv_name = self.config_form.get_entry("ServerName")
        self.ent_password = self.config_form.get_entry("ServerPassword")
        self.ent_admin_password = self.config_form.get_entry("AdminPassword")
        self.ent_direct_port = self.config_form.get_entry("PublicPort")
        self.ent_rest_api_port = self.config_form.get_entry("RESTAPIPort")
        self.scale_max = self.config_form.get_scale("ServerPlayerMaxNum")
        self.ent_launch_args = self.config_form.launch_args_entry
        self.var_rest_api_en = self.config_form.bindings["RESTAPIEnabled"].var
        self.chk_rest_api_en = self.config_form.bindings["RESTAPIEnabled"].widget
        self.lbl_max_val = self.config_form.bindings["ServerPlayerMaxNum"].value_label
        self.btn_reveal_password = self.config_form.bindings["ServerPassword"].reveal_btn
        self.btn_reveal_admin_password = self.config_form.bindings["AdminPassword"].reveal_btn

        bf = tk.Frame(pad, bg=self.c["bg"])
        bf.pack(fill=tk.X, pady=12)
        self.btn_cfg_save = tk_button(bf, "Save Config", self._on_save_config, bg=self.c["green_btn"])
        self.btn_cfg_save.pack(side=tk.LEFT, padx=2)
        self.btn_cfg_reload = tk_button(bf, "Reload Saved Config", self._on_reload_config, bg=self.c["gray_btn"])
        self.btn_cfg_reload.pack(side=tk.LEFT, padx=2)
        self.btn_cfg_open_server = tk_button(bf, "Open Server Config", self._on_open_server_config, bg=self.c["blue_btn"])
        self.btn_cfg_open_server.pack(side=tk.LEFT, padx=2)
        self.lbl_cfg_status = tk.Label(pad, text="", fg=self.c["green"], bg=self.c["bg"], font=(None, 10))
        self.lbl_cfg_status.pack(anchor="w", pady=4)

    def _build_tab_log(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self._log_tab = tab
        self._log_tab_tip: tk.Toplevel | None = None
        # Console capture into this tab is WIP; use the visible PalServer console for now.
        self.nb.add(tab, text="Log", state="disabled")
        self.nb.bind("<Motion>", self._on_notebook_motion_log_tip, add="+")
        self.nb.bind("<Leave>", self._on_notebook_leave_log_tip, add="+")
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

    def _log_tab_under_pointer(self, event: tk.Event) -> bool:
        try:
            idx = self.nb.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return False
        tabs = self.nb.tabs()
        if idx < 0 or idx >= len(tabs):
            return False
        return tabs[idx] == str(self._log_tab)

    def _on_notebook_motion_log_tip(self, event: tk.Event) -> None:
        if not self._log_tab_under_pointer(event):
            self._hide_log_tab_tip()
            return
        if self._log_tab_tip is not None:
            return
        tip = tk.Toplevel(self.nb)
        tip.wm_overrideredirect(True)
        tip.geometry(f"+{event.x_root + 12}+{event.y_root + 16}")
        tk.Label(
            tip,
            text="WIP",
            bg="#1A2A3A",
            fg="#C0CDD8",
            bd=1,
            relief=tk.SOLID,
            padx=8,
            pady=5,
            font=(None, 10),
        ).pack()
        self._log_tab_tip = tip

    def _on_notebook_leave_log_tip(self, _event: tk.Event | None = None) -> None:
        self._hide_log_tab_tip()

    def _hide_log_tab_tip(self) -> None:
        if self._log_tab_tip is not None:
            self._log_tab_tip.destroy()
            self._log_tab_tip = None

    def _build_tab_commands(self) -> None:
        tab = tk.Frame(self.nb, bg=self.c["bg"])
        self.nb.add(tab, text="Commands")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        self._cmd_defs: list[dict] = [
            {
                "key": "info",
                "label": "Get Server Info",
                "fields": (),
                "docs": "https://docs.palworldgame.com/api/rest-api/info",
            },
            {
                "key": "players",
                "label": "Get Player List",
                "fields": (),
                "docs": "https://docs.palworldgame.com/api/rest-api/players",
            },
            {
                "key": "settings",
                "label": "Get Server Settings",
                "fields": (),
                "docs": "https://docs.palworldgame.com/api/rest-api/settings",
            },
            {
                "key": "metrics",
                "label": "Get Server Metrics",
                "fields": (),
                "docs": "https://docs.palworldgame.com/api/rest-api/metrics",
            },
            {
                "key": "announce",
                "label": "Announce Message",
                "fields": ("message",),
                "docs": "https://docs.palworldgame.com/api/rest-api/announce",
            },
            {
                "key": "kick",
                "label": "Kick Player",
                "fields": ("userid", "message_optional"),
                "docs": "https://docs.palworldgame.com/api/rest-api/kick",
            },
            {
                "key": "ban",
                "label": "Ban Player",
                "fields": ("userid", "message_optional"),
                "docs": "https://docs.palworldgame.com/api/rest-api/ban",
            },
            {
                "key": "unban",
                "label": "Unban Player",
                "fields": ("userid",),
                "docs": "https://docs.palworldgame.com/api/rest-api/unban",
            },
            {
                "key": "save",
                "label": "Save World",
                "fields": (),
                "docs": "https://docs.palworldgame.com/api/rest-api/save",
            },
        ]
        self._cmd_by_label = {d["label"]: d for d in self._cmd_defs}
        self._cmd_userid_choices: dict[str, str] = {}

        top = tk.Frame(tab, bg=self.c["bg"])
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Command").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.var_cmd = tk.StringVar(value=self._cmd_defs[0]["label"])
        self.cmb_cmd = ttk.Combobox(
            top,
            textvariable=self.var_cmd,
            values=[d["label"] for d in self._cmd_defs],
            state="readonly",
            width=28,
        )
        self.cmb_cmd.grid(row=0, column=1, sticky="w")
        self.cmb_cmd.bind("<<ComboboxSelected>>", lambda _e: self._on_command_selected())
        self.btn_cmd_send = tk_button(top, "Send", self._on_command_send, bg=self.c["green_btn"])
        self.btn_cmd_send.grid(row=0, column=2, sticky="e", padx=(12, 0))

        self.frm_cmd_fields = tk.Frame(tab, bg=self.c["bg"])
        self.frm_cmd_fields.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        self.frm_cmd_fields.columnconfigure(1, weight=1)

        self.lbl_cmd_message = ttk.Label(self.frm_cmd_fields, text="Message")
        self.ent_cmd_message = ttk.Entry(self.frm_cmd_fields)
        self.lbl_cmd_userid = ttk.Label(self.frm_cmd_fields, text="User ID")
        self.cmb_cmd_userid = ttk.Combobox(self.frm_cmd_fields)
        self.lbl_cmd_hint = tk.Label(
            self.frm_cmd_fields,
            text="",
            fg=self.c["text_muted"],
            bg=self.c["bg"],
            font=(None, 9),
            wraplength=720,
            justify=tk.LEFT,
            anchor="w",
        )

        result_wrap = tk.Frame(tab, bg=self.c["bg"])
        result_wrap.grid(row=2, column=0, sticky="nsew", padx=12, pady=(6, 12))
        result_wrap.columnconfigure(0, weight=1)
        result_wrap.rowconfigure(1, weight=1)
        ttk.Label(result_wrap, text="Result", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.txt_cmd_result = tk.Text(
            result_wrap,
            bg="#111E2A",
            fg="#C0CDD8",
            font=("Consolas", 10),
            wrap=tk.WORD,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.c["border"],
        )
        self.txt_cmd_result.grid(row=1, column=0, sticky="nsew")
        ys = ttk.Scrollbar(result_wrap, command=self.txt_cmd_result.yview)
        ys.grid(row=1, column=1, sticky="ns")
        self.txt_cmd_result.config(yscrollcommand=ys.set)
        self.txt_cmd_result.insert("1.0", "Select a command and click Send.\n")
        self.txt_cmd_result.config(state=tk.DISABLED)

        self._on_command_selected()

    def _on_command_selected(self) -> None:
        cmd = self._cmd_by_label.get(self.var_cmd.get())
        fields = cmd["fields"] if cmd else ()
        for w in (
            self.lbl_cmd_message,
            self.ent_cmd_message,
            self.lbl_cmd_userid,
            self.cmb_cmd_userid,
            self.lbl_cmd_hint,
        ):
            w.grid_forget()
        row = 0
        if "userid" in fields:
            self.lbl_cmd_userid.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            self.cmb_cmd_userid.grid(row=row, column=1, sticky="ew", pady=3)
            row += 1
            self._refresh_command_userid_choices()
            self.lbl_cmd_hint.config(
                text="Pick an online player or type a userId (e.g. steam_...). Kick/Ban/Unban use userid."
            )
            self.lbl_cmd_hint.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 4))
            row += 1
        if "message" in fields or "message_optional" in fields:
            label = "Message" if "message" in fields else "Message (optional)"
            self.lbl_cmd_message.config(text=label)
            self.lbl_cmd_message.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            self.ent_cmd_message.grid(row=row, column=1, sticky="ew", pady=3)

    def _refresh_command_userid_choices(self) -> None:
        choices: list[str] = []
        mapping: dict[str, str] = {}
        for name in sorted(self.online_players):
            # Prefer a matching userId if we recently fetched player rows.
            uid = self._cmd_userid_choices.get(name, "")
            label = f"{name} ({uid})" if uid else name
            choices.append(label)
            mapping[label] = uid or name
        self.cmb_cmd_userid["values"] = choices
        # Keep free-text entry allowed.
        self.cmb_cmd_userid.configure(state="normal")

    def _resolve_command_userid(self) -> str:
        raw = self.cmb_cmd_userid.get().strip()
        if not raw:
            return ""
        if raw in self._cmd_userid_choices.values():
            return raw
        # Label form: "Name (userid)" or bare userid / name
        if raw.endswith(")") and "(" in raw:
            inner = raw[raw.rfind("(") + 1 : -1].strip()
            if inner:
                return inner
        # Map display name -> userid when known
        for name, uid in self._cmd_userid_choices.items():
            if raw == name and uid:
                return uid
        return raw

    def _set_command_result(self, text: str) -> None:
        self.txt_cmd_result.config(state=tk.NORMAL)
        self.txt_cmd_result.delete("1.0", tk.END)
        self.txt_cmd_result.insert("1.0", text)
        self.txt_cmd_result.config(state=tk.DISABLED)

    def _format_command_result(self, data: object, err: str | None) -> str:
        if err:
            return f"Error:\n{err}"
        try:
            return json.dumps(data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)

    def _on_command_send(self) -> None:
        cmd = self._cmd_by_label.get(self.var_cmd.get())
        if not cmd:
            return
        enabled, api_port, admin_pw = config_io.read_rest_api_config(self.paths)
        if not enabled:
            self._set_command_result("REST API is disabled. Enable RESTAPIEnabled in Config and restart the server.")
            self.log("REST API is disabled.")
            return
        if not admin_pw:
            self._set_command_result("Admin password is not set. Set AdminPassword in Config.")
            self.log("Admin password is not set.")
            return
        if not process_ops.get_server_process():
            self._set_command_result("Server is not running.")
            self.log("Server is not running.")
            return

        fields = cmd["fields"]
        message = self.ent_cmd_message.get().strip()
        userid = self._resolve_command_userid()
        if "message" in fields and not message:
            self._set_command_result("Message is required for Announce.")
            return
        if "userid" in fields and not userid:
            self._set_command_result("User ID is required.")
            return

        key = cmd["key"]
        self.btn_cmd_send.config(state=tk.DISABLED)
        self._set_command_result(f"Sending {cmd['label']}...")
        self.log(f"Commands: sending {cmd['label']}...")

        def worker() -> None:
            data: object = None
            err: str | None = None
            try:
                if key == "info":
                    data, err = rest_api.get_server_info("127.0.0.1", api_port, admin_pw)
                elif key == "players":
                    rows, err = rest_api.get_players("127.0.0.1", api_port, admin_pw)
                    data = {"players": rows}
                    if not err:
                        mapping = {
                            rest_api.player_display_name(p): rest_api.player_user_id(p)
                            for p in rows
                            if rest_api.player_user_id(p)
                        }
                        self.root.after(0, lambda m=mapping: self._store_cmd_userid_map(m))
                elif key == "settings":
                    data, err = rest_api.get_server_settings("127.0.0.1", api_port, admin_pw)
                elif key == "metrics":
                    data, err = rest_api.get_server_metrics("127.0.0.1", api_port, admin_pw)
                elif key == "announce":
                    data, err = rest_api.announce_message("127.0.0.1", api_port, admin_pw, message)
                elif key == "kick":
                    data, err = rest_api.kick_player(
                        "127.0.0.1", api_port, admin_pw, userid, message
                    )
                elif key == "ban":
                    data, err = rest_api.ban_player(
                        "127.0.0.1", api_port, admin_pw, userid, message
                    )
                elif key == "unban":
                    data, err = rest_api.unban_player("127.0.0.1", api_port, admin_pw, userid)
                elif key == "save":
                    data, err = rest_api.save_world("127.0.0.1", api_port, admin_pw)
                else:
                    err = f"Unknown command: {key}"
            except Exception as e:
                err = str(e)
            text = self._format_command_result(data, err)
            self.root.after(0, lambda t=text, e=err, label=cmd["label"]: self._finish_command_send(t, e, label))

        threading.Thread(target=worker, daemon=True).start()

    def _store_cmd_userid_map(self, mapping: dict[str, str]) -> None:
        self._cmd_userid_choices.update(mapping)
        self._refresh_command_userid_choices()

    def _finish_command_send(self, text: str, err: str | None, label: str) -> None:
        self.btn_cmd_send.config(state=tk.NORMAL)
        self._set_command_result(text)
        if err:
            self.log(f"Commands: {label} failed.")
        else:
            self.log(f"Commands: {label} OK.")
            if label in ("Kick Player", "Ban Player", "Announce Message", "Save World"):
                self._refresh_player_list()

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
        self.cmb_schedule_hour = ttk.Combobox(
            sf, values=[str(h) for h in range(1, 13)], width=3, state="readonly"
        )
        self.cmb_schedule_hour.set("4")
        self.cmb_schedule_hour.pack(side=tk.LEFT, padx=(6, 2))
        tk.Label(sf, text=":", fg=self.c["text"], bg=self.c["bg_panel"], font=(None, 11)).pack(side=tk.LEFT)
        self.cmb_schedule_minute = ttk.Combobox(
            sf,
            values=[f"{m:02d}" for m in range(60)],
            width=3,
            state="readonly",
        )
        self.cmb_schedule_minute.set("00")
        self.cmb_schedule_minute.pack(side=tk.LEFT, padx=2)
        self.cmb_schedule_ampm = ttk.Combobox(sf, values=["AM", "PM"], width=4, state="readonly")
        self.cmb_schedule_ampm.set("AM")
        self.cmb_schedule_ampm.pack(side=tk.LEFT, padx=(4, 0))
        for w in (self.cmb_schedule_hour, self.cmb_schedule_minute, self.cmb_schedule_ampm):
            w.bind("<<ComboboxSelected>>", lambda _e: self._save_settings())
        tk.Label(
            s,
            text=(
                "Uses graceful Shutdown API. With the server running, announce warnings "
                "are sent at 30, 20, 15, 10, 5, and 1 minute(s) before restart."
            ),
            fg=self.c["text_muted"],
            bg=self.c["bg_panel"],
            font=(None, 9),
            wraplength=340,
            justify=tk.LEFT,
            anchor="w",
        ).pack(anchor="w", pady=(6, 0))
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
        tk.Label(bi, text="Follow these steps to get your Palworld dedicated server running.", fg=self.c["text_dim"], bg=self.c["bg_panel"], font=(None, 10), wraplength=520, justify=tk.LEFT).pack(anchor="w", pady=4)
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
            text="Palworld must be installed via Steam (App ID 2394010). Install the Palworld Dedicated Server app from Steam.",
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
            text="Project links for documentation and support.",
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
        self.lbl_server_info.bind("<Button-1>", lambda e: self._copy_server_info())

    def _on_max_players_slide_value(self, n: int) -> None:
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
            # Graceful/manual stop finished; do not treat as a crash.
            self._stop_pending = False
            self._stop_pending_logged = False
            self.server_popen = None
            self.start_time = None
            if ui_running:
                self._set_ui_stopped()
                self.log("Server stopped.")
        elif proc and not ui_running:
            if self._stop_pending:
                # Stop was requested; ignore this short shutdown window.
                if not self._stop_pending_logged:
                    self.log("Shutdown in progress...")
                    self._stop_pending_logged = True
            else:
                self._set_ui_running()
                if self.start_time is None:
                    self.start_time = datetime.now()
        elif proc and ui_running:
            self._update_stats(proc)
            # Poll often enough that join/leave history stays reasonably timely.
            if self.watchdog_tick % 2 == 0:
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
            self._tick_scheduled_restart_warnings(proc)
            now_hm = datetime.now().strftime("%H:%M")
            target = self._get_schedule_time_24h()
            today = date.today()
            if target and now_hm == target and self.last_schedule_date != today:
                self.last_schedule_date = today
                self.log("Scheduled daily restart.")
                self._do_restart("schedule")
        else:
            self._schedule_prev_secs = None
            self._schedule_warn_sent.clear()
        if self.paths.server_installed():
            self.canvas_install.itemconfig(self._install_dot, fill="#00FF00")
            self.lbl_install_status.config(text="Server installed.", fg=self.c["green"])
        # Keep Config tab lock state in sync with real process state, including
        # the short shutdown window right after pressing Stop.
        self._apply_config_tab_state()
        self._apply_install_update_button_state()

    def _schedule_log_tail(self) -> None:
        self._drain_console_log_queue()
        self.root.after(200, self._schedule_log_tail)

    def _drain_console_log_queue(self) -> None:
        while True:
            try:
                line = self._console_log_queue.get_nowait()
            except Empty:
                break
            self._append_console_log_line(line)

    def _append_console_log_line(self, line: str, *, persist: bool = True) -> None:
        """Append one console line to buffer and Log tab UI."""
        text = line.rstrip("\r\n")
        if not text:
            return
        self.log_buffer.append(text)
        while len(self.log_buffer) > 5000:
            self.log_buffer.pop(0)
        if persist:
            try:
                with open(self.paths.log_path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(text + "\n")
            except OSError:
                pass

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
            text,
            online=self.online_players,
            account_to_player=self.account_to_player,
            on_join_history=hist_join,
            on_leave_history=hist_leave,
        )
        if self._test_log_filter(text, self.log_filter):
            self.txt_log.insert(tk.END, text + "\n", self._log_line_tag(text))
            while int(self.txt_log.index("end-1c").split(".")[0]) > 1000:
                self.txt_log.delete("1.0", "2.0")
            if self.var_autoscroll_log.get():
                self.txt_log.see(tk.END)

    def _set_ui_running(self) -> None:
        self.canvas_status.itemconfig(self._status_dot, fill="#00FF00")
        self.lbl_status.config(text="  Running", fg=self.c["green"])
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_restart.config(state=tk.NORMAL)
        self._rest_api_poll_count = 0
        self._poll_rest_api()
        self._apply_config_tab_state()
        self._start_insights_timer()

    def _set_ui_stopped(self) -> None:
        # Anyone still listed as online left when the server stopped.
        self._stop_insights_timer()
        self._apply_online_player_names(set(), record_history=True)
        self._close_open_insight_sessions()
        self.canvas_status.itemconfig(self._status_dot, fill=self.c["status_stopped"])
        self.lbl_status.config(text="  Stopped", fg=self.c["text_dim"])
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_restart.config(state=tk.DISABLED)
        self.lbl_server_info.config(text="--")
        self._reset_stats()
        self._apply_config_tab_state()
        self._apply_start_button_state()

    def _apply_config_tab_state(self) -> None:
        """While the dedicated server is running, block all Config tab changes."""
        if process_ops.get_server_process():
            if self.config_form.first_section_header is not None:
                self.lbl_config_lock.pack(
                    anchor="w", pady=(0, 8), before=self.config_form.first_section_header
                )
            self.config_form.set_enabled(False)
            for b in (
                self.btn_cfg_save,
                self.btn_cfg_reload,
                self.btn_cfg_open_server,
            ):
                b.config(state=tk.DISABLED)
            return
        self.lbl_config_lock.pack_forget()
        self.config_form.set_enabled(True)
        for b in (
            self.btn_cfg_save,
            self.btn_cfg_reload,
            self.btn_cfg_open_server,
        ):
            b.config(state=tk.NORMAL)

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
        # Used when attaching to a session that already wrote manager_console.log.
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
            for line in text.splitlines():
                if line:
                    self._append_console_log_line(line, persist=False)
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
        enabled, api_port, admin_pw = config_io.read_rest_api_config(self.paths)
        if enabled and admin_pw:
            player_rows, err = rest_api.get_players("127.0.0.1", api_port, admin_pw)
            if not err:
                names = {rest_api.player_display_name(p) for p in player_rows}
                if hasattr(self, "_cmd_userid_choices"):
                    self._cmd_userid_choices = {
                        rest_api.player_display_name(p): rest_api.player_user_id(p)
                        for p in player_rows
                        if rest_api.player_user_id(p)
                    }
                self._apply_online_player_names(names, record_history=True)
                return
        if self.paths.log_path.is_file():
            online, acct = players.replay_full_log(self.paths.log_path)
            self.account_to_player = acct
            # Log replay is a snapshot only (no join/leave events).
            self._apply_online_player_names(online, record_history=False)

    def _apply_online_player_names(self, names: set[str], *, record_history: bool) -> None:
        """Update Connected Players and optionally emit Connection History from set diffs."""
        previous = set(self.online_players)
        incoming = set(names)
        joined = incoming - previous
        left = previous - incoming
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        for name in sorted(joined):
            self._on_player_join(name, now)
            if record_history:
                self._add_history(f"[{stamp}] JOINED: {name}")
        for name in sorted(left):
            self._on_player_leave(name, now)
            if record_history:
                self._add_history(f"[{stamp}] LEFT: {name}")
        self.online_players = incoming
        self.list_players.delete(0, tk.END)
        for p in sorted(self.online_players):
            self.list_players.insert(tk.END, p)
        self.lbl_players_big.config(text=f"{len(self.online_players)} / {self.max_players}")
        if hasattr(self, "cmb_cmd_userid"):
            self._refresh_command_userid_choices()

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

    def _checkpoint_open_insight_sessions(self, when: datetime | None = None) -> bool:
        """Commit in-progress session time into Insights without ending the sessions."""
        ts = when or datetime.now()
        changed = False

        for name, started in list(self._player_session_start_totals.items()):
            if ts > started:
                self._player_total_seconds[name] = self._player_total_seconds.get(name, 0.0) + (
                    ts - started
                ).total_seconds()
                self._player_session_start_totals[name] = ts
                changed = True

        for name, started in list(self._player_session_start_hourly.items()):
            if ts > started:
                self._accumulate_hourly_seconds(started, ts)
                self._player_session_start_hourly[name] = ts
                changed = True

        if changed:
            self._save_insights_data()
            self._refresh_insights_ui()
        return changed

    def _start_insights_timer(self) -> None:
        if self._insights_after is not None:
            return
        self._insights_after = self.root.after(60_000, self._insights_tick)

    def _stop_insights_timer(self) -> None:
        if self._insights_after is not None:
            try:
                self.root.after_cancel(self._insights_after)
            except tk.TclError:
                pass
            self._insights_after = None

    def _insights_tick(self) -> None:
        self._insights_after = None
        if self._player_session_start_totals or self._player_session_start_hourly:
            self._checkpoint_open_insight_sessions()
        # Keep ticking while the server UI is marked running.
        if "Running" in self.lbl_status.cget("text"):
            self._start_insights_timer()

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
        self.mgr.schedule_time = self._get_schedule_time_24h() or "04:00"
        if hasattr(self, "ent_launch_args"):
            self.mgr.launch_arguments = self.ent_launch_args.get().strip() or constants.DEFAULT_LAUNCH_ARGS
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
                url, "Palworld Server Manager: **test** notification (webhook OK)."
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
        self._set_schedule_widgets_from_24h(self.mgr.schedule_time)
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
        if hasattr(self, "ent_launch_args"):
            self.ent_launch_args.delete(0, tk.END)
            self.ent_launch_args.insert(0, self.mgr.launch_arguments or constants.DEFAULT_LAUNCH_ARGS)
        if self.mgr.steamcmd_force_install_dir:
            self.client.steamcmd_force_install_dir = self.mgr.steamcmd_force_install_dir
            settings.sync_steamcmd_sidecar(
                self.client.install_client,
                self.client.steamcmd_force_install_dir,
                self.paths.steamcmd_sidecar,
            )

    def _post_load_init(self) -> None:
        self.lbl_cur_ver.config(text=f"Current version: {constants.APP_VERSION}")
        self.lbl_version_corner.config(text=f"Server Manager Version: {constants.APP_VERSION}")
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
        self._load_history()
        self._load_insights_data()
        self._update_setup_wizard()
        if self.client.install_client != "SteamCMD":
            det = self._find_steam_palworld()
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
        self._stop_insights_timer()
        self._close_open_insight_sessions()
        self._save_settings()
        self.root.destroy()

    def _steamcmd_force_path(self) -> Path | None:
        raw = self.ent_install_dest.get().strip()
        return Path(raw) if raw else None

    def _find_steam_palworld(self):
        fd = self.client.steamcmd_force_install_dir
        force = Path(fd) if fd else None
        return steam.find_steam_palworld(
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
                if (sr / "PalServer.exe").is_file() or (
                    sr / "Pal" / "Binaries" / "Win64" / "PalServer-Win64-Shipping-Cmd.exe"
                ).is_file():
                    self.paths.set_root(sr)
                    self.paths.ensure_backup_dir()
                    return sr
        else:
            r = messagebox.askyesnocancel(
                "Choose Client Type",
                "Which client are you using to host your Palworld server?\n\n"
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

        found = self._find_any_palworld()
        if not found and self.paths.server_installed():
            found = self.paths.server_dir
        if not found:
            if self.client.install_client == "Steam":
                if messagebox.askyesno(
                    "Server Files Not Found",
                    "Could not auto-detect Palworld server files. Do you already have server files installed?",
                ):
                    d = filedialog.askdirectory(title="Select Palworld server folder (contains PalServer.exe)")
                    if d and ((Path(d) / "PalServer.exe").is_file() or (
                        Path(d) / "Pal" / "Binaries" / "Win64" / "PalServer-Win64-Shipping-Cmd.exe"
                    ).is_file()):
                        found = Path(d)
            else:
                # SteamCMD-specific first-run guidance.
                has_existing = messagebox.askyesno(
                    "SteamCMD Server Files",
                    "Do you already have the Palworld server files installed?\n\n"
                    "Yes - Select your existing Palworld Server folder (contains PalServer.exe).\n"
                    "No - Select the folder where you want Palworld Server files installed via SteamCMD.",
                )
                if has_existing:
                    d = filedialog.askdirectory(
                        title="Select Palworld Server folder (contains PalServer.exe)"
                    )
                    if d and ((Path(d) / "PalServer.exe").is_file() or (
                        Path(d) / "Pal" / "Binaries" / "Win64" / "PalServer-Win64-Shipping-Cmd.exe"
                    ).is_file()):
                        found = Path(d)
                else:
                    d = filedialog.askdirectory(
                        title="Select destination for Palworld server files (SteamCMD)"
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

    def _find_any_palworld(self) -> Path | None:
        if self.paths.server_installed():
            return self.paths.server_dir
        w = self._find_steam_palworld()
        return w

    def _update_setup_wizard(self) -> None:
        if self.client.install_client == "SteamCMD":
            self.lbl_req_help.config(
                text=(
                    "Palworld must be installed via SteamCMD (App ID 2394010). "
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
                    "Palworld must be installed via Steam (App ID 2394010). "
                    "Install the Palworld Dedicated Server app from Steam."
                )
            )
            self.lbl_steam_source.config(text="Steam Source")
        self.lbl_client_mode.config(text=f"Current mode: {self.client.install_client}")

        steam_found = self._find_steam_palworld() is not None or self.paths.server_installed()
        server_ready = self.paths.server_installed()
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
                text=f"\u2713 Palworld found ({self.client.install_client})", fg=fg_ok
            )
        else:
            set_step(0, b, "1", "Action needed", r_)
            msg = (
                "\u2717 Palworld not found - install/update with SteamCMD app_update 2394010"
                if self.client.install_client == "SteamCMD"
                else "\u2717 Palworld not found - install it via Steam first (App ID 2394010)"
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

        if not self.paths.server_installed():
            self.canvas_install.itemconfig(self._install_dot, fill=self.c["red"])
            self.lbl_install_status.config(text="Not installed - see Install tab", fg=self.c["red"])
            self._select_install_tab()
        self._apply_install_update_button_state()
        self._apply_start_button_state()

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
        opts = config_io.read_effective_option_settings(self.paths)
        if not opts:
            return
        self.config_form.populate(opts, self.mgr.launch_arguments or constants.DEFAULT_LAUNCH_ARGS)
        server_name = opts.get("ServerName")
        if server_name:
            self.lbl_server_title.config(text=str(server_name))

    def _start_server_process(self) -> None:
        # Match the manual .bat: start PalServer.exe <args> from the server folder.
        # Visible console only (Log-tab capture is WIP / disabled).
        if self.paths.server_exe.is_file():
            exe = self.paths.server_exe
            cwd = str(self.paths.server_dir)
        else:
            exe = self.paths.server_exe_direct
            cwd = str(exe.parent if exe.is_file() else self.paths.server_dir)
        launch_args = shlex.split(self.ent_launch_args.get().strip() or constants.DEFAULT_LAUNCH_ARGS)
        self.mgr.launch_arguments = self.ent_launch_args.get().strip() or constants.DEFAULT_LAUNCH_ARGS
        settings.save_manager_settings(self.paths, self.mgr, self.client)
        child_env = _isolated_dedicated_server_env(cwd)
        switched, meipass = _reset_windows_dll_directory_for_child_launch()
        try:
            creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
            self.server_popen = subprocess.Popen(
                [str(exe), *launch_args],
                cwd=cwd,
                env=child_env,
                creationflags=creationflags,
            )
        finally:
            if switched:
                _restore_windows_dll_directory_after_child_launch(meipass)

    def _poll_rest_api(self) -> None:
        self._rest_api_poll_count += 1
        enabled, api_port, admin_pw = config_io.read_rest_api_config(self.paths)
        if enabled and admin_pw:
            info, _err = rest_api.get_server_info("127.0.0.1", api_port, admin_pw)
            if info:
                version = info.get("version") or info.get("serverversion") or info.get("ServerVersion")
                if version:
                    self.lbl_server_info.config(text=str(version))
                    self._server_version_label = str(version)
                self._refresh_player_list()
                return
        if self._rest_api_poll_count < 24:
            self.root.after(5000, self._poll_rest_api)

    def _on_start(self) -> None:
        self._stop_pending = False
        self._stop_pending_logged = False
        if not self.paths.server_installed():
            self.log("Server not installed.")
            return
        if not config_io.is_server_config_ready(self.paths):
            self.log(_CONFIG_REQUIRED_MSG)
            messagebox.showwarning("Server Config Required", _CONFIG_REQUIRED_MSG)
            self._select_config_tab()
            return
        try:
            self.log_position = 0
            self.log_buffer.clear()
            self.txt_log.delete("1.0", tk.END)
            while True:
                try:
                    self._console_log_queue.get_nowait()
                except Empty:
                    break
            try:
                self.paths.log_path.write_text("", encoding="utf-8")
            except OSError:
                pass
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
            self.log("Server started. Console output appears in the PalServer window.")
        except OSError as e:
            self.log(f"Failed to start: {e}")

    def _on_stop(self) -> None:
        if self._stop_pending:
            return
        self._stop_pending = True
        self._stop_pending_logged = False
        self._restart_pending = False
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_restart.config(state=tk.DISABLED)
        self._discord_maybe_send(
            self.ent_discord_msg_stop.get().strip() or settings.DEFAULT_DISCORD_MSG_STOP
        )
        enabled, api_port, admin_pw = config_io.read_rest_api_config(self.paths)
        if not enabled or not admin_pw:
            self.log("REST API not available; force-stopping the server.")
            self._force_stop_processes()
            return
        waittime = 1
        message = "Server shutting down."
        self.log(f"Requesting graceful shutdown via REST API ({waittime}s)...")
        threading.Thread(
            target=self._shutdown_via_api_worker,
            args=(api_port, admin_pw, waittime, message),
            daemon=True,
        ).start()

    def _force_stop_processes(self) -> None:
        process_ops.stop_all_server_processes()
        self.server_popen = None
        self.start_time = None
        self._stop_pending = False
        self._stop_pending_logged = False
        self._set_ui_stopped()
        self.log("Server stopped.")

    def _shutdown_via_api_worker(
        self,
        api_port: int,
        admin_pw: str,
        waittime: int,
        message: str,
    ) -> None:
        _, err = rest_api.shutdown_server(
            "127.0.0.1",
            api_port,
            admin_pw,
            waittime=waittime,
            message=message,
        )
        if err:
            self.root.after(0, lambda e=err: self._on_shutdown_api_failed(e))
            return
        # Wait for the server process to exit after the countdown.
        deadline = time.monotonic() + max(waittime, 0) + 30
        while time.monotonic() < deadline:
            if not process_ops.get_server_process():
                # Watchdog marks the UI stopped when _stop_pending clears the process.
                return
            time.sleep(0.5)
        self.root.after(0, self._on_shutdown_timeout)

    def _on_shutdown_api_failed(self, err: str) -> None:
        self.log(f"Shutdown API failed ({err}); force-stopping.")
        self._force_stop_processes()

    def _on_shutdown_timeout(self) -> None:
        if not self._stop_pending or not process_ops.get_server_process():
            return
        self.log("Graceful shutdown timed out; force-stopping.")
        self._force_stop_processes()

    def _do_restart(self, reason: str = "manual") -> None:
        if self._restart_pending:
            return
        self._restart_pending = True
        self._stop_pending = False
        self._stop_pending_logged = False
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_restart.config(state=tk.DISABLED)
        if reason == "manual":
            self._discord_maybe_send(
                self.ent_discord_msg_restart.get().strip() or settings.DEFAULT_DISCORD_MSG_RESTART
            )
        elif reason == "schedule":
            self._discord_maybe_send(
                self.ent_discord_msg_schedule.get().strip() or settings.DEFAULT_DISCORD_MSG_SCHEDULE
            )

        # Crash recovery: process may already be dead; force-kill leftovers and restart.
        if reason == "crash":
            process_ops.stop_all_server_processes()
            self.root.after(1500, self._restart_after_kill)
            return

        enabled, api_port, admin_pw = config_io.read_rest_api_config(self.paths)
        if not enabled or not admin_pw or not process_ops.get_server_process():
            self.log("REST API unavailable; force-restarting.")
            process_ops.stop_all_server_processes()
            self.root.after(1500, self._restart_after_kill)
            return

        waittime = 1
        message = (
            "Scheduled server restart beginning."
            if reason == "schedule"
            else "Server restarting."
        )
        self.log(f"Restart: requesting graceful shutdown via REST API ({waittime}s)...")
        threading.Thread(
            target=self._restart_via_api_worker,
            args=(api_port, admin_pw, waittime, message),
            daemon=True,
        ).start()

    def _restart_via_api_worker(
        self,
        api_port: int,
        admin_pw: str,
        waittime: int,
        message: str,
    ) -> None:
        _, err = rest_api.shutdown_server(
            "127.0.0.1",
            api_port,
            admin_pw,
            waittime=waittime,
            message=message,
        )
        if err:
            self.root.after(0, lambda e=err: self._on_restart_shutdown_failed(e))
            return
        deadline = time.monotonic() + max(waittime, 0) + 30
        while time.monotonic() < deadline:
            if not process_ops.get_server_process():
                self.root.after(1500, self._restart_after_kill)
                return
            time.sleep(0.5)
        self.root.after(0, self._on_restart_shutdown_timeout)

    def _on_restart_shutdown_failed(self, err: str) -> None:
        self.log(f"Restart shutdown API failed ({err}); force-restarting.")
        process_ops.stop_all_server_processes()
        self.root.after(1500, self._restart_after_kill)

    def _on_restart_shutdown_timeout(self) -> None:
        if not self._restart_pending:
            return
        if not process_ops.get_server_process():
            self.root.after(1500, self._restart_after_kill)
            return
        self.log("Graceful restart shutdown timed out; force-restarting.")
        process_ops.stop_all_server_processes()
        self.root.after(1500, self._restart_after_kill)

    def _parse_schedule_hhmm(self, value: str) -> tuple[int, int] | None:
        """Parse HH:MM (24h) or h:mm AM/PM into (hour24, minute)."""
        text = (value or "").strip()
        if not text:
            return None
        m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", text, re.I)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2))
        if minute < 0 or minute > 59:
            return None
        ampm = (m.group(3) or "").upper()
        if ampm:
            if hour < 1 or hour > 12:
                return None
            if ampm == "AM":
                hour = 0 if hour == 12 else hour
            else:
                hour = 12 if hour == 12 else hour + 12
        elif hour > 23:
            return None
        return hour, minute

    def _get_schedule_time_24h(self) -> str:
        try:
            hour12 = int(self.cmb_schedule_hour.get())
            minute = int(self.cmb_schedule_minute.get())
        except (TypeError, ValueError):
            return ""
        ampm = (self.cmb_schedule_ampm.get() or "AM").strip().upper()
        if hour12 < 1 or hour12 > 12 or minute < 0 or minute > 59:
            return ""
        if ampm == "AM":
            hour24 = 0 if hour12 == 12 else hour12
        else:
            hour24 = 12 if hour12 == 12 else hour12 + 12
        return f"{hour24:02d}:{minute:02d}"

    def _set_schedule_widgets_from_24h(self, value: str) -> None:
        parsed = self._parse_schedule_hhmm(value) or (4, 0)
        hour24, minute = parsed
        ampm = "AM" if hour24 < 12 else "PM"
        hour12 = hour24 % 12
        if hour12 == 0:
            hour12 = 12
        self.cmb_schedule_hour.set(str(hour12))
        self.cmb_schedule_minute.set(f"{minute:02d}")
        self.cmb_schedule_ampm.set(ampm)

    def _seconds_until_todays_schedule(self) -> float | None:
        target = self._get_schedule_time_24h()
        if not target:
            return None
        try:
            parts = target.split(":")
            hh, mm = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return None
        now = datetime.now()
        try:
            sched = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            return None
        if sched <= now:
            return None
        return (sched - now).total_seconds()

    def _tick_scheduled_restart_warnings(self, proc) -> None:
        """Announce at 30/20/15/10/5/1 minutes before the daily restart."""
        if not proc or self._restart_pending or self._stop_pending:
            self._schedule_prev_secs = None
            return
        secs = self._seconds_until_todays_schedule()
        if secs is None:
            self._schedule_prev_secs = None
            return
        today = date.today()
        if self.last_schedule_date == today:
            # Already restarted (or triggered) for today; no more warnings.
            self._schedule_prev_secs = secs
            return
        # Reset warning bookkeeping at day boundaries.
        warn_day = getattr(self, "_schedule_warn_day", None)
        if warn_day != today:
            self._schedule_warn_day = today
            self._schedule_warn_sent.clear()
        prev = self._schedule_prev_secs
        self._schedule_prev_secs = secs
        if prev is None:
            return
        for minutes in (30, 20, 15, 10, 5, 1):
            if minutes in self._schedule_warn_sent:
                continue
            threshold = minutes * 60
            # Fire once when remaining time crosses this threshold.
            if prev > threshold >= secs:
                self._schedule_warn_sent.add(minutes)
                self._send_schedule_restart_announce(minutes)

    def _send_schedule_restart_announce(self, minutes: int) -> None:
        unit = "minute" if minutes == 1 else "minutes"
        message = f"Server restarting in {minutes} {unit}."
        self.log(f"Scheduled restart announce: {message}")
        enabled, api_port, admin_pw = config_io.read_rest_api_config(self.paths)
        if not enabled or not admin_pw:
            self.log("Could not announce restart warning (REST API unavailable).")
            return

        def worker() -> None:
            _, err = rest_api.announce_message("127.0.0.1", api_port, admin_pw, message)
            if err:
                self.root.after(0, lambda e=err: self.log(f"Restart announce failed: {e}"))

        threading.Thread(target=worker, daemon=True).start()

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

    def _copy_server_info(self) -> None:
        info = self.lbl_server_info.cget("text")
        if info not in ("--", "(pending...)"):
            self.root.clipboard_clear()
            self.root.clipboard_append(info)
            self.log("Server version copied to clipboard.")

    def _on_share(self) -> None:
        name = self.lbl_server_title.cget("text")
        if hasattr(self, "ent_srv_name"):
            ui_name = self.ent_srv_name.get().strip()
            if ui_name:
                name = ui_name
        password = ""
        if hasattr(self, "ent_password"):
            password = self.ent_password.get()
        if not password:
            opts = config_io.read_effective_option_settings(self.paths)
            password = str(opts.get("ServerPassword") or "")
        msg = f'Join my Palworld server "{name}" Password: "{password}"'
        self.root.clipboard_clear()
        self.root.clipboard_append(msg)
        self.log("Invite message copied to clipboard.")

    def _on_save_config(self) -> None:
        if process_ops.get_server_process():
            self.lbl_cfg_status.config(text="Stop the server before saving config.", fg="tomato")
            return
        try:
            updates = self.config_form.collect()
            config_io.merge_option_settings(self.paths, updates)
            self.mgr.launch_arguments = self.config_form.get_launch_arguments()
            settings.save_manager_settings(self.paths, self.mgr, self.client)
            self.lbl_server_title.config(text=str(updates.get("ServerName", "Default Palworld Server")))
            ts = datetime.now().strftime("%H:%M:%S")
            self.lbl_cfg_status.config(text=f"Config saved at {ts}.", fg=self.c["green"])
            self._apply_start_button_state()
            self.log(f"Config saved at {ts}.")
        except OSError as e:
            self.lbl_cfg_status.config(text=f"Error: {e}", fg="tomato")

    def _on_reload_config(self) -> None:
        self._read_server_config_ui()
        self.lbl_cfg_status.config(text="Config reloaded from disk.", fg=self.c["text_dim"])

    def _on_open_server_config(self) -> None:
        if self.paths.config_path.is_file() and self.paths.config_path.stat().st_size > 0:
            subprocess.Popen(["notepad", str(self.paths.config_path)])
        else:
            self.log(
                "PalWorldSettings.ini not found. Open the Config tab and click Save Config to generate it."
            )
            self._select_config_tab()

    def _on_export_logs(self) -> None:
        p = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log", "*.log"), ("Text", "*.txt"), ("All", "*.*")],
            initialfile=f"Palworld-Log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log",
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
                "The Palworld Server Manager will restart automatically.",
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
        found = self._find_steam_palworld()
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
                "Could not auto-detect Palworld in SteamCMD libraries."
                if self.client.install_client == "SteamCMD"
                else "Could not auto-detect Palworld in Steam libraries.",
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
            d = filedialog.askdirectory(title="Select Palworld server folder (PalServer.exe)")
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
            steamcmd_path = str(steamcmd_exe)
            server_location = str(Path(dst).resolve())
            start_cmd = (
                f'start "" "{steamcmd_path}" +force_install_dir "{server_location}" '
                f"+login anonymous +app_update {constants.PALWORLD_STEAM_APP_ID} validate +quit"
            )
            display_cmd = (
                f'start {steamcmd_path} +force_install_dir "{server_location}" '
                f"+login anonymous +app_update {constants.PALWORLD_STEAM_APP_ID} validate +quit"
            )
            self.txt_install_log.delete("1.0", tk.END)
            self.txt_install_log.insert(
                tk.END,
                f"Starting SteamCMD...\n{display_cmd}\n\nA separate SteamCMD window will open.",
            )
            subprocess.Popen(start_cmd, shell=True, cwd=cmd_root)
            return

        src = self.ent_steam_src.get().strip()
        if not src or not (
            (Path(src) / "PalServer.exe").is_file()
            or (Path(src) / "Pal" / "Binaries" / "Win64" / "PalServer-Win64-Shipping-Cmd.exe").is_file()
        ):
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
        dst_exe = Path(dst) / "PalServer.exe"
        dst_cmd = (
            Path(dst) / "Pal" / "Binaries" / "Win64" / "PalServer-Win64-Shipping-Cmd.exe"
        )
        if dst_exe.is_file() or dst_cmd.is_file():
            self.paths.set_root(dst)
            self.paths.ensure_backup_dir()
            self.client.server_root = str(self.paths.server_dir)
            settings.save_client_settings(self.paths, self.client)
            self._save_bootstrap_client_settings()
            self.canvas_install.itemconfig(self._install_dot, fill="#00FF00")
            self.lbl_install_status.config(text="Server installed successfully.", fg=self.c["green"])
            self.txt_install_log.insert(tk.END, "\n\nInstall complete!")
            self._read_server_config_ui()
            self._update_setup_wizard()
        else:
            self.txt_install_log.insert(tk.END, "\n\nWARNING: PalServer.exe not found at destination.")

    def _on_check_reqs(self) -> None:
        if self.client.install_client == "SteamCMD":
            td = self.ent_install_dest.get().strip()
            if td:
                self.client.steamcmd_force_install_dir = td
                settings.save_client_settings(self.paths, self.client)
                self._save_bootstrap_client_settings()
        found = self._find_steam_palworld()
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
        elif self.paths.server_installed():
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
