from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any


def _auth_header(admin_password: str) -> str:
    token = base64.b64encode(f"admin:{admin_password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def api_request(
    *,
    host: str = "127.0.0.1",
    port: int,
    admin_password: str,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    if not admin_password:
        return None, "Admin password is not configured."
    url = f"http://{host}:{port}/v1/api{path}"
    data = None
    headers = {
        "Authorization": _auth_header(admin_password),
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}, None
            return json.loads(raw), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except OSError:
            detail = str(e)
        return None, f"HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        return None, str(e.reason)
    except (OSError, json.JSONDecodeError, TimeoutError) as e:
        return None, str(e)


def get_server_info(host: str, port: int, admin_password: str) -> tuple[dict[str, Any] | None, str | None]:
    data, err = api_request(host=host, port=port, admin_password=admin_password, path="/info")
    return data if isinstance(data, dict) else None, err


def get_server_settings(host: str, port: int, admin_password: str) -> tuple[dict[str, Any] | None, str | None]:
    data, err = api_request(host=host, port=port, admin_password=admin_password, path="/settings")
    return data if isinstance(data, dict) else None, err


def get_server_metrics(host: str, port: int, admin_password: str) -> tuple[dict[str, Any] | None, str | None]:
    data, err = api_request(host=host, port=port, admin_password=admin_password, path="/metrics")
    return data if isinstance(data, dict) else None, err


def get_players(host: str, port: int, admin_password: str) -> tuple[list[dict[str, Any]], str | None]:
    data, err = api_request(host=host, port=port, admin_password=admin_password, path="/players")
    if err:
        return [], err
    if isinstance(data, dict):
        players = data.get("players")
        if isinstance(players, list):
            return players, None
    if isinstance(data, list):
        return data, None
    return [], None


def player_display_name(player: dict[str, Any]) -> str:
    for key in ("name", "playerName", "accountName"):
        val = player.get(key)
        if val:
            return str(val)
    uid = player.get("playerUid") or player.get("userId") or player.get("userid")
    return str(uid) if uid else "Unknown"


def player_user_id(player: dict[str, Any]) -> str:
    for key in ("userId", "userid", "playerId", "playerUid"):
        val = player.get(key)
        if val:
            return str(val)
    return ""


def announce_message(
    host: str, port: int, admin_password: str, message: str
) -> tuple[dict[str, Any] | None, str | None]:
    data, err = api_request(
        host=host,
        port=port,
        admin_password=admin_password,
        path="/announce",
        method="POST",
        body={"message": message},
    )
    if err:
        return None, err
    return data if isinstance(data, dict) else {}, None


def kick_player(
    host: str,
    port: int,
    admin_password: str,
    userid: str,
    message: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    body: dict[str, Any] = {"userid": userid}
    if message:
        body["message"] = message
    data, err = api_request(
        host=host,
        port=port,
        admin_password=admin_password,
        path="/kick",
        method="POST",
        body=body,
    )
    if err:
        return None, err
    return data if isinstance(data, dict) else {}, None


def ban_player(
    host: str,
    port: int,
    admin_password: str,
    userid: str,
    message: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    body: dict[str, Any] = {"userid": userid}
    if message:
        body["message"] = message
    data, err = api_request(
        host=host,
        port=port,
        admin_password=admin_password,
        path="/ban",
        method="POST",
        body=body,
    )
    if err:
        return None, err
    return data if isinstance(data, dict) else {}, None


def unban_player(
    host: str, port: int, admin_password: str, userid: str
) -> tuple[dict[str, Any] | None, str | None]:
    data, err = api_request(
        host=host,
        port=port,
        admin_password=admin_password,
        path="/unban",
        method="POST",
        body={"userid": userid},
    )
    if err:
        return None, err
    return data if isinstance(data, dict) else {}, None


def save_world(host: str, port: int, admin_password: str) -> tuple[dict[str, Any] | None, str | None]:
    data, err = api_request(
        host=host,
        port=port,
        admin_password=admin_password,
        path="/save",
        method="POST",
    )
    if err:
        return None, err
    return data if isinstance(data, dict) else {}, None


def shutdown_server(
    host: str,
    port: int,
    admin_password: str,
    *,
    waittime: int = 1,
    message: str = "Server shutting down.",
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Graceful shutdown via POST /v1/api/shutdown.
    See https://docs.palworldgame.com/api/rest-api/shutdown
    """
    data, err = api_request(
        host=host,
        port=port,
        admin_password=admin_password,
        path="/shutdown",
        method="POST",
        body={"waittime": int(waittime), "message": message},
        timeout=max(10.0, float(waittime) + 5.0),
    )
    if err:
        return None, err
    return data if isinstance(data, dict) else {}, None
