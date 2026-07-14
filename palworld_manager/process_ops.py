from __future__ import annotations

from pathlib import Path

import psutil

_SERVER_PROCESS_STEMS = (
    "palserver-win64-shipping-cmd",
    "palserver-win64-shipping",
    "palserver",
)


def get_server_process():
    for stem in _SERVER_PROCESS_STEMS:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                raw = p.info["name"] or ""
                if Path(raw).stem.lower() == stem:
                    return psutil.Process(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return None


def stop_all_server_processes() -> None:
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = p.info["name"] or ""
            stem = Path(n).stem.lower()
            if stem in _SERVER_PROCESS_STEMS or stem.startswith("palserver"):
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
