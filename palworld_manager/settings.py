from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ClientInstallSettings:
    install_client_choice_saved: bool = False
    install_client: str = "Steam"
    steam_install_root: str | None = None
    steamcmd_install_root: str | None = None
    steamcmd_force_install_dir: str | None = None
    server_root: str | None = None


def sync_steamcmd_sidecar(
    install_client: str, force_dir: str | None, sidecar: Path
) -> None:
    if install_client != "SteamCMD":
        try:
            if sidecar.is_file():
                sidecar.unlink()
        except OSError:
            pass
        return
    if not force_dir:
        return
    try:
        sidecar.write_text(force_dir.rstrip("\\/"), encoding="utf-8")
    except OSError:
        pass


def import_steamcmd_force_from_sidecar(sidecar: Path) -> str | None:
    if not sidecar.is_file():
        return None
    try:
        line = sidecar.read_text(encoding="utf-8").strip()
        return line.rstrip("\\/") if line else None
    except OSError:
        return None


def read_client_settings(paths) -> ClientInstallSettings | None:
    if paths.client_settings_file.is_file():
        try:
            data = json.loads(paths.client_settings_file.read_text(encoding="utf-8"))
            if not data.get("InstallClientChoiceSaved"):
                return None
            ic = data.get("InstallClient")
            if ic not in ("Steam", "SteamCMD"):
                return None
            return ClientInstallSettings(
                install_client_choice_saved=True,
                install_client=ic,
                steam_install_root=_norm(data.get("SteamInstallRoot")),
                steamcmd_install_root=_norm(data.get("SteamCmdInstallRoot")),
                steamcmd_force_install_dir=_norm(data.get("SteamCmdForceInstallDir")),
                server_root=_norm(data.get("ServerRoot")),
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    if paths.settings_file.is_file():
        try:
            legacy = json.loads(paths.settings_file.read_text(encoding="utf-8"))
            if legacy.get("InstallClientChoiceSaved") and legacy.get("InstallClient"):
                s = ClientInstallSettings(
                    install_client_choice_saved=True,
                    install_client=str(legacy["InstallClient"]),
                    steam_install_root=_norm(legacy.get("SteamInstallRoot")),
                    steamcmd_install_root=_norm(legacy.get("SteamCmdInstallRoot")),
                    steamcmd_force_install_dir=_norm(legacy.get("SteamCmdForceInstallDir")),
                    server_root=_norm(legacy.get("ServerRoot")),
                )
                if s.install_client not in ("Steam", "SteamCMD"):
                    return None
                save_client_settings(paths, s)
                return s
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return None


def _norm(p: Any) -> str | None:
    if not p or not str(p).strip():
        return None
    return str(p).rstrip("\\/")


def save_client_settings(paths, s: ClientInstallSettings) -> None:
    try:
        payload = {
            "InstallClientChoiceSaved": s.install_client_choice_saved,
            "InstallClient": s.install_client,
            "SteamInstallRoot": s.steam_install_root,
            "SteamCmdInstallRoot": s.steamcmd_install_root,
            "SteamCmdForceInstallDir": s.steamcmd_force_install_dir,
            "ServerRoot": s.server_root,
        }
        paths.client_settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        sync_steamcmd_sidecar(s.install_client, s.steamcmd_force_install_dir, paths.steamcmd_sidecar)
    except OSError:
        pass


DEFAULT_DISCORD_MSG_STOP = "Windrose dedicated server **stopped**."
DEFAULT_DISCORD_MSG_RESTART = "Windrose dedicated server is **restarting** (manual)."
DEFAULT_DISCORD_MSG_SCHEDULE = "Windrose dedicated server **scheduled restart** started."
DEFAULT_DISCORD_MSG_CRASH = (
    "Windrose dedicated server process **ended unexpectedly** (possible crash)."
)


@dataclass
class ManagerSettings:
    auto_restart: bool = False
    crash_count: int = 0
    auto_backup: bool = False
    backup_interval_value: int = 4
    backup_interval_unit: str = "hours"
    schedule_enabled: bool = False
    schedule_time: str = "04:00"
    steamcmd_force_install_dir: str | None = None
    discord_webhook_enabled: bool = False
    discord_webhook_url: str = ""
    discord_msg_stop: str = DEFAULT_DISCORD_MSG_STOP
    discord_msg_restart: str = DEFAULT_DISCORD_MSG_RESTART
    discord_msg_schedule: str = DEFAULT_DISCORD_MSG_SCHEDULE
    discord_msg_crash: str = DEFAULT_DISCORD_MSG_CRASH


def load_manager_settings(paths) -> ManagerSettings:
    m = ManagerSettings()
    if not paths.settings_file.is_file():
        return m
    try:
        s = json.loads(paths.settings_file.read_text(encoding="utf-8"))
        if "AutoRestart" in s:
            m.auto_restart = bool(s["AutoRestart"])
        if "CrashCount" in s:
            m.crash_count = max(0, int(s["CrashCount"]))
        if "AutoBackup" in s:
            m.auto_backup = bool(s["AutoBackup"])
        if "BackupIntervalValue" in s:
            m.backup_interval_value = max(1, int(s["BackupIntervalValue"]))
        elif "BackupInterval" in s:
            # Backward compatibility with old combobox index storage.
            idx = int(s["BackupInterval"])
            m.backup_interval_value = (1, 4, 8, 16, 24)[idx] if 0 <= idx < 5 else 4
        if "BackupIntervalUnit" in s:
            u = str(s["BackupIntervalUnit"]).strip().lower()
            m.backup_interval_unit = "minutes" if u.startswith("minute") else "hours"
        if "ScheduleEnabled" in s:
            m.schedule_enabled = bool(s["ScheduleEnabled"])
        if s.get("ScheduleTime"):
            m.schedule_time = str(s["ScheduleTime"])
        if s.get("SteamCmdForceInstallDir"):
            m.steamcmd_force_install_dir = str(s["SteamCmdForceInstallDir"]).rstrip("\\/")
        if "DiscordWebhookEnabled" in s:
            m.discord_webhook_enabled = bool(s["DiscordWebhookEnabled"])
        if "DiscordWebhookUrl" in s:
            m.discord_webhook_url = str(s.get("DiscordWebhookUrl") or "").strip()
        if "DiscordMsgStop" in s:
            m.discord_msg_stop = str(s.get("DiscordMsgStop") or "").strip() or DEFAULT_DISCORD_MSG_STOP
        if "DiscordMsgRestart" in s:
            m.discord_msg_restart = str(s.get("DiscordMsgRestart") or "").strip() or DEFAULT_DISCORD_MSG_RESTART
        if "DiscordMsgSchedule" in s:
            m.discord_msg_schedule = str(s.get("DiscordMsgSchedule") or "").strip() or DEFAULT_DISCORD_MSG_SCHEDULE
        if "DiscordMsgCrash" in s:
            m.discord_msg_crash = str(s.get("DiscordMsgCrash") or "").strip() or DEFAULT_DISCORD_MSG_CRASH
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return m


def save_manager_settings(
    paths,
    m: ManagerSettings,
    client: ClientInstallSettings,
) -> None:
    payload = {
        "AutoRestart": m.auto_restart,
        "CrashCount": m.crash_count,
        "AutoBackup": m.auto_backup,
        "BackupInterval": m.backup_interval_value,
        "BackupIntervalValue": m.backup_interval_value,
        "BackupIntervalUnit": m.backup_interval_unit,
        "ScheduleEnabled": m.schedule_enabled,
        "ScheduleTime": m.schedule_time,
        "InstallClientChoiceSaved": client.install_client_choice_saved,
        "InstallClient": client.install_client,
        "SteamInstallRoot": client.steam_install_root,
        "SteamCmdInstallRoot": client.steamcmd_install_root,
        "SteamCmdForceInstallDir": client.steamcmd_force_install_dir,
        "DiscordWebhookEnabled": m.discord_webhook_enabled,
        "DiscordWebhookUrl": m.discord_webhook_url,
        "DiscordMsgStop": m.discord_msg_stop,
        "DiscordMsgRestart": m.discord_msg_restart,
        "DiscordMsgSchedule": m.discord_msg_schedule,
        "DiscordMsgCrash": m.discord_msg_crash,
    }
    try:
        paths.settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        sync_steamcmd_sidecar(
            client.install_client, client.steamcmd_force_install_dir, paths.steamcmd_sidecar
        )
    except OSError:
        pass
