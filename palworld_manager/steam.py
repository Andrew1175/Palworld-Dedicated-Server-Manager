from __future__ import annotations

import re
import os
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None  # type: ignore


def get_steam_install_root() -> Path | None:
    candidates: list[str] = []
    if winreg:
        for hive, sub, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, key)
                    if val:
                        candidates.append(str(val).replace("/", "\\").rstrip("\\"))
            except OSError:
                pass
    for extra in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if extra:
            candidates.append(str(Path(extra) / "Steam"))
    candidates.extend([r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"])
    seen: set[str] = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        steam_exe = Path(p) / "steam.exe"
        if steam_exe.is_file():
            return Path(p)
    return None


def get_steamcmd_install_root(manager_parent: Path) -> Path | None:
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [
        Path(r"C:\SteamCMD"),
        Path(pf86) / "SteamCMD",
        Path(pf) / "SteamCMD",
        manager_parent / "SteamCMD",
    ]
    for p in candidates:
        if (p / "steamcmd.exe").is_file():
            return p
    return None


def get_steam_library_roots(install_root: Path | None) -> list[Path]:
    if not install_root:
        return []
    roots: list[Path] = []
    ir = install_root.resolve()
    roots.append(ir)
    vdf = ir / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                raw = m.group(1).replace("\\\\", "\\")
                if raw:
                    roots.append(Path(raw))
        except OSError:
            pass
    # unique preserve order
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = str(r.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def find_windrose_server_in_libraries(library_roots: list[Path]) -> Path | None:
    exact_suffixes = [
        Path(r"steamapps\common\Windrose\R5\Builds\WindowsServer"),
        Path(r"steamapps\common\Windrose Dedicated Server\R5\Builds\WindowsServer"),
        Path(r"steamapps\common\Windrose\Builds\WindowsServer"),
        Path(r"steamapps\common\Windrose Dedicated Server\Builds\WindowsServer"),
        Path(r"steamapps\common\Windrose\WindowsServer"),
        Path(r"steamapps\common\Windrose Dedicated Server\WindowsServer"),
    ]
    for lib in library_roots:
        lib = lib.resolve()
        for suf in exact_suffixes:
            p = lib / suf
            if (p / "WindroseServer.exe").is_file():
                return p
        common = lib / "steamapps" / "common"
        if not common.is_dir():
            continue
        try:
            for app_dir in common.iterdir():
                if not app_dir.is_dir():
                    continue
                for candidate in (
                    app_dir / "R5" / "Builds" / "WindowsServer" / "WindroseServer.exe",
                    app_dir / "Builds" / "WindowsServer" / "WindroseServer.exe",
                    app_dir / "WindowsServer" / "WindroseServer.exe",
                ):
                    if candidate.is_file():
                        return candidate.parent
                for hit in app_dir.rglob("WindroseServer.exe"):
                    if hit.is_file():
                        return hit.parent
        except OSError:
            continue
    return None


def find_steam_windrose(
    client: str,
    *,
    steam_install_root: Path | None,
    steamcmd_install_root: Path | None,
    steamcmd_force_install_dir: Path | None,
) -> Path | None:
    if client == "SteamCMD":
        if steamcmd_force_install_dir:
            fr = steamcmd_force_install_dir
            if (fr / "WindroseServer.exe").is_file():
                return fr
            if (fr / "R5" / "Binaries" / "Win64" / "WindroseServer-Win64-Shipping.exe").is_file():
                return fr
        cmd_root = steamcmd_install_root
        roots_set: dict[str, Path] = {}
        if cmd_root:
            for r in get_steam_library_roots(cmd_root):
                roots_set[str(r).lower()] = r
        if steamcmd_force_install_dir:
            for r in get_steam_library_roots(steamcmd_force_install_dir):
                roots_set[str(r).lower()] = r
        if roots_set:
            return find_windrose_server_in_libraries(list(roots_set.values()))
        return None

    steam_root = steam_install_root
    if not steam_root:
        return None
    libs = get_steam_library_roots(steam_root)
    return find_windrose_server_in_libraries(libs)
