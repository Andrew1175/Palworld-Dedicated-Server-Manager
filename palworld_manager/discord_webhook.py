from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import constants

# Discord/Cloudflare often returns 403 for the default Python-urllib User-Agent.
_USER_AGENT = (
    f"WindroseServerManager/{constants.APP_VERSION} "
    "(+https://github.com/Andrew1175/Windrose-Server-Manager-Enhanced)"
)

_ALLOWED = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)


def is_valid_discord_webhook_url(url: str) -> bool:
    u = (url or "").strip()
    if not u or len(u) > 2048:
        return False
    return u.startswith(_ALLOWED)


def send_discord_webhook(url: str, content: str, timeout: float = 12.0) -> tuple[bool, str]:
    """POST plain text content to a Discord webhook. Returns (success, error_detail)."""
    text = (content or "").strip() or "(empty)"
    if len(text) > 2000:
        text = text[:1997] + "..."
    # Without this, <@id> in content is shown as text but may not actually notify the user/role.
    # https://discord.com/developers/docs/resources/channel#allowed-mentions-object
    payload: dict = {
        "content": text,
        "allowed_mentions": {
            "parse": ["users", "roles"],
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url.strip(),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except URLError as e:
        reason = e.reason if isinstance(e.reason, str) else getattr(e.reason, "strerror", str(e.reason))
        return False, reason or "connection error"
    except OSError as e:
        return False, str(e) or "I/O error"
