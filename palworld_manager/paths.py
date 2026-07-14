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
        return self.server_dir / "windrose_client_settings.json"

    @property
    def steamcmd_sidecar(self) -> Path:
        return self.server_dir / "WindroseManagerSteamCMDRoot.txt"

    @property
    def server_exe(self) -> Path:
        return self.server_dir / "WindroseServer.exe"

    @property
    def server_exe_direct(self) -> Path:
        return self.server_dir / "R5" / "Binaries" / "Win64" / "WindroseServer-Win64-Shipping.exe"

    @property
    def config_path(self) -> Path:
        return self.server_dir / "R5" / "ServerDescription.json"

    @property
    def log_path(self) -> Path:
        return self.server_dir / "R5" / "Saved" / "Logs" / "R5.log"

    @property
    def saves_base(self) -> Path:
        return self.server_dir / "R5" / "Saved" / "SaveProfiles"

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
