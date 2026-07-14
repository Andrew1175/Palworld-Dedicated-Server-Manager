from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

from .paths import ServerPaths


def backup_saves_now(paths: ServerPaths) -> tuple[str, Path]:
    paths.ensure_backup_dir()
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    zip_path = paths.backup_dir / f"Backup_{stamp}.zip"
    base = paths.saves_base
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in base.rglob("*"):
            if p.is_file():
                arc = p.relative_to(base)
                zf.write(p, arc.as_posix())
    return stamp, zip_path


def find_latest_backup(paths: ServerPaths) -> Path | None:
    if not paths.backup_dir.is_dir():
        return None
    zips = list(paths.backup_dir.glob("Backup_*.zip"))
    if not zips:
        return None
    return max(zips, key=lambda p: p.stat().st_mtime)
