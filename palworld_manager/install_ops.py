from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path


STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"


def install_steamcmd_from_official_zip(parent_directory: Path) -> Path | None:
    steamcmd_dir = parent_directory / "SteamCMD"
    try:
        steamcmd_dir.mkdir(parents=True, exist_ok=True)
        exe = steamcmd_dir / "steamcmd.exe"
        if exe.is_file():
            return steamcmd_dir
        zip_path = steamcmd_dir / "steamcmd.zip"
        req = urllib.request.Request(
            STEAMCMD_ZIP_URL, headers={"User-Agent": "Windrose-Server-Manager"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            zip_path.write_bytes(resp.read())
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(steamcmd_dir)
        zip_path.unlink(missing_ok=True)
        if not (steamcmd_dir / "steamcmd.exe").is_file():
            return None
        return steamcmd_dir
    except OSError:
        return None


def robocopy_install(src: Path, dst: Path, log_path: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    cmd = [
        "robocopy",
        str(src),
        str(dst),
        "/E",
        "/IS",
        "/IT",
        "/NP",
        f"/LOG+:{log_path}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    return r.returncode


def shutil_copytree_install(src: Path, dst: Path) -> None:
    """Fallback when robocopy is unavailable."""
    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n == "install.log"}

    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=_ignore)
