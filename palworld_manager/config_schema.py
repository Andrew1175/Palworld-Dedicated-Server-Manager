from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from . import constants
from .config_tooltips import tooltip_for

SECTION_ORDER: tuple[str, ...] = (
    "Server Settings",
    "World Settings",
    "Pal Settings",
    "Stat Settings",
    "Death Settings",
    "PvP Settings",
    "VOIP Settings",
    "Guild Settings",
    "Drop Rates",
    "General Settings",
)


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    kind: str
    section: str
    default: Any
    choices: tuple[str, ...] | None = None
    min_val: float | None = None
    max_val: float | None = None
    resolution: float = 0.1
    tooltip: str = ""


def _rate(key: str, label: str, section: str, default: float = 1.0, tooltip: str = "") -> ConfigField:
    return ConfigField(
        key=key,
        label=label,
        kind="float_rate",
        section=section,
        default=default,
        min_val=0.1,
        max_val=10.0,
        resolution=0.1,
        tooltip=tooltip or tooltip_for(key),
    )


def _bool(key: str, label: str, section: str, default: bool = False, tooltip: str = "") -> ConfigField:
    return ConfigField(
        key=key,
        label=label,
        kind="bool",
        section=section,
        default=default,
        tooltip=tooltip or tooltip_for(key),
    )


def _int(
    key: str,
    label: str,
    section: str,
    default: int,
    *,
    min_val: int | None = None,
    max_val: int | None = None,
    tooltip: str = "",
) -> ConfigField:
    return ConfigField(
        key=key,
        label=label,
        kind="int",
        section=section,
        default=default,
        min_val=min_val,
        max_val=max_val,
        tooltip=tooltip or tooltip_for(key),
    )


def _port(key: str, label: str, section: str, default: int, tooltip: str = "") -> ConfigField:
    return ConfigField(
        key=key,
        label=label,
        kind="port",
        section=section,
        default=default,
        min_val=1,
        max_val=65535,
        tooltip=tooltip or tooltip_for(key),
    )


