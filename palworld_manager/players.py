from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path


RE_JOIN = re.compile(r"Join succeeded:\s*(.+)", re.I)
RE_LEAVE = re.compile(r"Leave:\s*(.+)", re.I)
RE_ACCOUNT = re.compile(r"AccountName '([^']+)'.*AccountId (\w+)")
RE_FAREWELL = re.compile(r"Name '([^']+)'.*State 'SaidFarewell'")
RE_DISCONNECT = re.compile(r"disconnectaccount.*accountid (\w+)", re.I)


def process_log_line_for_players(
    line: str,
    *,
    online: set[str],
    account_to_player: dict[str, str],
    on_join_history: Callable[[str], None] | None = None,
    on_leave_history: Callable[[str, str], None] | None = None,
) -> None:
    low = line.lower()
    if "lognet: join succeeded:" in low:
        m = RE_JOIN.search(line)
        if m:
            name = m.group(1).strip()
            if name:
                online.add(name)
                if on_join_history:
                    on_join_history(name)
    elif m := RE_ACCOUNT.search(line):
        account_to_player[m.group(2)] = m.group(1)
    elif "lognet: leave:" in low:
        m = RE_LEAVE.search(line)
        if m:
            name = m.group(1).strip()
            if name and name in online:
                online.discard(name)
                if on_leave_history:
                    on_leave_history(name, "")
    elif m := RE_FAREWELL.search(line):
        name = m.group(1)
        if name in online:
            online.discard(name)
            if on_leave_history:
                on_leave_history(name, "")
    elif m := RE_DISCONNECT.search(line):
        acct = m.group(1)
        pname = account_to_player.get(acct)
        if pname and pname in online:
            online.discard(pname)
            if on_leave_history:
                on_leave_history(pname, " (disconnect)")


def replay_full_log(log_path: Path) -> tuple[set[str], dict[str, str]]:
    online: set[str] = set()
    account_to_player: dict[str, str] = {}
    if not log_path.is_file():
        return online, account_to_player
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            for line in f:
                process_log_line_for_players(
                    line.rstrip("\n\r"),
                    online=online,
                    account_to_player=account_to_player,
                )
    except OSError:
        pass
    return online, account_to_player
