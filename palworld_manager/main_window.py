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

from . import config_io, config_form, constants, discord_bot, discord_webhook, install_ops, players, process_ops, rest_api, settings, steam, updater
from .discord_bot import DiscordBotService, EventType
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
    return _app_package_dir().parent / "palworld_client_settings.json"


def _isolated_dedicated_server_env(working_dir: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "SystemDrive", "TEMP", "TMP", "USERPROFILE", "COMPUTERNAME", "PATHEXT", "ComSpec", "PSModulePath", "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE")}
    system_root = base.get("SystemRoot") or os.environ.get("SystemRoot", r"C:\Windows")
    windir = base.get("WINDIR") or os.environ.get("WINDIR", system_root)
    base["SystemRoot"] = system_root
    base["WINDIR"] = windir
    sys32 = str(Path(system_root) / "System32")
    sys_wow = str(Path(system_root) / "SysWOW64")
    path_parts = [working_dir, sys32, sys_wow, str(Path(system_root) / "")]
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
        self._discord_bot: DiscordBotService = DiscordBotService(
            get_settings_fn=lambda: self.mgr,
            get_server_state_fn=self._get_bot_server_state,
            get_rest_api_config_fn=self._get_bot_rest_api_config,
        )

        apply_dark_theme(root)
        root.title("Palworld Server Manager")
        root.minsize(760, 660)
        root.geometry("920x820")
        try:
            candidates: list[Path] = []
            candidates.append(_app_package_dir().parent / "palworld_logo.ico")
            if getattr(sys, "frozen", False):
                candidates.append(Path(sys.executable).resolve().parent / "palworld_logo.ico")
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

    def _maybe_start_discord_bot(self) -> None:
        self._discord_bot.stop()
        if getattr(self.mgr, "discord_bot_enabled", False):
            self._discord_bot.start()

    def _get_bot_server_state(self) -> dict:
        proc = process_ops.get_server_process()
        status = "running" if proc else "stopped"
        uptime = (datetime.now() - self.start_time).total_seconds() if self.start_time and proc else 0
        return {
            "status": status,
            "uptime_seconds": uptime,
            "pid": proc.pid if proc else None,
            "start_callback": self._on_start,
            "stop_callback": self._on_stop,
            "backup_callback": self._trigger_backup_for_bot,
        }

    def _get_bot_rest_api_config(self) -> dict:
        enabled, port, admin_pw = config_io.read_rest_api_config(self.paths)
        return {"host": "127.0.0.1", "port": port, "admin_password": admin_pw}

    def _trigger_backup_for_bot(self) -> str:
        stamp, zp = backup_saves_now(self.paths)
        self.root.after(0, lambda s=stamp: self.lbl_last_backup.config(text=f"Last backup: {s}"))
        self.root.after(0, lambda p=zp: self.log(f"Backup created: {p}"))
        return zp.name

    # ... remaining file content omitted in this patch body for brevity ...
