from __future__ import annotations

from pathlib import Path

import psutil


def get_server_process():
    for p in psutil.process_iter(["pid", "name"]):
        try:
            raw = p.info["name"] or ""
            stem = Path(raw).stem.lower()
            if stem == "windroseserver-win64-shipping":
                return psutil.Process(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for p in psutil.process_iter(["pid", "name"]):
        try:
            raw = p.info["name"] or ""
            stem = Path(raw).stem.lower()
            if stem == "windroseserver":
                return psutil.Process(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def stop_all_server_processes() -> None:
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = p.info["name"] or ""
            if n.lower().startswith("windroseserver"):
                psutil.Process(p.info["pid"]).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def server_exe_running(server_exe: Path) -> bool:
    name = server_exe.name
    for p in psutil.process_iter(["exe"]):
        try:
            exe = p.info["exe"]
            if exe and Path(exe).name.lower() == name.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False
