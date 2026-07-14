from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import constants
from .paths import ServerPaths


def read_invite_code(paths: ServerPaths) -> str | None:
    if not paths.config_path.is_file():
        return None
    try:
        data = json.loads(paths.config_path.read_text(encoding="utf-8"))
        inner = data.get("ServerDescription_Persistent") or {}
        code = inner.get("InviteCode")
        return str(code) if code else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def find_world_config(paths: ServerPaths) -> Path | None:
    rock = paths.server_dir / "R5" / "Saved" / "SaveProfiles" / "Default" / "RocksDB"
    if not rock.is_dir():
        return None
    newest: Path | None = None
    newest_mtime = -1.0
    try:
        for f in rock.rglob("WorldDescription.json"):
            if f.is_file():
                m = f.stat().st_mtime
                if m > newest_mtime:
                    newest_mtime = m
                    newest = f
    except OSError:
        return None
    return newest


def read_server_config_dict(paths: ServerPaths) -> dict[str, Any] | None:
    if not paths.config_path.is_file():
        return None
    try:
        return json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_world_config_dict(wpath: Path) -> dict[str, Any] | None:
    if not wpath.is_file():
        return None
    try:
        return json.loads(wpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def default_server_description_inner() -> dict[str, Any]:
    return {
        "PersistentServerId": "",
        "InviteCode": "",
        "IsPasswordProtected": False,
        "Password": "",
        "ServerName": "My Windrose Server",
        "WorldIslandId": "",
        "MaxPlayerCount": 10,
        "UserSelectedRegion": "",
        "P2pProxyAddress": "127.0.0.1",
        "UseDirectConnection": False,
        "DirectConnectionServerAddress": "",
        "DirectConnectionServerPort": 7777,
        "DirectConnectionProxyAddress": "0.0.0.0",
    }


def write_minimal_server_config(paths: ServerPaths) -> None:
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    root = {
        "Version": 1,
        "DeploymentId": "",
        "ServerDescription_Persistent": default_server_description_inner(),
    }
    paths.config_path.write_text(json.dumps(root, indent=2), encoding="utf-8")


def build_world_save_payload(
    *,
    paths: ServerPaths,
    preset: str,
    combat_short: str,
    floats: dict[str, float],
    bools: dict[str, bool],
    existing_world: dict[str, Any] | None,
) -> dict[str, Any]:
    wdesc = (existing_world or {}).get("WorldDescription") or {}
    island_id = wdesc.get("islandId", "")
    world_name = wdesc.get("WorldName", "")
    creation_time = wdesc.get("CreationTime", 0)
    float_params = {constants.FLOAT_PARAM_KEYS[k]: round(v, 2) for k, v in floats.items()}
    bool_params = {constants.BOOL_PARAM_KEYS[k]: v for k, v in bools.items()}
    tag_params = {
        constants.TAG_COMBAT_KEY: {"TagName": f"WDS.Parameter.CombatDifficulty.{combat_short}"}
    }
    ver = 1
    if existing_world and "Version" in existing_world:
        ver = existing_world["Version"]
    return {
        "Version": ver,
        "WorldDescription": {
            "islandId": island_id,
            "WorldName": world_name,
            "CreationTime": creation_time,
            "WorldPresetType": preset,
            "WorldSettings": {
                "BoolParameters": bool_params,
                "FloatParameters": float_params,
                "TagParameters": tag_params,
            },
        },
    }


def parse_combat_from_world(wd: dict[str, Any]) -> str:
    tp = (wd.get("WorldSettings") or {}).get("TagParameters") or {}
    prop = tp.get(constants.TAG_COMBAT_KEY)
    if not prop or not isinstance(prop, dict):
        return "Normal"
    tag = prop.get("TagName") or ""
    m = re.search(r"\.CombatDifficulty\.(.+)$", str(tag))
    return m.group(1) if m else "Normal"


def extract_floats_from_world(ws: dict[str, Any]) -> dict[str, float]:
    fp = ws.get("FloatParameters") or {}
    out: dict[str, float] = {}
    key_by_val = {v: k for k, v in constants.FLOAT_PARAM_KEYS.items()}
    for json_key, short in key_by_val.items():
        if json_key in fp:
            try:
                out[short] = float(fp[json_key])
            except (TypeError, ValueError):
                pass
    return out


def extract_bools_from_world(ws: dict[str, Any]) -> dict[str, bool]:
    bp = ws.get("BoolParameters") or {}
    out: dict[str, bool] = {}
    for short, json_key in constants.BOOL_PARAM_KEYS.items():
        if json_key in bp:
            out[short] = bool(bp[json_key])
    return out
