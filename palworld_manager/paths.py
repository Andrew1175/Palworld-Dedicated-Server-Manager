from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ServerPaths:

    server_dir: Path

    def __post_init__(self):
        try:
            self.server_dir = self.server_dir.expanduser().resolve()
        except OSError:
            self.server_dir = self.server_dir.expanduser().absolute()

    @property
    def client_settings_file(self) -> Path:
        return self.server_dir / "palworld_client_settings.json"

    @property
    def steamcmd_sidecar(self) -> Path:
        return self.server_dir / "PalworldManagerSteamCMDRoot.txt"

    @property
    def server_exe(self) -> Path:
        return self.server_dir / "PalServer.exe"

    @property
    def server_exe_direct(self) -> Path:
        return (
            self.server_dir
            / "Pal"
            / "Binaries"
            / "Win64"
            / "PalServer-Win64-Shipping-Cmd.exe"
        )

    @property
    def config_path(self) -> Path:
        return (
            self.server_dir
            / "Pal"
            / "Saved"
            / "Config"
            / "WindowsServer"
            / "PalWorldSettings.ini"
        )

    @property
    def default_config_template(self) -> Path:
        return self.server_dir / "DefaultPalWorldSettings.ini"

    @property
    def log_path(self) -> Path:
        # Palworld does not write console output to disk; the manager mirrors
        # captured stdout here so the Log tab / export have a stable file.
        return self.server_dir / "manager_console.log"

    @property
    def saves_base(self) -> Path:
        return self.server_dir / "Pal" / "Saved" / "SaveGames"

    @property
    def backup_dir(self) -> Path:
        return self.server_dir / "Backups"

    @property
    def history_file(self) -> Path:
        return self.server_dir / "player_history.txt"

    @property
    def settings_file(self) -> Path:
        return self.server_dir / "manager_settings.json"

    @property
    def insights_file(self) -> Path:
        return self.server_dir / "insights_data.json"

    def set_root(self, root: str | os.PathLike[str]) -> None:
        self.server_dir = Path(root).expanduser()
        self.__post_init__()

    def ensure_backup_dir(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def server_installed(self) -> bool:
        return self.server_exe.is_file() or self.server_exe_direct.is_file()