_RAW_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    # Server Settings
    ConfigField(
        "ServerName",
        "Server Name",
        "string",
        "Server Settings",
        "Default Palworld Server",
        tooltip=tooltip_for("ServerName"),
    ),
    ConfigField(
        "ServerPlayerMaxNum",
        "Max Players",
        "player_max",
        "Server Settings",
        32,
        min_val=1,
        max_val=32,
        tooltip=tooltip_for("ServerPlayerMaxNum"),
    ),
    _bool("bIsMultiplay", "Multiplay", "Server Settings", False),
    ConfigField("ServerDescription", "Server Description", "string", "Server Settings", ""),
    ConfigField("ServerPassword", "Password", "password", "Server Settings", ""),
    ConfigField("AdminPassword", "Admin Password", "password", "Server Settings", ""),
    ConfigField("PublicPort", "Connection Port", "port", "Server Settings", constants.DEFAULT_GAME_PORT),
    ConfigField("PublicIP", "Public IP", "string", "Server Settings", ""),
    _bool("RESTAPIEnabled", "REST API Enabled", "Server Settings", True),
    _port("RESTAPIPort", "REST API Port", "Server Settings", constants.DEFAULT_REST_API_PORT),
    _bool("RCONEnabled", "RCON Enabled", "Server Settings", False),
    _port("RCONPort", "RCON Port", "Server Settings", 25575),
    _bool("bAllowClientMod", "Allow Client Mods", "Server Settings", True),
    _bool("bUseAuth", "Use Auth", "Server Settings", True),
    _bool("bShowPlayerList", "Show Player List", "Server Settings", False),
    _bool("bIsUseBackupSaveData", "Use Backup Save Data", "Server Settings", True),
    _bool("bIsShowJoinLeftMessage", "Show Join/Left Messages", "Server Settings", True),
    _bool("bEnableNonLoginPenalty", "Enable Non-Login Penalty", "Server Settings", True),
    ConfigField("Region", "Region", "string", "Server Settings", ""),
    ConfigField("BanListURL", "Ban List URL", "string", "Server Settings", "https://b.palworldgame.com/api/banlist.txt"),
    ConfigField("CrossplayPlatforms", "Crossplay Platforms", "tuple_string", "Server Settings", "(Steam,Xbox,PS5,Mac)"),
    ConfigField("LogFormatType", "Log Format", "enum", "Server Settings", "Text", choices=("Text", "Json")),
    _int("ChatPostLimitPerMinute", "Chat Post Limit / Min", "Server Settings", 30, min_val=0, max_val=999),
    # World Settings
    ConfigField("Difficulty", "Difficulty", "enum", "World Settings", "None", choices=("None", "Normal", "Hard")),
    ConfigField("RandomizerType", "Randomizer Type", "enum", "World Settings", "None", choices=("None", "Region", "World")),
    ConfigField("RandomizerSeed", "Randomizer Seed", "string", "World Settings", ""),
    _bool("bIsRandomizerPalLevelRandom", "Randomizer Pal Level Random", "World Settings", False),
    _rate("DayTimeSpeedRate", "Day Time Speed", "World Settings"),
    _rate("NightTimeSpeedRate", "Night Time Speed", "World Settings"),
    _rate("ExpRate", "EXP Rate", "World Settings"),
    _rate("ItemWeightRate", "Item Weight Rate", "World Settings"),
    _rate("WorkSpeedRate", "Work Speed Rate", "World Settings"),
    _bool("bHardcore", "Hardcore", "World Settings", False),
    _bool("bIsStartLocationSelectByMap", "Start Location Select by Map", "World Settings", False),
    _bool("bExistPlayerAfterLogout", "Exist Player After Logout", "World Settings", False),
    _bool("bEnableDefenseOtherGuildPlayer", "Defense Other Guild Player", "World Settings", False),
    _bool("bInvisibleOtherGuildBaseCampAreaFX", "Invisible Other Guild Base FX", "World Settings", False),
    _bool("bBuildAreaLimit", "Build Area Limit", "World Settings", False),
    _bool("bEnableFastTravel", "Enable Fast Travel", "World Settings", True),
    _bool("bEnableFastTravelOnlyBaseCamp", "Fast Travel Only Base Camp", "World Settings", False),
    _bool("bEnableInvaderEnemy", "Enable Invader Enemy", "World Settings", True),
    _int("AutoSaveSpan", "Auto Save Interval (min)", "World Settings", 30, min_val=1, max_val=1440),
    _int("SupplyDropSpan", "Supply Drop Interval (min)", "World Settings", 180, min_val=1, max_val=10000),
    _bool("EnablePredatorBossPal", "Enable Predator Boss Pal", "World Settings", True),
    _int("MaxBuildingLimitNum", "Max Building Limit", "World Settings", 0, min_val=0, max_val=100000),
    _int("ServerReplicatePawnCullDistance", "Replicate Pawn Cull Distance", "World Settings", 15000, min_val=1000, max_val=100000),
    _rate("EquipmentDurabilityDamageRate", "Equipment Durability Damage", "World Settings"),
    _int("ItemContainerForceMarkDirtyInterval", "Item Container Dirty Interval", "World Settings", 1, min_val=0, max_val=3600),
    _int("PlayerDataPalStorageUpdateCheckTickInterval", "Pal Storage Update Tick Interval", "World Settings", 1, min_val=0, max_val=3600),
    _rate("ItemCorruptionMultiplier", "Item Corruption Multiplier", "World Settings", 1.0),
    _rate("MonsterFarmActionSpeedRate", "Monster Farm Action Speed", "World Settings"),
    ConfigField("DenyTechnologyList", "Deny Technology List", "string", "World Settings", ""),
    _bool("bAllowGlobalPalboxExport", "Allow Global Palbox Export", "World Settings", True),
    _bool("bAllowGlobalPalboxImport", "Allow Global Palbox Import", "World Settings", False),
    _bool("bEnableBuildingPlayerUIdDisplay", "Building Player UID Display", "World Settings", False),
    _int("BuildingNameDisplayCacheTTLSeconds", "Building Name Cache TTL (sec)", "World Settings", 60, min_val=0, max_val=86400),
    # Pal Settings
    _rate("PalCaptureRate", "Pal Capture Rate", "Pal Settings"),
    _rate("PalSpawnNumRate", "Pal Spawn Num Rate", "Pal Settings"),
    _rate("PalDamageRateAttack", "Pal Attack Damage", "Pal Settings"),
    _rate("PalDamageRateDefense", "Pal Defense Damage", "Pal Settings"),
    _rate("PalStomachDecreaceRate", "Pal Hunger Rate", "Pal Settings"),
    _rate("PalStaminaDecreaceRate", "Pal Stamina Rate", "Pal Settings"),
    _rate("PalAutoHPRegeneRate", "Pal HP Regen Rate", "Pal Settings"),
    _rate("PalAutoHpRegeneRateInSleep", "Pal HP Regen In Sleep", "Pal Settings"),
    _rate("PalEggDefaultHatchingTime", "Pal Egg Hatching Time", "Pal Settings"),
    _bool("bActiveUNKO", "Active UNKO", "Pal Settings", False),
    # Stat Settings
    _rate("PlayerDamageRateAttack", "Player Attack Damage", "Stat Settings"),
    _rate("PlayerDamageRateDefense", "Player Defense Damage", "Stat Settings"),
    _rate("PlayerStomachDecreaceRate", "Player Hunger Rate", "Stat Settings"),
    _rate("PlayerStaminaDecreaceRate", "Player Stamina Rate", "Stat Settings"),
    _rate("PlayerAutoHPRegeneRate", "Player HP Regen Rate", "Stat Settings"),
    _rate("PlayerAutoHpRegeneRateInSleep", "Player HP Regen In Sleep", "Stat Settings"),
    _bool("bAllowEnhanceStat_Health", "Allow Enhance Health", "Stat Settings", True),
    _bool("bAllowEnhanceStat_Attack", "Allow Enhance Attack", "Stat Settings", True),
    _bool("bAllowEnhanceStat_Stamina", "Allow Enhance Stamina", "Stat Settings", True),
    _bool("bAllowEnhanceStat_Weight", "Allow Enhance Weight", "Stat Settings", True),
    _bool("bAllowEnhanceStat_WorkSpeed", "Allow Enhance Work Speed", "Stat Settings", True),
    # Death Settings
    _bool("bPalLost", "Pal Lost on Death", "Death Settings", False),
    _bool("bCharacterRecreateInHardcore", "Recreate Character in Hardcore", "Death Settings", False),
    ConfigField(
        "DeathPenalty",
        "Death Penalty",
        "enum",
        "Death Settings",
        "Item",
        choices=("None", "Item", "ItemAndEquipment", "All"),
    ),
    _bool("bCanPickupOtherGuildDeathPenaltyDrop", "Pickup Other Guild Death Drops", "Death Settings", False),
    _int("BlockRespawnTime", "Block Respawn Time (sec)", "Death Settings", 5, min_val=0, max_val=3600),
    _int("RespawnPenaltyDurationThreshold", "Respawn Penalty Threshold", "Death Settings", 0, min_val=0, max_val=86400),
    _rate("RespawnPenaltyTimeScale", "Respawn Penalty Time Scale", "Death Settings", 2.0),
    # PvP Settings
    _bool("bIsPvP", "PvP", "PvP Settings", False),
    _bool("bEnablePlayerToPlayerDamage", "Player vs Player Damage", "PvP Settings", False),
    _bool("bEnableFriendlyFire", "Friendly Fire", "PvP Settings", False),
    _bool("bAdditionalDropItemWhenPlayerKillingInPvPMode", "Extra Drop on PvP Kill", "PvP Settings", False),
    ConfigField(
        "AdditionalDropItemWhenPlayerKillingInPvPMode",
        "PvP Kill Drop Type",
        "enum",
        "PvP Settings",
        "PlayerDropItem",
        choices=("None", "Item", "ItemAndEquipment", "All", "PlayerDropItem"),
    ),
    _int("AdditionalDropItemNumWhenPlayerKillingInPvPMode", "PvP Kill Drop Count", "PvP Settings", 1, min_val=0, max_val=100),
    _bool("bDisplayPvPItemNumOnWorldMap_BaseCamp", "Show PvP Items on Map (Base)", "PvP Settings", False),
    _bool("bDisplayPvPItemNumOnWorldMap_Player", "Show PvP Items on Map (Player)", "PvP Settings", False),
    # VOIP Settings
    _bool("bEnableVoiceChat", "Enable Voice Chat", "VOIP Settings", False),
    _int("VoiceChatMaxVolumeDistance", "Voice Max Volume Distance", "VOIP Settings", 3000, min_val=0, max_val=100000),
    _int("VoiceChatZeroVolumeDistance", "Voice Zero Volume Distance", "VOIP Settings", 15000, min_val=0, max_val=100000),
    # Guild Settings
    _bool("bAutoResetGuildNoOnlinePlayers", "Auto Reset Inactive Guilds", "Guild Settings", False),
    _int("AutoResetGuildTimeNoOnlinePlayers", "Guild Reset After (hours)", "Guild Settings", 72, min_val=1, max_val=8760),
    _int("GuildPlayerMaxNum", "Guild Player Max", "Guild Settings", 20, min_val=1, max_val=100),
    _int("BaseCampMaxNumInGuild", "Base Camps Per Guild", "Guild Settings", 4, min_val=1, max_val=128),
    _int("GuildRejoinCooldownMinutes", "Guild Rejoin Cooldown (min)", "Guild Settings", 0, min_val=0, max_val=10080),
    _int("AutoTransferMasterCheckIntervalSeconds", "Auto Transfer Master Check (sec)", "Guild Settings", 3600, min_val=60, max_val=86400),
    _int("AutoTransferMasterThresholdDays", "Auto Transfer Master After (days)", "Guild Settings", 14, min_val=1, max_val=365),
    _int("MaxGuildsPerFrame", "Max Guilds Per Frame", "Guild Settings", 10, min_val=1, max_val=1000),
    # Drop Rates
    _rate("CollectionDropRate", "Collection Drop Rate", "Drop Rates"),
    _rate("CollectionObjectHpRate", "Collection Object HP", "Drop Rates"),
    _rate("CollectionObjectRespawnSpeedRate", "Collection Respawn Speed", "Drop Rates"),
    _rate("EnemyDropItemRate", "Enemy Drop Item Rate", "Drop Rates"),
    _int("DropItemMaxNum", "Max Drop Items", "Drop Rates", 3000, min_val=0, max_val=100000),
    _int("PhysicsActiveDropItemMaxNum", "Physics Active Drop Max", "Drop Rates", -1, min_val=-1, max_val=100000),
    _int("DropItemMaxNum_UNKO", "Max Drop Items (UNKO)", "Drop Rates", 100, min_val=0, max_val=100000),
    _int("DropItemAliveMaxHours", "Drop Item Alive Max (hours)", "Drop Rates", 1, min_val=0, max_val=168),
    # General Settings
    _rate("BuildObjectHpRate", "Build Object HP", "General Settings"),
    _rate("BuildObjectDamageRate", "Build Object Damage", "General Settings"),
    _rate("BuildObjectDeteriorationDamageRate", "Build Object Decay", "General Settings"),
    _int("BaseCampMaxNum", "Max Base Camps", "General Settings", 128, min_val=1, max_val=1000),
    _int("BaseCampWorkerMaxNum", "Base Camp Workers Max", "General Settings", 15, min_val=1, max_val=100),
    _int("CoopPlayerMaxNum", "Coop Player Max", "General Settings", 4, min_val=1, max_val=32),
    _bool("bEnableAimAssistPad", "Aim Assist (Gamepad)", "General Settings", True),
    _bool("bEnableAimAssistKeyboard", "Aim Assist (Keyboard)", "General Settings", False),
)


def _apply_official_tooltips(fields: tuple[ConfigField, ...]) -> tuple[ConfigField, ...]:
    result: list[ConfigField] = []
    for field in fields:
        official = tooltip_for(field.key)
        if official:
            result.append(replace(field, tooltip=official))
        else:
            result.append(field)
    return tuple(result)


CONFIG_FIELDS = _apply_official_tooltips(_RAW_CONFIG_FIELDS)

CONFIG_FIELD_BY_KEY: dict[str, ConfigField] = {f.key: f for f in CONFIG_FIELDS}


def all_config_keys() -> tuple[str, ...]:
    return tuple(f.key for f in CONFIG_FIELDS)


def default_option_settings() -> dict[str, Any]:
    return {f.key: f.default for f in CONFIG_FIELDS}
