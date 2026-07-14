from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import constants
from .config_schema import CONFIG_FIELDS
from .config_schema import default_option_settings as schema_default_option_settings
from .paths import ServerPaths

_SECTION_HEADER = "[/Script/Pal.PalGameWorldSettings]"
_OPTION_PREFIX = "OptionSettings=("

_SAMPLE_COMMENT_LINES = frozenset({
    "; This configuration file is a sample of the default server settings.",
    "; Changes to this file will NOT be reflected on the server.",
    "; To change the server settings, modify Pal/Saved/Config/WindowsServer/PalWorldSettings.ini.",
})

# Enum keys stay bare (e.g. DeathPenalty=Item). All other strings must be quoted
# (e.g. AdminPassword="RestPassword") or Palworld logs "Missing opening '"'"'."
_ENUM_KEYS = frozenset(f.key for f in CONFIG_FIELDS if f.kind == "enum")


def _parse_option_value(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if text.startswith("(") and text.endswith(")"):
        return text
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "none":
        return None
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _split_option_pairs(inner: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    i = 0
    n = len(inner)
    while i < n:
        while i < n and inner[i] in " \t,":
            i += 1
        if i >= n:
            break
        key_start = i
        while i < n and inner[i] not in "=,":
            i += 1
        key = inner[key_start:i].strip()
        if i >= n or inner[i] != "=":
            break
        i += 1
        if i < n and inner[i] == '"':
            i += 1
            val_start = i
            while i < n:
                if inner[i] == '"' and (i == 0 or inner[i - 1] != "\\"):
                    val = inner[val_start:i]
                    i += 1
                    pairs.append((key, val))
                    break
                i += 1
            continue
        if i < n and inner[i] == "(":
            depth = 0
            val_start = i
            while i < n:
                if inner[i] == "(":
                    depth += 1
                elif inner[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        pairs.append((key, inner[val_start:i]))
                        break
                i += 1
            continue
        val_start = i
        while i < n and inner[i] != ",":
            i += 1
        pairs.append((key, inner[val_start:i].strip()))
    return pairs


def _extract_option_settings_block(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_OPTION_PREFIX):
            inner = stripped[len(_OPTION_PREFIX) :]
            if inner.endswith(")"):
                inner = inner[:-1]
            return inner
    return None


def parse_option_settings(text: str) -> dict[str, Any]:
    inner = _extract_option_settings_block(text)
    if inner is None:
        return {}
    return {k: _parse_option_value(v) for k, v in _split_option_pairs(inner)}


def format_option_value(value: Any, key: str | None = None) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            return f"{value:.6f}".rstrip("0").rstrip(".")
        return str(value)
    if isinstance(value, str) and value.startswith("(") and value.endswith(")"):
        return value
    s = str(value)
    # Bare enum tokens (DeathPenalty=Item, LogFormatType=Text, …).
    if key in _ENUM_KEYS:
        return s
    # String properties (passwords, Region, ServerName, URLs, …) must be quoted.
    escaped = s.replace('"', '\\"')
    return f'"{escaped}"'


def format_option_settings(options: dict[str, Any]) -> str:
    parts = [f"{k}={format_option_value(v, k)}" for k, v in options.items()]
    return f"{_OPTION_PREFIX}{', '.join(parts)})"


def _strip_comment_and_blank_lines(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(";") or stripped in _SAMPLE_COMMENT_LINES:
            continue
        lines.append(stripped)
    return "\n".join(lines)


def format_config_ini_text(text: str) -> str:
    """
    Normalize PalWorldSettings.ini to a clean two-line file.

    Palworld requires OptionSettings on a single line; we add spaces after commas
    for readability and strip sample/disclaimer comment lines from the template.
    """
    cleaned = _strip_comment_and_blank_lines(text)
    inner = _extract_option_settings_block(cleaned)
    if inner is None:
        return f"{cleaned}\n" if cleaned else ""
    pairs = _split_option_pairs(inner)
    formatted_pairs = [
        f"{key}={format_option_value(_parse_option_value(raw), key)}" for key, raw in pairs
    ]
    option_line = f"{_OPTION_PREFIX}{', '.join(formatted_pairs)})"
    return f"{_SECTION_HEADER}\n{option_line}\n"


def _config_text_source(paths: ServerPaths) -> str:
    if paths.config_path.is_file():
        try:
            return paths.config_path.read_text(encoding="utf-8")
        except OSError:
            return ""
    if paths.default_config_template.is_file():
        try:
            return paths.default_config_template.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def read_default_template_settings(paths: ServerPaths) -> dict[str, Any]:
    if not paths.default_config_template.is_file():
        return {}
    try:
        return parse_option_settings(paths.default_config_template.read_text(encoding="utf-8"))
    except OSError:
        return {}


def read_option_settings(paths: ServerPaths) -> dict[str, Any]:
    if not paths.config_path.is_file():
        return {}
    try:
        return parse_option_settings(paths.config_path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def read_effective_option_settings(paths: ServerPaths) -> dict[str, Any]:
    """Settings from PalWorldSettings.ini, or DefaultPalWorldSettings.ini if not created yet."""
    opts = read_option_settings(paths)
    if opts:
        return opts
    template = read_default_template_settings(paths)
    if template:
        return template
    return default_option_settings()


def _option_value_pattern(key: str) -> str:
    return (
        rf"{re.escape(key)}="
        rf'(?:\([^)]*\)|"[^"]*"|[^\s,)(]+)'
    )


def extract_option_value(text: str, key: str) -> Any:
    m = re.search(_option_value_pattern(key), text)
    if not m:
        return None
    raw = m.group(0)[len(key) + 1 :]
    return _parse_option_value(raw)


def patch_option_settings_text(text: str, updates: dict[str, Any]) -> str:
    if not updates:
        return text
    if _OPTION_PREFIX not in text:
        text = f"{_SECTION_HEADER}\n{format_option_settings(updates)}\n"
        return text

    for key, value in updates.items():
        fragment = f"{key}={format_option_value(value, key)}"
        pattern = _option_value_pattern(key)
        if re.search(pattern, text):
            text = re.sub(pattern, fragment, text, count=1)
        else:
            text = re.sub(
                r"(OptionSettings=\()",
                rf"\1{fragment},",
                text,
                count=1,
            )
    return text


def is_server_config_ready(paths: ServerPaths) -> bool:
    """True when PalWorldSettings.ini exists and contains OptionSettings."""
    if not paths.config_path.is_file():
        return False
    try:
        text = paths.config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.strip():
        return False
    inner = _extract_option_settings_block(text)
    return bool(inner and inner.strip())


def init_config_from_template(paths: ServerPaths) -> bool:
    """Create PalWorldSettings.ini from DefaultPalWorldSettings.ini if missing."""
    if paths.config_path.is_file():
        return False
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    if not paths.default_config_template.is_file():
        return False
    try:
        template_text = paths.default_config_template.read_text(encoding="utf-8")
        paths.config_path.write_text(format_config_ini_text(template_text), encoding="utf-8")
        return True
    except OSError:
        return False


def ensure_config_file(paths: ServerPaths) -> None:
    if paths.config_path.is_file():
        return
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    if init_config_from_template(paths):
        return
    write_option_settings(paths, default_option_settings())


def default_option_settings() -> dict[str, Any]:
    return dict(schema_default_option_settings())


def write_option_settings(paths: ServerPaths, options: dict[str, Any]) -> None:
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    content = f"{_SECTION_HEADER}\n{format_option_settings(options)}\n"
    paths.config_path.write_text(content, encoding="utf-8")


def _write_config_text(paths: ServerPaths, text: str) -> None:
    paths.config_path.write_text(format_config_ini_text(text), encoding="utf-8")


def merge_option_settings(paths: ServerPaths, updates: dict[str, Any]) -> dict[str, Any]:
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    if not paths.config_path.is_file():
        if not init_config_from_template(paths):
            write_option_settings(paths, {**default_option_settings(), **updates})
            return read_option_settings(paths)

    try:
        text = paths.config_path.read_text(encoding="utf-8")
    except OSError:
        text = ""

    if not text.strip():
        if paths.default_config_template.is_file():
            try:
                text = paths.default_config_template.read_text(encoding="utf-8")
            except OSError:
                text = ""
        if not text.strip():
            write_option_settings(paths, {**default_option_settings(), **updates})
            return read_option_settings(paths)

    patched = patch_option_settings_text(text, updates)
    _write_config_text(paths, patched)
    merged = parse_option_settings(patched)
    for key, value in updates.items():
        merged[key] = value
    return merged


def read_server_name(paths: ServerPaths) -> str | None:
    text = _config_text_source(paths)
    if not text:
        return None
    name = extract_option_value(text, "ServerName")
    return str(name) if name else None


def read_rest_api_config(paths: ServerPaths) -> tuple[bool, int, str]:
    text = _config_text_source(paths)
    if not text:
        return False, constants.DEFAULT_REST_API_PORT, ""
    enabled = extract_option_value(text, "RESTAPIEnabled")
    port_raw = extract_option_value(text, "RESTAPIPort")
    admin_pw = extract_option_value(text, "AdminPassword")
    try:
        port = int(port_raw) if port_raw is not None else constants.DEFAULT_REST_API_PORT
    except (TypeError, ValueError):
        port = constants.DEFAULT_REST_API_PORT
    return bool(enabled), port, str(admin_pw or "")


def read_game_port(paths: ServerPaths) -> int:
    text = _config_text_source(paths)
    if not text:
        return constants.DEFAULT_GAME_PORT
    port_raw = extract_option_value(text, "PublicPort")
    try:
        return int(port_raw) if port_raw is not None else constants.DEFAULT_GAME_PORT
    except (TypeError, ValueError):
        return constants.DEFAULT_GAME_PORT
