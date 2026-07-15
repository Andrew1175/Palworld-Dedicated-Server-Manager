"""DEPRECATED: This module is superseded by discord_bot.DiscordBotService.

Kept as a compatibility shim so that any remaining import does not crash
the application. All functions emit a DeprecationWarning and are no-ops.
"""
from __future__ import annotations

import warnings


def is_valid_discord_webhook_url(url: str) -> bool:
    warnings.warn(
        "is_valid_discord_webhook_url() is deprecated. "
        "Configure a Discord Bot Token in Settings instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return False


def send_discord_webhook(
    url: str, content: str, timeout: float = 12.0
) -> tuple[bool, str]:
    warnings.warn(
        "send_discord_webhook() is deprecated. "
        "Use DiscordBotService.queue_event() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return False, (
        "Webhook system has been replaced by a Discord Bot. "
        "Please reconfigure your Discord settings (Bot Token, Guild ID, Channel ID)."
    )
