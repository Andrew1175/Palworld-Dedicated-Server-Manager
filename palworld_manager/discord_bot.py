"""Discord Bot service for Palworld Dedicated Server Manager.

Replaces the simple webhook system with a full Discord bot using discord.py.
Inspired by WindroseServerManager DiscordBotService.cs architecture.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

import discord
from discord import app_commands

from . import constants
from . import rest_api

log = logging.getLogger(__name__)


class EventType(str, Enum):
    SERVER_STARTED = "started"
    SERVER_STOPPED = "stopped"
    SERVER_CRASHED = "crashed"
    SERVER_RESTART_SCHEDULED = "restart_scheduled"
    SERVER_RESTART_MANUAL = "restart_manual"
    BACKUP_SUCCESS = "backup_success"
    BACKUP_FAILED = "backup_failed"
    UPDATE_STARTED = "update_started"
    UPDATE_DONE = "update_done"
    PLAYER_JOINED = "player_joined"
    PLAYER_LEFT = "player_left"


_EVENT_META: dict[str, tuple[discord.Color, str, str]] = {
    EventType.SERVER_STARTED:           (discord.Color.green(),        "🟢", "Server Started"),
    EventType.SERVER_STOPPED:           (discord.Color.red(),          "🔴", "Server Stopped"),
    EventType.SERVER_CRASHED:           (discord.Color.red(),          "⚠️", "Crash Detected"),
    EventType.SERVER_RESTART_SCHEDULED: (discord.Color.blurple(),      "🔄", "Scheduled Restart"),
    EventType.SERVER_RESTART_MANUAL:    (discord.Color.blurple(),      "🔄", "Manual Restart"),
    EventType.BACKUP_SUCCESS:           (discord.Color.green(),        "✅", "Backup Created"),
    EventType.BACKUP_FAILED:            (discord.Color.red(),          "❌", "Backup Failed"),
    EventType.UPDATE_STARTED:           (discord.Color.yellow(),       "🔧", "Update In Progress"),
    EventType.UPDATE_DONE:              (discord.Color.green(),        "✅", "Update Complete"),
    EventType.PLAYER_JOINED:            (discord.Color.green(),        "👋", "Player Joined"),
    EventType.PLAYER_LEFT:              (discord.Color(0xE67E22),      "🚪", "Player Left"),
}


class _BotEvent:
    __slots__ = ("type", "kwargs")

    def __init__(self, event_type: EventType, **kwargs: Any) -> None:
        self.type = event_type
        self.kwargs = kwargs


class DiscordBotService:
    """Thread-safe Discord bot service.

    Runs an asyncio event loop in a daemon thread so it never blocks the
    tkinter main thread.  All public methods are safe to call from any thread.
    """

    _LOG_FLUSH_INTERVAL = 3        # seconds
    _PRESENCE_UPDATE_INTERVAL = 60 # seconds
    _PLAYER_POLL_INTERVAL = 15     # seconds
    _LOG_MAX_CHARS = 1900

    def __init__(
        self,
        get_settings_fn: Callable[[], Any],
        get_server_state_fn: Callable[[], dict[str, Any]],
        get_rest_api_config_fn: Callable[[], dict[str, Any]],
    ) -> None:
        self._get_settings = get_settings_fn
        self._get_server_state = get_server_state_fn
        self._get_rest_api_config = get_rest_api_config_fn

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: discord.Client | None = None
        self._tree: app_commands.CommandTree | None = None
        self._stop_event = threading.Event()
        self._is_ready = False
        self._log_queue: asyncio.Queue[_BotEvent] | None = None
        self._known_players: set[str] = set()
        self._last_presence_update = datetime.min

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the bot in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="DiscordBot")
        self._thread.start()
        log.info("Discord bot thread started")

    def stop(self) -> None:
        """Request a graceful shutdown and wait briefly."""
        self._stop_event.set()
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        if self._thread:
            self._thread.join(timeout=8)
        log.info("Discord bot stopped")

    def queue_event(self, event_type: EventType, **kwargs: Any) -> None:
        """Enqueue a server event to be sent to Discord (non-blocking)."""
        if not self._loop or self._loop.is_closed() or self._log_queue is None:
            return
        evt = _BotEvent(event_type, **kwargs)
        asyncio.run_coroutine_threadsafe(self._log_queue.put(evt), self._loop)

    def is_connected(self) -> bool:
        """Return True if the bot is connected and ready."""
        return self._is_ready and self._client is not None and not self._client.is_closed()

    def get_status_label(self) -> str:
        """Return a short status string for UI display."""
        s = self._get_settings()
        if not getattr(s, "discord_bot_enabled", False):
            return "○ Discord Bot disabled"
        if self.is_connected():
            return "● Discord Bot connected"
        return "○ Discord Bot disconnected"

    # ------------------------------------------------------------------
    # Internal – thread entrypoint
    # ------------------------------------------------------------------

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            log.exception("Discord bot loop exited with error")
        finally:
            self._loop.close()
            self._is_ready = False

    # ------------------------------------------------------------------
    # Internal – async main
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        settings = self._get_settings()
        if not getattr(settings, "discord_bot_enabled", False):
            log.info("Discord bot is disabled in settings")
            return
        token = getattr(settings, "discord_bot_token", "").strip()
        if not token:
            log.warning("Discord bot enabled but no token configured")
            return
        guild_id = getattr(settings, "discord_guild_id", 0)
        if not guild_id:
            log.warning("Discord bot: guild_id is not set")
            return

        intents = discord.Intents.default()
        self._client = discord.Client(intents=intents)
        self._tree = app_commands.CommandTree(self._client)
        self._log_queue = asyncio.Queue()

        self._register_slash_commands()

        @self._client.event
        async def on_ready() -> None:
            log.info("Discord bot ready as %s", self._client.user)
            guild = discord.Object(id=guild_id)
            self._tree.copy_global_to(guild=guild)
            synced = await self._tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %d", len(synced), guild_id)
            await self._update_activity()
            self._is_ready = True

        @self._client.event
        async def on_disconnect() -> None:
            log.warning("Discord bot disconnected")
            self._is_ready = False

        asyncio.ensure_future(self._log_flush_loop())
        asyncio.ensure_future(self._player_tracking_loop())

        try:
            await self._client.start(token)
        except discord.LoginFailure:
            log.error("Discord bot login failed: invalid token")
        except Exception:
            log.exception("Discord bot encountered an error")

    async def _shutdown(self) -> None:
        self._is_ready = False
        await self._flush_queue_to_discord()
        if self._client and not self._client.is_closed():
            try:
                await self._client.close()
            except Exception:
                log.warning("Error closing Discord client", exc_info=True)

    # ------------------------------------------------------------------
    # Internal – activity
    # ------------------------------------------------------------------

    async def _update_activity(self) -> None:
        if not self._client or self._client.is_closed():
            return
        try:
            state = self._get_server_state()
            status = state.get("status", "stopped")
            uptime = state.get("uptime_seconds") or 0
            if status == "running":
                h, rem = divmod(int(uptime), 3600)
                m = rem // 60
                name = f"Online {h}h {m:02d}m" if h else f"Online {m}m"
                activity = discord.Activity(type=discord.ActivityType.playing, name=name)
            else:
                activity = discord.Activity(type=discord.ActivityType.watching, name="Server offline")
            await self._client.change_presence(activity=activity)
            self._last_presence_update = datetime.now(timezone.utc)
        except Exception:
            log.debug("Failed to update bot activity", exc_info=True)

    # ------------------------------------------------------------------
    # Internal – log flush loop
    # ------------------------------------------------------------------

    async def _log_flush_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self._LOG_FLUSH_INTERVAL)

                now = datetime.now(timezone.utc)
                if (now - self._last_presence_update).total_seconds() >= self._PRESENCE_UPDATE_INTERVAL:
                    await self._update_activity()

                if self._log_queue and not self._log_queue.empty():
                    await self._flush_queue_to_discord()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("Log flush loop error", exc_info=True)

    async def _flush_queue_to_discord(self) -> None:
        if not self._log_queue or not self._is_ready:
            return
        settings = self._get_settings()
        channel_id = getattr(settings, "discord_log_channel_id", 0)
        if not channel_id or not self._client:
            return
        channel = self._client.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        while not self._log_queue.empty():
            try:
                evt: _BotEvent = self._log_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._send_event_embed(channel, evt, settings)

    async def _send_event_embed(
        self,
        channel: discord.TextChannel,
        evt: _BotEvent,
        settings: Any,
    ) -> None:
        # Respect per-type notification flags
        if evt.type == EventType.SERVER_CRASHED and not getattr(settings, "discord_notify_crash", True):
            return
        if evt.type in (EventType.BACKUP_SUCCESS, EventType.BACKUP_FAILED) and not getattr(settings, "discord_notify_backup", True):
            return
        if evt.type in (EventType.SERVER_RESTART_SCHEDULED, EventType.SERVER_RESTART_MANUAL) and not getattr(settings, "discord_notify_restart", True):
            return
        if evt.type in (EventType.PLAYER_JOINED, EventType.PLAYER_LEFT) and not getattr(settings, "discord_notify_player_join_leave", False):
            return

        color, emoji, title = _EVENT_META.get(evt.type, (discord.Color.greyple(), "ℹ️", str(evt.type)))

        player_name = evt.kwargs.get("name", "")
        if evt.type in (EventType.PLAYER_JOINED, EventType.PLAYER_LEFT) and player_name:
            title = f"{title}: {player_name}"

        embed = discord.Embed(
            title=f"{emoji} {title}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Palworld Server Manager v{constants.APP_VERSION}")

        reason = evt.kwargs.get("reason", "")
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)

        filename = evt.kwargs.get("filename", "")
        if filename:
            embed.add_field(name="File", value=filename, inline=True)

        await self._safe_send_embed(channel, embed)

    async def _safe_send_embed(self, channel: discord.TextChannel, embed: discord.Embed) -> None:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            if e.status == 429:
                log.warning("Discord rate limit hit, retrying in 2s")
                await asyncio.sleep(2)
                try:
                    await channel.send(embed=embed)
                except Exception:
                    log.warning("Retry after rate limit also failed", exc_info=True)
            elif e.code in (50001, 50013):
                log.warning("Discord bot missing permissions for channel %d", channel.id)
            else:
                log.warning("Failed to send embed: %s", e)
        except Exception:
            log.warning("Unexpected error sending embed", exc_info=True)

    # ------------------------------------------------------------------
    # Internal – player tracking
    # ------------------------------------------------------------------

    async def _player_tracking_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self._PLAYER_POLL_INTERVAL)

                settings = self._get_settings()
                if not getattr(settings, "discord_notify_player_join_leave", False):
                    continue
                if not self._is_ready:
                    continue

                state = self._get_server_state()
                if state.get("status") != "running":
                    self._known_players.clear()
                    continue

                try:
                    cfg = self._get_rest_api_config()
                    players, err = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: rest_api.get_players(
                            cfg.get("host", "127.0.0.1"),
                            cfg.get("port", constants.DEFAULT_REST_API_PORT),
                            cfg.get("admin_password", ""),
                        ),
                    )
                    if err:
                        log.debug("Player tracking poll failed: %s", err)
                        continue

                    current = {rest_api.player_display_name(p) for p in players if rest_api.player_display_name(p)}
                    joined = current - self._known_players
                    left = self._known_players - current

                    for name in joined:
                        self.queue_event(EventType.PLAYER_JOINED, name=name)
                    for name in left:
                        self.queue_event(EventType.PLAYER_LEFT, name=name)

                    self._known_players = current
                except Exception:
                    log.debug("Player tracking error", exc_info=True)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("Player tracking loop error", exc_info=True)

    # ------------------------------------------------------------------
    # Internal – slash commands registration
    # ------------------------------------------------------------------

    def _register_slash_commands(self) -> None:
        tree = self._tree
        service = self

        server_group = app_commands.Group(name="server", description="Palworld server management")

        @server_group.command(name="status", description="Show current server status")
        async def cmd_status(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            try:
                state = service._get_server_state()
                status = state.get("status", "stopped")
                uptime = state.get("uptime_seconds") or 0
                pid = state.get("pid")

                h, rem = divmod(int(uptime), 3600)
                m = rem // 60
                uptime_str = f"{h}h {m:02d}m" if uptime else "N/A"

                status_emoji = {
                    "running": "🟢", "starting": "🟡",
                    "stopping": "🟠", "stopped": "🔴",
                }.get(status, "⚫")

                embed = discord.Embed(
                    title="📊 Server Status",
                    color=discord.Color.green() if status == "running" else discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="State", value=f"{status_emoji} {status.capitalize()}", inline=True)
                embed.add_field(name="Uptime", value=uptime_str, inline=True)
                embed.add_field(name="PID", value=str(pid) if pid else "N/A", inline=True)

                # Try to get metrics from REST API
                try:
                    cfg = service._get_rest_api_config()
                    metrics, _ = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: rest_api.get_server_metrics(
                            cfg.get("host", "127.0.0.1"),
                            cfg.get("port", constants.DEFAULT_REST_API_PORT),
                            cfg.get("admin_password", ""),
                        ),
                    )
                    if metrics:
                        ram_mb = metrics.get("memoryUsed", 0) // (1024 * 1024)
                        embed.add_field(name="RAM", value=f"{ram_mb} MB", inline=True)
                except Exception:
                    pass

                embed.set_footer(text=f"Palworld Server Manager v{constants.APP_VERSION}")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                log.error("Error in /server status: %s", e)
                await interaction.followup.send("❌ Failed to retrieve server status.", ephemeral=True)

        @server_group.command(name="start", description="Start the server (admin only)")
        async def cmd_start(interaction: discord.Interaction) -> None:
            if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
                await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
                return
            await interaction.response.defer()
            state = service._get_server_state()
            if state.get("status") in ("running", "starting"):
                await interaction.followup.send("⚠️ Server is already running or starting.")
                return
            cb = state.get("start_callback")
            if cb:
                try:
                    await asyncio.get_event_loop().run_in_executor(None, cb)
                    await interaction.followup.send("🟢 Server start requested.")
                except Exception as e:
                    await interaction.followup.send(f"❌ Failed to start server: {e}")
            else:
                await interaction.followup.send("⚠️ Start callback not configured.", ephemeral=True)

        @server_group.command(name="stop", description="Stop the server (admin only)")
        async def cmd_stop(interaction: discord.Interaction) -> None:
            if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
                await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
                return
            await interaction.response.defer()
            state = service._get_server_state()
            if state.get("status") in ("stopped", "stopping"):
                await interaction.followup.send("⚠️ Server is already stopped or stopping.")
                return
            cb = state.get("stop_callback")
            if cb:
                try:
                    await asyncio.get_event_loop().run_in_executor(None, cb)
                    await interaction.followup.send("🔴 Server stop requested.")
                except Exception as e:
                    await interaction.followup.send(f"❌ Failed to stop server: {e}")
            else:
                await interaction.followup.send("⚠️ Stop callback not configured.", ephemeral=True)

        @server_group.command(name="restart", description="Restart the server (admin only)")
        async def cmd_restart(interaction: discord.Interaction) -> None:
            if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
                await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
                return
            await interaction.response.defer()
            state = service._get_server_state()
            stop_cb = state.get("stop_callback")
            start_cb = state.get("start_callback")
            if not stop_cb or not start_cb:
                await interaction.followup.send("⚠️ Restart callbacks not configured.", ephemeral=True)
                return
            try:
                await interaction.followup.send("🔄 Stopping server...")
                await asyncio.get_event_loop().run_in_executor(None, stop_cb)
                await asyncio.sleep(5)
                await interaction.edit_original_response(content="🔄 Starting server...")
                await asyncio.get_event_loop().run_in_executor(None, start_cb)
                await interaction.edit_original_response(content="🟢 Server restarted successfully.")
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Restart failed: {e}")

        @server_group.command(name="backup", description="Create a manual backup (admin only)")
        async def cmd_backup(interaction: discord.Interaction) -> None:
            if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
                await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
                return
            await interaction.response.send_message("⏳ Creating backup...")
            state = service._get_server_state()
            cb = state.get("backup_callback")
            if cb:
                try:
                    result = await asyncio.get_event_loop().run_in_executor(None, cb)
                    fname = result if isinstance(result, str) else "done"
                    await interaction.edit_original_response(content=f"✅ Backup created: `{fname}`")
                except Exception as e:
                    await interaction.edit_original_response(content=f"❌ Backup failed: {e}")
            else:
                await interaction.edit_original_response(content="⚠️ Backup callback not configured.")

        tree.add_command(server_group)
