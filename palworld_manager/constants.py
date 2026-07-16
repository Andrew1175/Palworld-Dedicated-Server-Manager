# Developed by: https://github.com/Andrew1175

APP_VERSION = "2.0.1"

# GitHub REST API for the latest published release (used by Check for Updates).
GITHUB_LATEST_RELEASE_API_URL = (
    "https://api.github.com/repos/Andrew1175/Palworld-Dedicated-Server-Manager/releases/latest"
)

DONATE_URL = "https://buymeacoffee.com/TheWisestGuy"
GITHUB_REPO_URL = "https://github.com/Andrew1175/Palworld-Dedicated-Server-Manager"
GITHUB_ISSUES_NEW_URL = "https://github.com/Andrew1175/Palworld-Dedicated-Server-Manager/issues/new/choose"

PALWORLD_STEAM_APP_ID = "2394010"

DEFAULT_LAUNCH_ARGS = "-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS -publiclobby"

DEFAULT_REST_API_PORT = 8212
DEFAULT_GAME_PORT = 8211

PATCH_NOTES: dict[str, list[str]] = {
    "2.0.1": [
        "Fixed Insights tab to update every 60 seconds. Previously it was only updating after the player left the server.",
        "Updated text in Help tab."
    ],
    "2.0.0": [
        "HUGE rework to the entire UI and codebase",
        "Switched from RCON to REST API",
        "Added initial install wizard"
    ]
}

# Theme (hex without # for tk)
COLORS = {
    "bg": "#0F1923",
    "bg_header": "#0A1520",
    "bg_panel": "#111E2A",
    "bg_input": "#1A2736",
    "border": "#1E3348",
    "border_input": "#2A3E55",
    "text": "#C0CDD8",
    "text_dim": "#8DA4B5",
    "text_muted": "#607080",
    "accent": "#D4A843",
    "green": "#70C48A",
    "red": "#CC3333",
    "blue_btn": "#1A4A7A",
    "gray_btn": "#2A3E55",
    "green_btn": "#1A6B3A",
    "red_btn": "#6B1A1A",
    "navy_btn": "#1A3A7A",
    "save_btn": "#2A3A4A",
    "folder_btn": "#1A4A2A",
    "warn_btn": "#7A3A1A",
    "history_clear": "#5A2020",
    "tab_bg": "#162330",
    "tab_selected": "#1E3348",
    "status_stopped": "#555555",
}
