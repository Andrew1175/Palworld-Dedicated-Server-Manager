"""Official parameter descriptions from the Palworld server guide."""

from __future__ import annotations

DOCS_CONFIGURATION_URL = "https://docs.palworldgame.com/settings-and-operation/configuration"

_RESERVED = "Reserved for future updates and deprecated parameter."

# Descriptions from the 1.0.0 configuration guide unless noted.
PARAMETER_TOOLTIPS: dict[str, str] = {
    # Server management
    "ServerName": "Server name",
    "ServerPlayerMaxNum": "Maximum number of players who can join the server.",
    "ServerDescription": "Server description",
    "ServerPassword": "Password required to log in to the server.",
    "AdminPassword": "Password used to obtain administrative privileges on the server.",
    "PublicPort": (
        "(Community server) Explicitly specify the external public port. "
        "(Does not change the server's listening port.)"
    ),
    "PublicIP": "(Community server) Explicitly specify the external public IP.",
    "RESTAPIEnabled": "Enable the REST API.",
    "RESTAPIPort": "Listening port for the REST API.",
    "RCONEnabled": "Enable RCON.",
    "RCONPort": "Port number used for RCON.",
    "bAllowClientMod": "Allow players with mods enabled to join the server.",
    "bShowPlayerList": "Enable the player list on the ESC menu.",
    "bIsUseBackupSaveData": (
        "Enable world backups. Enabling this increases disk load. "
        "Creates a backup directory with intervals of 5 saves per 30 sec, 6 per 10 min, 12 per hour, and 7 per day."
    ),
    "bIsShowJoinLeftMessage": "On dedicated servers, show in-game messages when players join/leave.",
    "ChatPostLimitPerMinute": "Maximum number of chat messages allowed per minute.",
    "CrossplayPlatforms": "Allowed platform to connect the server. Default: (Steam,Xbox,PS5,Mac)",
    "LogFormatType": "Log format: Text or Json",
    # Features / world
    "bIsMultiplay": _RESERVED,
    "Difficulty": _RESERVED,
    "RandomizerType": (
        "Pal spawn randomization mode: None = no randomization; "
        "Region = randomize per region; All = fully randomized."
    ),
    "RandomizerSeed": "Seed value used when Pal spawn randomization mode is enabled.",
    "bIsRandomizerPalLevelRandom": (
        "If true, wild Pal levels are fully random. "
        "If false, levels are randomized within each area's intended range."
    ),
    "DayTimeSpeedRate": "Daytime progression speed.",
    "NightTimeSpeedRate": "Nighttime progression speed.",
    "ExpRate": "EXP gain multiplier.",
    "ItemWeightRate": "Item weight multiplier.",
    "WorkSpeedRate": _RESERVED,
    "bHardcore": "Enable Hardcore. You will not be able to respawn on death.",
    "bIsStartLocationSelectByMap": "Whether to allow players to choose their starting location.",
    "bExistPlayerAfterLogout": (
        "Whether players enter a sleeping state at their current location when logging out."
    ),
    "bEnableDefenseOtherGuildPlayer": (
        "Set to True with bIsPvP and bEnablePlayerToPlayerDamage to enable PvP."
    ),
    "bInvisibleOtherGuildBaseCampAreaFX": "Show base area boundaries.",
    "bBuildAreaLimit": "Prevent building near structures such as fast-travel points.",
    "bEnableFastTravel": "Enable fast travel.",
    "bEnableFastTravelOnlyBaseCamp": "Restrict fast travel to between bases only.",
    "bEnableInvaderEnemy": "Enable Invader",
    "AutoSaveSpan": _RESERVED,
    "SupplyDropSpan": "Meteorite / supply drop interval (minutes).",
    "EnablePredatorBossPal": _RESERVED,
    "MaxBuildingLimitNum": "Per-player building count cap (0 = unlimited).",
    "ServerReplicatePawnCullDistance": (
        "Pal sync distance from players (cm). Minimum 5000 – maximum 15000."
    ),
    "EquipmentDurabilityDamageRate": "Equipment durability loss multiplier.",
    "ItemContainerForceMarkDirtyInterval": (
        "How often to force re-sync while a container UI is open (seconds)."
    ),
    "PlayerDataPalStorageUpdateCheckTickInterval": _RESERVED,
    "ItemCorruptionMultiplier": "Item corruption speed multiplier.",
    "MonsterFarmActionSpeedRate": "Item production speed multiplier from grazing.",
    "DenyTechnologyList": (
        "Disable specific technologies. Specify Technology IDs. "
        'Example: DenyTechnologyList=(""PALBOX"", ""RepairBench""))'
    ),
    "bAllowGlobalPalboxExport": "Allow saving to the Global Palbox.",
    "bAllowGlobalPalboxImport": "Allow loading from the Global Palbox.",
    "bEnableBuildingPlayerUIdDisplay": "Whether to display the creator's player ID on structures.",
    "BuildingNameDisplayCacheTTLSeconds": _RESERVED,
    # Pal balances
    "PalCaptureRate": "Capture rate multiplier.",
    "PalSpawnNumRate": "Pal spawn rate. (Impacts performance.)",
    "PalDamageRateAttack": "Damage dealt by Pals multiplier.",
    "PalDamageRateDefense": "Damage taken by Pals multiplier.",
    "PalStomachDecreaceRate": "Pal hunger depletion rate multiplier.",
    "PalStaminaDecreaceRate": "Pal stamina depletion rate multiplier.",
    "PalAutoHPRegeneRate": "Pal natural HP regen multiplier.",
    "PalAutoHpRegeneRateInSleep": "Pal HP regen while sleeping (in Palbox) multiplier.",
    "PalEggDefaultHatchingTime": (
        "Time to hatch a Huge Egg (hours). Note: Other eggs also require time to incubate."
    ),
    "bActiveUNKO": _RESERVED,
    # Player balances
    "PlayerDamageRateAttack": "Damage dealt by players multiplier.",
    "PlayerDamageRateDefense": "Damage taken by players multiplier.",
    "PlayerStomachDecreaceRate": "Player hunger depletion rate multiplier.",
    "PlayerStaminaDecreaceRate": "Player stamina depletion rate multiplier.",
    "PlayerAutoHPRegeneRate": "Player natural HP regen multiplier.",
    "PlayerAutoHpRegeneRateInSleep": "Player HP regen while sleeping multiplier.",
    "bAllowEnhanceStat_Health": "Allow allocating stat points to HP.",
    "bAllowEnhanceStat_Attack": "Allow allocating stat points to Attack.",
    "bAllowEnhanceStat_Stamina": "Allow allocating stat points to Stamina.",
    "bAllowEnhanceStat_Weight": "Allow allocating stat points to Carry Weight.",
    "bAllowEnhanceStat_WorkSpeed": "Allow allocating stat points to Work Speed.",
    # Death
    "bPalLost": "Permanently lose Pals on death.",
    "bCharacterRecreateInHardcore": (
        "Whether you may recreate your character upon death in Hardcore mode."
    ),
    "DeathPenalty": (
        "Death Penalty. None: No drops, Item: Drop all items except equipment, "
        "ItemAndEquipment: Drop all items, All: Drop all items and all Pals on team"
    ),
    "bCanPickupOtherGuildDeathPenaltyDrop": (
        "Whether death penalty drops from other guilds can be picked up."
    ),
    "BlockRespawnTime": "Cooldown before you can respawn after death (seconds).",
    "RespawnPenaltyDurationThreshold": (
        "Survival-time threshold (seconds) for applying the respawn cooldown multiplier "
        "set by RespawnPenaltyTimeScale on a subsequent death."
    ),
    "RespawnPenaltyTimeScale": "Multiplier applied to the respawn cooldown.",
    # PvP
    "bIsPvP": "Enable PvP",
    "bEnablePlayerToPlayerDamage": (
        "Set to True with bIsPvP and bEnableDefenseOtherGuildPlayer to enable PvP. "
        "When PvP is active, players can harm each other."
    ),
    "bEnableFriendlyFire": _RESERVED,
    "bAdditionalDropItemWhenPlayerKillingInPvPMode": (
        "Whether to drop a special item when a player is killed while PvP is enabled."
    ),
    "AdditionalDropItemWhenPlayerKillingInPvPMode": (
        "When bAdditionalDropItemWhenPlayerKillingInPvPMode is enabled, the ID of the item to drop."
    ),
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode": (
        "When bAdditionalDropItemWhenPlayerKillingInPvPMode is enabled, the quantity of the item to drop."
    ),
    "bDisplayPvPItemNumOnWorldMap_BaseCamp": (
        "Whether to show, on the map, the number of PvP-exclusive items in each base."
    ),
    "bDisplayPvPItemNumOnWorldMap_Player": (
        "Whether to show player locations and the number of PvP-exclusive items on the map."
    ),
    # VOIP
    "bEnableVoiceChat": "Enable in-game voice chat.",
    "VoiceChatMaxVolumeDistance": "Distance at which voice chat volume does not attenuate.",
    "VoiceChatZeroVolumeDistance": "Distance at which voice chat volume becomes zero.",
    # Guild
    "bAutoResetGuildNoOnlinePlayers": (
        "If no guild members log in, automatically delete structures and base Pals."
    ),
    "AutoResetGuildTimeNoOnlinePlayers": (
        "Offline duration before bAutoResetGuildNoOnlinePlayers triggers. "
        "Ignored if bAutoResetGuildNoOnlinePlayers is False."
    ),
    "GuildPlayerMaxNum": "Max player number of guild.",
    "BaseCampMaxNumInGuild": (
        "Maximum number of bases per guild. Default: 4 (max 10). "
        "Increasing this value raises processing load."
    ),
    "GuildRejoinCooldownMinutes": "Guild rejoin cooldown (minutes).",
    "AutoTransferMasterCheckIntervalSeconds": _RESERVED,
    "AutoTransferMasterThresholdDays": _RESERVED,
    "MaxGuildsPerFrame": _RESERVED,
    # Drops / performance
    "CollectionDropRate": "Gatherable items multiplier",
    "CollectionObjectHpRate": "Gatherable objects health multiplier",
    "CollectionObjectRespawnSpeedRate": "Gatherable objects respawn interval",
    "EnemyDropItemRate": "Dropped item quantity multiplier.",
    "DropItemMaxNum": _RESERVED,
    "PhysicsActiveDropItemMaxNum": (
        "Maximum number of dropped items that can use physics behavior."
    ),
    "DropItemMaxNum_UNKO": _RESERVED,
    "DropItemAliveMaxHours": _RESERVED,
    # General / structures
    "BuildObjectHpRate": _RESERVED,
    "BuildObjectDamageRate": "Damage multiplier to buildings.",
    "BuildObjectDeteriorationDamageRate": "Building decay speed multiplier.",
    "BaseCampMaxNum": "Total number of bases across the server.",
    "BaseCampWorkerMaxNum": (
        "Maximum number of Pals per base (max 50). Increasing this value raises processing load."
    ),
    "CoopPlayerMaxNum": _RESERVED,
    "bEnableAimAssistPad": "When True, aim assist is enabled for players using controllers.",
    "bEnableAimAssistKeyboard": _RESERVED,
    "bUseAuth": _RESERVED,
    "bEnableNonLoginPenalty": _RESERVED,
    "Region": _RESERVED,
    "BanListURL": _RESERVED,
}


def tooltip_for(key: str) -> str:
    return PARAMETER_TOOLTIPS.get(key, "")
