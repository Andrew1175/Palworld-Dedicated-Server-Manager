# Developed by: https://github.com/Andrew1175

APP_VERSION = "1.2.1"

# GitHub REST API for the latest published release (used by Check for Updates).
GITHUB_LATEST_RELEASE_API_URL = (
    "https://api.github.com/repos/Andrew1175/Windrose-Server-Manager-Enhanced/releases/latest"
)

DONATE_URL = "https://buymeacoffee.com/TheWisestGuy"
GITHUB_REPO_URL = "https://github.com/Andrew1175/Windrose-Server-Manager-Enhanced"
GITHUB_ISSUES_NEW_URL = "https://github.com/Andrew1175/Windrose-Server-Manager-Enhanced/issues/new/choose"

WINDROSE_STEAM_APP_ID = "4129620"

PATCH_NOTES: dict[str, list[str]] = {
    "1.2.1": [
        "Fixed an issue where the Server Manager would incorrectly register a manual restart or stop as a crash.",
        "Fixed the 'Auto-restart if crashed' checkbox to only take affect if the server actually crashed and was not manually stopped.",
        "Added a hover tooltip to the 'Auto-restart if crashed' checkbox to explain how it works.",
    ],
    "1.2.0": [
        "Added a new Insights tab to the Server Manager to provide information about server activity.",
        "Added a new Player Activity section to track the activity of players who have connected to the game server and their total session times",
        "Added a new Most Active Times section to the Server Manager to track the most active times of the day for the game server.",
    ],
    "1.1.3": [
        "Fixed an issue with the Server Manager updater not working correctly under certain conditions.",
        "Included a fallback to the Server Manager update process. Any future issues where the updater fails will launch the previous version of the Server Manager.",
    ],
    "1.1.2": [
        "Created a new help tab to provide additional information about the Server Manager.",
        "Added a new crash counter to the dashboard to track the number of times the game server has crashed.",
    ],
    "1.1.1": [
        "Removed a safety check to prevent the Server Manager from being updated while the game server is running.\n(You can now safely update the Server Manager while the game server is running without interrupting the game server.)",
        "Fixed an issue with the Uptime statistic not showing the minutes correctly.",
    ],
    "1.1.0": [
        "Added a new Discord Webhook feature to send notifications to a Discord channel when the server starts, stops, restarts, schedules a restart, or crashes.",
        "Updated RAM display to show total percentage used instead of just the size.",
    ],
    "1.0.3": [
        "Updated the Auto-Backup feature to allow for custom backup intervals.",
    ],
    "1.0.2": [
        "Fixed the 'Check for Updates' feature not working correctly.",
    ],
    "1.0.1": [
        "Added a safety check to prevent configuration changes while the game server is running.",
        "Added a safety check to prevent the Server Manager from being updated while the game server is running.",
    ],
    "1.0.0": [
        "Initial Release",
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

FLOAT_PARAM_KEYS = {
    "mob_health": '{"TagName": "WDS.Parameter.MobHealthMultiplier"}',
    "mob_damage": '{"TagName": "WDS.Parameter.MobDamageMultiplier"}',
    "ship_health": '{"TagName": "WDS.Parameter.ShipsHealthMultiplier"}',
    "ship_damage": '{"TagName": "WDS.Parameter.ShipsDamageMultiplier"}',
    "boarding": '{"TagName": "WDS.Parameter.BoardingDifficultyMultiplier"}',
    "coop_stats": '{"TagName": "WDS.Parameter.Coop.StatsCorrectionModifier"}',
    "coop_ship": '{"TagName": "WDS.Parameter.Coop.ShipStatsCorrectionModifier"}',
}

BOOL_PARAM_KEYS = {
    "coop_quests": '{"TagName": "WDS.Parameter.Coop.SharedQuests"}',
    "easy_explore": '{"TagName": "WDS.Parameter.EasyExplore"}',
}

TAG_COMBAT_KEY = '{"TagName": "WDS.Parameter.CombatDifficulty"}'
