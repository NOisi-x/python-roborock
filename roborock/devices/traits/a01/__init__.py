"""Create traits for A01 devices.

This module provides the API implementations for A01 protocol devices, which include
Dyad (Wet/Dry Vacuums) and Zeo (Washing Machines).

Using A01 APIs
--------------
A01 devices expose a single API object that handles all device interactions. This API is
available on the device instance (typically via `device.a01_properties`).

The API provides two main methods:
1.  **query_values(protocols)**: Fetches current state for specific data points.
    You must pass a list of protocol enums (e.g. `RoborockDyadDataProtocol` or
    `RoborockZeoProtocol`) to request specific data.
2.  **set_value(protocol, value)**: Sends a command to the device to change a setting
    or perform an action.

``DyadApi`` is stateless.  ``ZeoApi`` maintains an internal DPS cache
(populated by MQTT push, ``set_value``, and ``query_values``) to bridge
the ``set_value`` → ``start()`` call window without extra MQTT round trips.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from typing import Any

from roborock.data import DyadProductInfo, DyadSndState, HomeDataProduct, RoborockCategory
from roborock.data.dyad.dyad_code_mappings import (
    DyadBrushSpeed,
    DyadCleanMode,
    DyadError,
    DyadSelfCleanLevel,
    DyadSelfCleanMode,
    DyadSuction,
    DyadWarmLevel,
    DyadWaterLevel,
    RoborockDyadStateCode,
)
from roborock.data.zeo.zeo_code_mappings import (
    ZeoDetergentExpansionType,
    ZeoDetergentType,
    ZeoDirtDetectionStatus,
    ZeoDryAndCare,
    ZeoDryerStartError,
    ZeoDryingMethod,
    ZeoDryingMode,
    ZeoError,
    ZeoMode,
    ZeoProgram,
    ZeoRinse,
    ZeoSoak,
    ZeoSoftenerExpansionType,
    ZeoSoftenerType,
    ZeoSpin,
    ZeoState,
    ZeoSteamVolume,
    ZeoTemperature,
)
from roborock.device_features import ZeoFeatures
from roborock.devices.rpc.a01_channel import send_decoded_command
from roborock.devices.traits import Trait
from roborock.devices.traits.a01.device_features import ZeoFeatureTrait
from roborock.devices.transport.mqtt_channel import MqttChannel
from roborock.exceptions import RoborockException
from roborock.mqtt.session import MqttQos
from roborock.protocols.a01_protocol import decode_rpc_response
from roborock.roborock_message import (
    RoborockDyadDataProtocol,
    RoborockMessage,
    RoborockMessageProtocol,
    RoborockZeoProtocol,
)

_LOGGER = logging.getLogger(__name__)

__init__ = [
    "DyadApi",
    "ZeoApi",
    "ZeoFeatureTrait",
    "ZeoCustomMode",
    "ZeoDryerCustomMode",
    "ZeoStartParams",
]


def _try_json(val: Any) -> Any:
    """Return ``val`` parsed as JSON when it is a JSON string, else ``val``.

    Several meta DPs (robotInfo / WashHistory / VoiceRecord …) arrive as
    JSON-encoded strings from the device.  This lets converters return
    decoded dicts/lists transparently while keeping raw values for
    non‑JSON payloads.
    """
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            pass
    return val


def parse_bool(val: Any) -> bool:
    """Parse a Zeo/Dyad boolean DP value robustly.

    The official app sends and receives booleans as the strings
    ``"True"`` / ``"False"`` (its ``DPBoolean`` enum), but firmware revisions
    and sibling protocols also use ``0`` / ``1`` or JSON ``true`` / ``false``.
    Accept every representation so decoding never mis-classifies a falsy
    string such as ``"False"`` (``bool("False")`` is ``True`` — a bug we avoid).
    """
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1")
    return bool(val)


def _decode_expansion_type(val: Any, default: int) -> int:
    """Decode an expansion-type DP, returning *default* when the device
    reports ``None`` (type not yet configured).  Keeps the protocol
    entry lambda clean.
    """
    return int(val) if val is not None else default


def to_dp_bool(val: Any) -> str:
    """Normalise a boolean-like value to the wire-format string ``"True"`` or ``"False"``.

    The official app serialises booleans as the strings ``"True"`` /
    ``"False"`` (its ``DPBoolean`` enum) on SET commands — not as ``1`` / ``0``
    or JSON ``true`` / ``false``.  Used only in the ``set_value`` encoder
    path because callers (HA switch entities, external code) may pass
    Python ``True`` / ``False`` which ``json.dumps`` would serialise as
    JSON ``true`` / ``false``.
    """
    return "True" if parse_bool(val) else "False"


DYAD_PROTOCOL_ENTRIES: dict[RoborockDyadDataProtocol, Callable] = {
    RoborockDyadDataProtocol.STATUS: lambda val: RoborockDyadStateCode(val).name,
    RoborockDyadDataProtocol.SELF_CLEAN_MODE: lambda val: DyadSelfCleanMode(val).name,
    RoborockDyadDataProtocol.SELF_CLEAN_LEVEL: lambda val: DyadSelfCleanLevel(val).name,
    RoborockDyadDataProtocol.WARM_LEVEL: lambda val: DyadWarmLevel(val).name,
    RoborockDyadDataProtocol.CLEAN_MODE: lambda val: DyadCleanMode(val).name,
    RoborockDyadDataProtocol.SUCTION: lambda val: DyadSuction(val).name,
    RoborockDyadDataProtocol.WATER_LEVEL: lambda val: DyadWaterLevel(val).name,
    RoborockDyadDataProtocol.BRUSH_SPEED: lambda val: DyadBrushSpeed(val).name,
    RoborockDyadDataProtocol.POWER: lambda val: int(val),
    RoborockDyadDataProtocol.AUTO_DRY: parse_bool,
    RoborockDyadDataProtocol.MESH_LEFT: lambda val: int(360000 - val * 60),
    RoborockDyadDataProtocol.BRUSH_LEFT: lambda val: int(360000 - val * 60),
    RoborockDyadDataProtocol.ERROR: lambda val: DyadError(val).name,
    RoborockDyadDataProtocol.VOLUME_SET: lambda val: int(val),
    RoborockDyadDataProtocol.STAND_LOCK_AUTO_RUN: parse_bool,
    RoborockDyadDataProtocol.AUTO_DRY_MODE: parse_bool,
    RoborockDyadDataProtocol.SILENT_DRY_DURATION: lambda val: int(val),  # in minutes
    RoborockDyadDataProtocol.SILENT_MODE: parse_bool,
    RoborockDyadDataProtocol.SILENT_MODE_START_TIME: lambda val: time(
        hour=int(val / 60), minute=val % 60
    ),  # in minutes since 00:00
    RoborockDyadDataProtocol.SILENT_MODE_END_TIME: lambda val: time(
        hour=int(val / 60), minute=val % 60
    ),  # in minutes since 00:00
    RoborockDyadDataProtocol.RECENT_RUN_TIME: lambda val: [
        int(v) for v in val.split(",")
    ],  # minutes of cleaning in past few days.
    RoborockDyadDataProtocol.TOTAL_RUN_TIME: lambda val: int(val),
    RoborockDyadDataProtocol.SND_STATE: lambda val: DyadSndState.from_dict(val),
    RoborockDyadDataProtocol.PRODUCT_INFO: lambda val: DyadProductInfo.from_dict(val),
}

ZEO_PROTOCOL_ENTRIES: dict[RoborockZeoProtocol, Callable] = {
    # read-only
    RoborockZeoProtocol.STATE: lambda val: ZeoState(val).name,
    RoborockZeoProtocol.WASHING_LEFT: lambda val: int(val),
    RoborockZeoProtocol.ERROR: lambda val: ZeoError(val).name,
    RoborockZeoProtocol.TIMES_AFTER_CLEAN: lambda val: int(val),
    RoborockZeoProtocol.DETERGENT_EMPTY: parse_bool,
    RoborockZeoProtocol.SOFTENER_EMPTY: parse_bool,
    RoborockZeoProtocol.DIRT_DETECTION_STATUS: lambda val: ZeoDirtDetectionStatus(val).name,
    RoborockZeoProtocol.TOTAL_TIME: lambda val: int(val),
    RoborockZeoProtocol.FEATURE_BITS: lambda val: int(val),
    RoborockZeoProtocol.SMART_HOSTING_WAITED_TIME: lambda val: int(val),
    RoborockZeoProtocol.IS_NEED_FLUFF_CLEAN: parse_bool,
    RoborockZeoProtocol.PANEL_PROGRAM_PARAMS_SET_RESULT: lambda val: int(val),
    RoborockZeoProtocol.DEVICE_BOUND: parse_bool,
    RoborockZeoProtocol.CLOTH_PUT_IN: parse_bool,
    RoborockZeoProtocol.CLOTH_READY_TO_DRY_COUNT_DOWN: lambda val: int(val),
    RoborockZeoProtocol.START_DRYER_ERROR: lambda val: ZeoDryerStartError(val).name,
    RoborockZeoProtocol.DOORLOCK_STATE: parse_bool,
    RoborockZeoProtocol.APP_AUTHORIZATION: parse_bool,
    RoborockZeoProtocol.SMART_HOSTING_TIME: lambda val: int(val),
    RoborockZeoProtocol.CUSTOM_PROGRAM_CLEANING_TIME: lambda val: int(val),
    RoborockZeoProtocol.PANEL_TIMING_PROGRAM_PARAMS: lambda val: int(val),
    RoborockZeoProtocol.STEAM_CARE_TIME: lambda val: int(val),
    # meta — read-only (JSON on wire, auto-decoded by converter)
    RoborockZeoProtocol.PRODUCT_INFO: lambda val: _try_json(val),  # robotInfo
    RoborockZeoProtocol.WASHING_LOG: lambda val: _try_json(val),  # washHistory
    RoborockZeoProtocol.VOICE_RECORD_INFO: lambda val: _try_json(val),
    RoborockZeoProtocol.VOICE_RECORD: lambda val: _try_json(val),
    # read-write
    RoborockZeoProtocol.COUNTDOWN: lambda val: int(val),
    RoborockZeoProtocol.MODE: lambda val: ZeoMode(val).name,
    RoborockZeoProtocol.PROGRAM: lambda val: ZeoProgram(val).name,
    RoborockZeoProtocol.TEMP: lambda val: ZeoTemperature(val).name,
    RoborockZeoProtocol.RINSE_TIMES: lambda val: ZeoRinse(val).name,
    RoborockZeoProtocol.SPIN_LEVEL: lambda val: ZeoSpin(val).name,
    RoborockZeoProtocol.DRYING_MODE: lambda val: ZeoDryingMode(val).name,
    RoborockZeoProtocol.DETERGENT_TYPE: lambda val: ZeoDetergentType(val).name,
    RoborockZeoProtocol.SOFTENER_TYPE: lambda val: ZeoSoftenerType(val).name,
    RoborockZeoProtocol.SOUND_SET: parse_bool,
    RoborockZeoProtocol.DIRT_DETECTION_SWITCH: parse_bool,
    RoborockZeoProtocol.SOAK: lambda val: ZeoSoak(val).name,
    RoborockZeoProtocol.SILENT_MODE_ON: parse_bool,
    RoborockZeoProtocol.SILENT_MODE_START_TIME: lambda val: int(val),
    RoborockZeoProtocol.SILENT_MODE_END_TIME: lambda val: int(val),
    RoborockZeoProtocol.DRY_CARE_MODE: lambda val: ZeoDryAndCare(val).name,
    RoborockZeoProtocol.WASH_DRY_LINKED: parse_bool,
    RoborockZeoProtocol.DRYING_METHOD: lambda val: ZeoDryingMethod(val).name,
    RoborockZeoProtocol.STEAM_VOLUME: lambda val: ZeoSteamVolume(val).name,
    RoborockZeoProtocol.ION_DEODORIZATION: parse_bool,
    RoborockZeoProtocol.UV_LIGHT: parse_bool,
    RoborockZeoProtocol.SMART_HOSTING: parse_bool,
    RoborockZeoProtocol.SOFTENER_EXPANSION_TYPE: lambda val: _decode_expansion_type(
        val, ZeoSoftenerExpansionType.softener
    ),
    RoborockZeoProtocol.DETERGENT_EXPANSION_TYPE: lambda val: _decode_expansion_type(
        val, ZeoDetergentExpansionType.concentrated_detergent
    ),
    RoborockZeoProtocol.SMILE_LIGHT_STATUS: parse_bool,
    RoborockZeoProtocol.POWER_LIGHT: parse_bool,
    RoborockZeoProtocol.PANEL_PROGRAM_PARAMS_SET: lambda val: int(val),
    RoborockZeoProtocol.WIFI_LINKAGE_RESET: lambda val: int(val),
    RoborockZeoProtocol.SAVE_ADAPTED_CLOUD_PROGRAM: lambda val: int(val),
    RoborockZeoProtocol.CHILD_LOCK: parse_bool,
    RoborockZeoProtocol.DETERGENT_SET: parse_bool,
    RoborockZeoProtocol.SOFTENER_SET: parse_bool,
    RoborockZeoProtocol.FLUFF_CLEANED: parse_bool,  # bundle: setFluffCleaned(1)
    # read-write (int-valued, not boolean)
    RoborockZeoProtocol.CUSTOM_PARAM_SAVE: lambda val: int(val),
    RoborockZeoProtocol.CUSTOM_PARAM_GET: lambda val: int(val),
    RoborockZeoProtocol.DEFAULT_SETTING: lambda val: int(val),
    RoborockZeoProtocol.LIGHT_SETTING: parse_bool,
    RoborockZeoProtocol.DETERGENT_VOLUME: lambda val: int(val),
    RoborockZeoProtocol.SOFTENER_VOLUME: lambda val: int(val),
    # meta — read-write (JSON-encoded on wire by official app; raw Python dicts)
    RoborockZeoProtocol.SET_SOUND_PACKAGE: lambda val: val,
    RoborockZeoProtocol.VOICE_VOLUME: lambda val: val,
    RoborockZeoProtocol.VOICE_SWITCH: parse_bool,
    RoborockZeoProtocol.VOICE_RECORD_DELETE: lambda val: int(val),
}


# Protocols whose converter is ``parse_bool``.  The encoder path in
# ``set_value`` uses ``to_dp_bool`` to normalise callers' Python ``True`` /
# ``False`` to the wire-format integer ``1`` / ``0`` (DPBoolean).  Derived
# automatically from ``ZEO_PROTOCOL_ENTRIES``.
_ZEO_BOOLEAN_PROTOCOLS: frozenset[RoborockZeoProtocol] = frozenset(
    p for p, conv in ZEO_PROTOCOL_ENTRIES.items() if conv is parse_bool
)
_DYAD_BOOLEAN_PROTOCOLS: frozenset[RoborockDyadDataProtocol] = frozenset(
    p for p, conv in DYAD_PROTOCOL_ENTRIES.items() if conv is parse_bool
)


def convert_dyad_value(protocol_value: RoborockDyadDataProtocol, value: Any) -> Any:
    """Convert a dyad protocol value to its corresponding type."""
    if (converter := DYAD_PROTOCOL_ENTRIES.get(protocol_value)) is not None:
        try:
            return converter(value)
        except (ValueError, TypeError):
            return None
    return None


def convert_zeo_value(protocol_value: RoborockZeoProtocol, value: Any) -> Any:
    """Convert a zeo protocol value to its corresponding type."""
    if (converter := ZEO_PROTOCOL_ENTRIES.get(protocol_value)) is not None:
        try:
            return converter(value)
        except (ValueError, TypeError):
            return None
    return None


class DyadApi(Trait):
    """API for interacting with Dyad devices."""

    def __init__(self, channel: MqttChannel) -> None:
        """Initialize the Dyad API."""
        self._channel = channel

    async def query_values(self, protocols: list[RoborockDyadDataProtocol]) -> dict[RoborockDyadDataProtocol, Any]:
        """Query the device for the values of the given Dyad protocols."""
        response = await send_decoded_command(
            self._channel,
            {RoborockDyadDataProtocol.ID_QUERY: protocols},
            value_encoder=json.dumps,
        )
        return {protocol: convert_dyad_value(protocol, response.get(protocol)) for protocol in protocols}

    async def set_value(self, protocol: RoborockDyadDataProtocol, value: Any) -> dict[RoborockDyadDataProtocol, Any]:
        """Set a value for a specific protocol on the device.

        Booleans are serialised as "True"/"False" (DPBoolean) on the wire,
        matching the official app; all other values pass through.
        """
        # Booleans are serialised as "True"/"False" (DPBoolean) on the wire.
        encoder = to_dp_bool if protocol in _DYAD_BOOLEAN_PROTOCOLS else None
        params = {protocol: value}
        return await send_decoded_command(self._channel, params, value_encoder=encoder)


@dataclass
class ZeoStartParams:
    """Parameters that must be bundled with a START command.

    All Zeo devices require ``mode`` and ``program`` to be sent together
    with the start signal. The remaining fields are optional and only
    included when the device reports a non-None value.
    """

    mode: int
    """Wash mode (e.g. wash, wash-and-dry, dry, treatment)."""

    program: int
    """Wash program (e.g. standard, quick, wool)."""

    temp: int | None = None
    """Water temperature (always ``None`` for dryers)."""

    rinse_times: int | None = None
    """Number of rinse cycles (always ``None`` for dryers)."""

    spin_level: int | None = None
    """Spin speed in RPM (always ``None`` for dryers)."""

    drying_mode: int | None = None
    """Drying mode (e.g. quick, iron, store)."""


# ── DP 222 (LoadCloudProgram) bitfield decoder ──────────────────────────
# The official app packs all custom-program parameters into a single
# 32-bit integer at DP 222.  This mirrors WasherDpsCache.customMode in
# module 725 of the React Native plugin bundle.


@dataclass
class ZeoCustomMode:
    """Decoded custom programme parameters from DP 222 (LoadCloudProgram).

    Null / absent fields are represented as ``0`` which matches the
    official app's behaviour (the right-shifted-and-masked value is
    always non-negative).
    """

    program: int
    """Wash program (bits 0-7)."""

    mode: int
    """Wash mode (bits 8-9)."""

    temperature: int
    """Temperature (bits 10-12)."""

    rinse: int
    """Rinse cycle (bits 13-15)."""

    spin: int
    """Spin speed (bits 16-18)."""

    dry: int
    """Drying mode (bits 19-21)."""

    soak: int
    """Soak (bits 22-24)."""

    dry_care_mode: int
    """Dry-care mode (bits 25-27)."""

    steam_volume: int
    """Steam volume (bits 28-30)."""

    total_time_min: int = 0
    """Total programme time in minutes (from DP 239)."""

    @classmethod
    def from_raw(cls, raw: int, total_time_min: int | None = None) -> "ZeoCustomMode":
        """Decode a raw 32-bit custom-programme value."""
        return cls(
            program=(raw & 0xFF),
            mode=(raw >> 8) & 0x3,
            temperature=(raw >> 10) & 0x7,
            rinse=(raw >> 13) & 0x7,
            spin=(raw >> 16) & 0x7,
            dry=(raw >> 19) & 0x7,
            soak=(raw >> 22) & 0x7,
            dry_care_mode=(raw >> 25) & 0x7,
            steam_volume=(raw >> 28) & 0x7,
            total_time_min=total_time_min or 0,
        )


@dataclass
class ZeoDryerCustomMode:
    """Decoded custom programme from DP 222 for a standalone dryer.

    Dryers pack a different (shorter) bitfield than washers — only 5
    fields after program/mode.  Mirrors ``WasherDpsCache.dryerCustomMode``
    in module 725 of the plugin bundle.
    """

    program: int
    """Drying program (bits 0-7)."""

    mode: int
    """Drying mode (bits 8-10)."""

    dry: int
    """Drying level (bits 11-13)."""

    dry_method: int
    """Drying method (bits 14-16)."""

    steam_volume: int
    """Steam volume (bits 17-19)."""

    total_time_min: int = 0
    """Total programme time in minutes (from DP 239)."""

    @classmethod
    def from_raw(cls, raw: int, total_time_min: int | None = None) -> "ZeoDryerCustomMode":
        """Decode a raw 32-bit dryer custom-programme value."""
        return cls(
            program=(raw & 0xFF),
            # Dryer mode spans bits 8-10 (0x700, 3 bits) because the
            # temperature field is absent from the dryer bitfield and
            # bit 10 is re-allocated to mode.  Washer uses 2 bits
            # (0x300, bits 8-9) to make room for temperature at 10-12.
            mode=(raw >> 8) & 0x7,
            dry=(raw >> 11) & 0x7,
            dry_method=(raw >> 14) & 0x7,
            steam_volume=(raw >> 17) & 0x7,
            total_time_min=total_time_min or 0,
        )


class ZeoApi(Trait):
    """API for interacting with Zeo devices.

    ── Bundle method mapping ──

    ======================== ================================================
    Bundle (module 727)      Python ``ZeoApi``
    ======================== ================================================
    ``startWith(...)``       :meth:`start` — bundles START+Mode+Program+params
    ``start()`` / ``continue()``  :meth:`resume` — single DP ``{200: 1}``
    ``presetWith(n)``        :meth:`start_with_preset` — startWith + Preset
    ``stop`` / ``pause`` /   ``set_value(PAUSE/SHUTDOWN/START, …)``
      ``shutdown``
    ``saveCloudProgramWith`` :meth:`save_cloud_program`
    ``savePanelProgramWith`` :meth:`save_panel_program`
    ``loadCloudProgram``     :meth:`load_cloud_program`
    ``setCleanserConfig``    :meth:`set_cleanser_config` — 4-DP bundle
    ``forceLoad`` +          :meth:`force_load` + :meth:`load_feature_dps`
      ``loadFeatureDps``
    ``setSilentMode(…)``     :meth:`set_silent_mode`
    ``checkFCCState``        :meth:`check_fcc_state`
    ``loadGeneralInfo``      :meth:`load_general_info`
    ``uploadLog``            :meth:`upload_log`
    ``syncPrivacyToDevice``  :meth:`sync_privacy_to_device`
    ``updateSoundPackageInfo`` :meth:`update_sound_package_info`
    28+ single-DP setters    :meth:`set_value`
    ======================== ================================================

    ── Two categories of DPs ──

    **startWith params** — MODE, PROGRAM, TEMP, RINSE, SPIN, DRYING_MODE,
    DRYING_METHOD, STEAM_VOLUME, SOAK, DRY_CARE_MODE, TOTAL_TIME, and the
    feature‑gated DPs (WASH_DRY_LINKED, ION_DEODORIZATION).  These are
    *staged* by ``set_value`` and committed only when ``start()`` bundles
    them with START.

    **Independent** — CHILD_LOCK, SOUND, UV_LIGHT, SILENT_MODE, DETERGENT_SET,
    PAUSE, SHUTDOWN, VOICE_SWITCH, … — these take effect immediately via a
    single MQTT ``publishDps`` without needing ``start()``.
    """

    name = "zeo"

    def __init__(
        self,
        channel: MqttChannel,
        *,
        is_dryer: bool = False,
        is_hyperion_halia_hera: bool = False,
        is_m1_muse_metis: bool = False,
    ) -> None:
        """Initialize the Zeo API.

        Args:
            channel: The MQTT channel for device communication.
            is_dryer: ``True`` for standalone dryers (Apollo series).
            is_hyperion_halia_hera: ``True`` for Hyperion/Halia/Hera.
                These series use a different detergent‑type auto‑enable
                protocol (type=0 sends *only* AutoDetergent=False,
                skipping DetergentType entirely).
            is_m1_muse_metis: ``True`` for M1/Muse/Metis series.
                These series skip softener‑related DPs in force_load
                (they do not have a softener compartment).
        """
        self._channel = channel
        self._feature_trait = ZeoFeatureTrait(channel)
        self.is_dryer = is_dryer
        self.is_hyperion_halia_hera = is_hyperion_halia_hera
        self.is_m1_muse_metis = is_m1_muse_metis
        # The DPS cache is populated from three sources.  The final
        # writer wins; there is no priority — ``start()`` reads
        # whatever is in the cache at call time.
        #
        #   1. MQTT push  — unsolicited device status updates (canonical)
        #   2. set_value  — local write after MQTT publish (PERMANENT)
        #   3. query_values — coordinator polling fill
        #
        # Point 2 can NOT be removed even after MQTT push is adopted:
        # ``start()`` packs cached values into the START command, and
        # a user flow like ``set_value(MODE, 2); start()`` relies on
        # point 2 to bridge the gap before the device echoes the change
        # back via MQTT push.  Point 3 CAN be removed once the HA
        # integration subscribes to push.
        self._dps_cache: dict[int, Any] = {}
        self._dps_unsub: Callable[[], None] | None = None

    def close(self) -> None:
        """Unsubscribe from MQTT push updates and release resources.

        Must be called when the API instance is no longer needed to
        prevent stale callbacks from referencing a deallocated object.
        """
        if self._dps_unsub is not None:
            self._dps_unsub()
            self._dps_unsub = None

    async def _ensure_subscribed(self) -> None:
        """Subscribe to MQTT DPS push updates (idempotent)."""
        if self._dps_unsub is not None:
            return
        self._dps_unsub = await self._channel.subscribe(self._on_dps_message)

    def _on_dps_message(self, message: RoborockMessage) -> None:
        """Primary cache injection: MQTT push (protocol 102) from the device.

        Protocol 102 (RPC_RESPONSE) messages carry JSON ``{"dps": {...}}``
        payloads — both query responses and unsolicited status updates.
        This is the canonical data source; ``query_values`` and ``set_value``
        serve as compatibility fallbacks.
        """
        if message.protocol != RoborockMessageProtocol.RPC_RESPONSE:
            return
        try:
            decoded = decode_rpc_response(message)
            self._dps_cache.update(decoded)
        except RoborockException:
            _LOGGER.debug("Failed to decode push message, skipping: %s", message, exc_info=True)

    async def query_values(self, protocols: list[RoborockZeoProtocol]) -> dict[RoborockZeoProtocol, Any]:
        """Query the device for the values of the given protocols.

        Also feeds the DPS cache so that ``start()`` can use cached
        values when the HA integration relies on coordinator polling
        instead of MQTT push.  This cache write (point 3 of 3) CAN be
        removed once HA switches to push-driven state updates.
        """
        response = await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.ID_QUERY: protocols},
            value_encoder=json.dumps,
        )
        for protocol, raw_val in response.items():
            if raw_val is not None:
                self._dps_cache[int(protocol)] = raw_val
        return {protocol: convert_zeo_value(protocol, response.get(protocol)) for protocol in protocols}

    # ── START parameter DPs ────────────────────────────────────────
    # These are the wash/dry programme parameters that the official app
    # bundles together with START in its ``startWith`` / ``presetWith``
    # methods.  Changing any of these via ``set_value`` does NOT
    # immediately launch the device — you must call ``start()``
    # (or ``start_with_preset()``) afterwards to commit the settings
    # and begin the cycle.
    #
    # Washer: Mode, Program, Temp, Rinse, Spin, DryingMode
    # Dryer:  Mode, Program, DryingMode, DryingMethod, SteamVolume
    _START_PARAM_DPS_WASHER: tuple[RoborockZeoProtocol, ...] = (
        RoborockZeoProtocol.MODE,
        RoborockZeoProtocol.PROGRAM,
        RoborockZeoProtocol.TEMP,
        RoborockZeoProtocol.RINSE_TIMES,
        RoborockZeoProtocol.SPIN_LEVEL,
        RoborockZeoProtocol.DRYING_MODE,
    )
    _START_PARAM_DPS_DRYER: tuple[RoborockZeoProtocol, ...] = (
        RoborockZeoProtocol.MODE,
        RoborockZeoProtocol.PROGRAM,
        RoborockZeoProtocol.DRYING_MODE,
        RoborockZeoProtocol.DRYING_METHOD,
        RoborockZeoProtocol.STEAM_VOLUME,
    )

    @property
    def _start_params(self) -> tuple[RoborockZeoProtocol, ...]:
        """Return the START-parameter DP set for this device type."""
        return self._START_PARAM_DPS_DRYER if self.is_dryer else self._START_PARAM_DPS_WASHER

    # DPs that are only included in START when the device reports
    # the corresponding FeatureBit in DP 237.
    _FEATURE_GATED_DPS: tuple[tuple[RoborockZeoProtocol, str], ...] = (
        (RoborockZeoProtocol.WASH_DRY_LINKED, "wash_dry_linkage"),
        (RoborockZeoProtocol.ION_DEODORIZATION, "ion_deodorization"),
    )

    async def _build_feature_gated_dps(self, features: ZeoFeatures | None, dps: dict[RoborockZeoProtocol, Any]) -> None:
        """Add feature-gated DPs to *dps* in-place, batch-querying uncached ones.

        Only includes DPs whose corresponding FeatureBit is set in *features*.
        Uncached DPs are collected and fetched in a single MQTT round trip.
        """
        if features is None:
            return
        to_query: list[RoborockZeoProtocol] = []
        for dp, attr in self._FEATURE_GATED_DPS:
            if getattr(features, attr, False) and int(dp) not in self._dps_cache:
                to_query.append(dp)
        if to_query:
            current = await send_decoded_command(
                self._channel,
                {RoborockZeoProtocol.ID_QUERY: to_query},
                value_encoder=json.dumps,
            )
            for fetched_dp, raw_val in current.items():
                if raw_val is not None:
                    self._dps_cache[fetched_dp] = raw_val
        for dp, attr in self._FEATURE_GATED_DPS:
            if not getattr(features, attr, False):
                continue
            dp_id = int(dp)
            if dp_id in self._dps_cache:
                dps[dp] = self._dps_cache[dp_id]  # cache stores raw ints from MQTT push

    async def _get_start_params(self) -> ZeoStartParams:
        """Return start parameters, using cache when available.

        The DPS cache is populated by MQTT push updates and ``query_values()``
        calls.  If the required core DPs are present in the cache, they are
        returned immediately with zero latency.  Otherwise a single MQTT
        query fills the cache as a fallback.
        """
        await self._ensure_subscribed()
        start_dps = self._start_params
        required_ids = [int(dp) for dp in start_dps]
        if all(dp_id in self._dps_cache for dp_id in required_ids):
            _LOGGER.debug("Using cached start parameters")
        else:
            _LOGGER.debug("Cache miss, querying start parameters")
            current = await send_decoded_command(
                self._channel,
                {RoborockZeoProtocol.ID_QUERY: list(start_dps)},
                value_encoder=json.dumps,
            )
            for dp, raw_val in current.items():
                if raw_val is not None:
                    self._dps_cache[int(dp)] = raw_val
        cache = self._dps_cache
        for dp in start_dps:
            if int(dp) not in cache:
                raise RoborockException(f"Device did not return required DP {dp.name} ({int(dp)})")
        return ZeoStartParams(
            mode=cache[int(RoborockZeoProtocol.MODE)],
            program=cache[int(RoborockZeoProtocol.PROGRAM)],
            temp=cache.get(int(RoborockZeoProtocol.TEMP)),
            rinse_times=cache.get(int(RoborockZeoProtocol.RINSE_TIMES)),
            spin_level=cache.get(int(RoborockZeoProtocol.SPIN_LEVEL)),
            drying_mode=cache.get(int(RoborockZeoProtocol.DRYING_MODE)),
        )

    async def start(self) -> dict[RoborockZeoProtocol, Any]:
        """Start the device, bundling the current programme parameters.

        Corresponds to the official app's ``startWith`` — queries the
        device for Mode, Program, Temperature, Rinse, Spin, DryingMode
        (or their dryer equivalents), discovers capabilities via DP 237,
        and sends everything together with the START command.

        Call ``set_value(MODE, …)`` / ``set_value(TEMP, …)`` etc. first,
        then call ``start()`` to commit and launch.

        For pausing / resuming, use :meth:`resume` which sends only
        ``START = "True"`` without re‑bundling parameters."""
        _LOGGER.debug("Start command: discovering features and building payload")
        await self._feature_trait.refresh()
        features = self._feature_trait.features
        p = await self._get_start_params()
        dps: dict[RoborockZeoProtocol, Any] = {
            RoborockZeoProtocol.START: "True",
            RoborockZeoProtocol.MODE: p.mode,
            RoborockZeoProtocol.PROGRAM: p.program,
        }
        if self.is_dryer:
            total = self._dps_cache.get(int(RoborockZeoProtocol.TOTAL_TIME))
            if total and int(total) > 0:
                dps[RoborockZeoProtocol.TOTAL_TIME] = int(total)
            for dp in (
                RoborockZeoProtocol.DRYING_MODE,
                RoborockZeoProtocol.DRYING_METHOD,
                RoborockZeoProtocol.STEAM_VOLUME,
            ):
                val = self._dps_cache.get(int(dp))
                if val is not None:
                    dps[dp] = val
        else:
            for dp, val in (
                (RoborockZeoProtocol.TEMP, p.temp),
                (RoborockZeoProtocol.RINSE_TIMES, p.rinse_times),
                (RoborockZeoProtocol.SPIN_LEVEL, p.spin_level),
                (RoborockZeoProtocol.DRYING_MODE, p.drying_mode),
            ):
                if val is not None:
                    dps[dp] = val
        await self._build_feature_gated_dps(features, dps)
        return await send_decoded_command(self._channel, dps, value_encoder=lambda x: x, qos=MqttQos.AT_LEAST_ONCE)

    async def resume(self) -> dict[RoborockZeoProtocol, Any]:
        """Resume a paused cycle — sends only ``START = "True"``.

        Matches the official app's ``continue()`` / ``start()``
        (simple single-DP version).  Unlike :meth:`start`, this does
        **not** query or re‑bundle Mode, Program or other parameters —
        the device simply resumes whatever programme was already in
        progress.  ``continue`` is a Python keyword so the method is
        named ``resume``.
        """
        dps: dict[RoborockZeoProtocol, Any] = {RoborockZeoProtocol.START: "True"}
        return await send_decoded_command(self._channel, dps, value_encoder=lambda x: x, qos=MqttQos.AT_LEAST_ONCE)

    # ── Custom programme (DP 222 bitfield) ──────────────────────────

    async def get_custom_mode(self) -> ZeoCustomMode | ZeoDryerCustomMode:
        """Decode the current custom programme from DP 222 + DP 239.

        Returns :class:`ZeoDryerCustomMode` for dryers, else
        :class:`ZeoCustomMode`.
        """
        result = await send_decoded_command(
            self._channel,
            {
                RoborockZeoProtocol.ID_QUERY: [
                    RoborockZeoProtocol.CUSTOM_PARAM_GET,
                    RoborockZeoProtocol.CUSTOM_PROGRAM_CLEANING_TIME,
                ],
            },
            value_encoder=json.dumps,
        )
        raw = result.get(RoborockZeoProtocol.CUSTOM_PARAM_GET, 0)
        total = result.get(RoborockZeoProtocol.CUSTOM_PROGRAM_CLEANING_TIME)
        raw_int = int(raw) if raw else 0
        total_int = int(total) if total else None
        if self.is_dryer:
            return ZeoDryerCustomMode.from_raw(raw_int, total_int)
        return ZeoCustomMode.from_raw(raw_int, total_int)

    # ── Silent mode (bundled 3-DP set) ──────────────────────────────

    async def set_silent_mode(
        self,
        on: bool,
        start_hour: int,
        start_min: int,
        end_hour: int,
        end_min: int,
    ) -> dict[RoborockZeoProtocol, Any]:
        """Enable / disable silent mode with the configured time window.

        Mirroring the official app, this bundles DP 240 (on/off),
        241 (start minute-of-day) and 242 (end minute-of-day) in a
        single ``publishDps`` call.
        """
        start_mins = start_hour * 60 + start_min
        end_mins = end_hour * 60 + end_min
        dps: dict[RoborockZeoProtocol, Any] = {
            RoborockZeoProtocol.SILENT_MODE_ON: 1 if on else 0,
            RoborockZeoProtocol.SILENT_MODE_START_TIME: start_mins,
            RoborockZeoProtocol.SILENT_MODE_END_TIME: end_mins,
        }
        result = await send_decoded_command(self._channel, dps, value_encoder=lambda x: x)
        self._dps_cache[int(RoborockZeoProtocol.SILENT_MODE_ON)] = on
        self._dps_cache[int(RoborockZeoProtocol.SILENT_MODE_START_TIME)] = start_mins
        self._dps_cache[int(RoborockZeoProtocol.SILENT_MODE_END_TIME)] = end_mins
        return result

    # ── Cloud / Panel program save ──────────────────────────────────
    # TODO: extract shared save logic from save_cloud_program and
    #       save_panel_program — the two methods are near-identical
    #       apart from the save-DP selection at the end.

    # DP set that mirrors the optional parameters in the official
    # app's saveCloudProgramWith / savePanelProgramWith.
    _SAVE_PARAM_DPS_WASHER: tuple[RoborockZeoProtocol, ...] = (
        RoborockZeoProtocol.SOAK,
        RoborockZeoProtocol.TEMP,
        RoborockZeoProtocol.RINSE_TIMES,
        RoborockZeoProtocol.SPIN_LEVEL,
        RoborockZeoProtocol.DRYING_MODE,
        RoborockZeoProtocol.DRY_CARE_MODE,
        RoborockZeoProtocol.DRYING_METHOD,
        RoborockZeoProtocol.STEAM_VOLUME,
    )
    _SAVE_PARAM_DPS_DRYER: tuple[RoborockZeoProtocol, ...] = (
        RoborockZeoProtocol.DRYING_MODE,
        RoborockZeoProtocol.DRYING_METHOD,
        RoborockZeoProtocol.STEAM_VOLUME,
    )

    @property
    def _save_params(self) -> tuple[RoborockZeoProtocol, ...]:
        """Return the save-parameter DP set for this device type."""
        return self._SAVE_PARAM_DPS_DRYER if self.is_dryer else self._SAVE_PARAM_DPS_WASHER

    async def _build_save_payload(self) -> dict[RoborockZeoProtocol, Any]:
        """Bundle Mode + Program + cached optionals for a save command."""
        await self._ensure_subscribed()
        payload: dict[RoborockZeoProtocol, Any] = {
            RoborockZeoProtocol.MODE: self._dps_cache.get(int(RoborockZeoProtocol.MODE), 1),
            RoborockZeoProtocol.PROGRAM: self._dps_cache.get(int(RoborockZeoProtocol.PROGRAM), 1),
        }
        for dp in self._save_params:
            val = self._dps_cache.get(int(dp))
            if val is not None:
                payload[dp] = val
        return payload

    async def save_cloud_program(
        self,
        mode: int | None = None,
        program: int | None = None,
        *,
        soak: int | None = None,
        temp: int | None = None,
        rinse: int | None = None,
        spin: int | None = None,
        drying_mode: int | None = None,
        dry_care_mode: int | None = None,
        drying_method: int | None = None,
        steam_volume: int | None = None,
        total_time_min: int | None = None,
    ) -> dict[RoborockZeoProtocol, Any]:
        """Save wash parameters as the custom cloud programme.

        Bundles Mode + Program + optional parameters with either
        DP 254 (SaveAdaptedCloudProgram) or DP 221 (SaveCloudProgram),
        matching the official app's ``saveCloudProgramWith``.

        When parameters are provided they take precedence over the
        internal DPS cache, allowing callers to save a specific
        configuration without first issuing individual set_value calls.
        """
        await self._feature_trait.refresh()
        features = self._feature_trait.features
        user_params: dict[RoborockZeoProtocol, Any] = {}
        if mode is not None:
            user_params[RoborockZeoProtocol.MODE] = mode
        if program is not None:
            user_params[RoborockZeoProtocol.PROGRAM] = program
        for dp, val in (
            (RoborockZeoProtocol.SOAK, soak),
            (RoborockZeoProtocol.TEMP, temp),
            (RoborockZeoProtocol.RINSE_TIMES, rinse),
            (RoborockZeoProtocol.SPIN_LEVEL, spin),
            (RoborockZeoProtocol.DRYING_MODE, drying_mode),
            (RoborockZeoProtocol.DRY_CARE_MODE, dry_care_mode),
            (RoborockZeoProtocol.DRYING_METHOD, drying_method),
            (RoborockZeoProtocol.STEAM_VOLUME, steam_volume),
        ):
            if val is not None:
                user_params[dp] = val
        if total_time_min is not None and total_time_min > 0:
            user_params[RoborockZeoProtocol.TOTAL_TIME] = total_time_min
        payload = await self._build_save_payload()
        payload.update(user_params)
        save_dp = (
            RoborockZeoProtocol.SAVE_ADAPTED_CLOUD_PROGRAM
            if features is not None and features.adapted_custom_program
            else RoborockZeoProtocol.CUSTOM_PARAM_SAVE
        )
        payload[save_dp] = 1
        return await send_decoded_command(self._channel, payload, value_encoder=lambda x: x)

    async def save_panel_program(
        self,
        mode: int | None = None,
        program: int | None = None,
        *,
        soak: int | None = None,
        temp: int | None = None,
        rinse: int | None = None,
        spin: int | None = None,
        drying_mode: int | None = None,
        dry_care_mode: int | None = None,
        drying_method: int | None = None,
        steam_volume: int | None = None,
        total_time_min: int | None = None,
    ) -> dict[RoborockZeoProtocol, Any]:
        """Save wash parameters as the device panel-onboard programme.

        Bundles Mode + Program + optional parameters with DP 252
        (PanelProgramParamsSet) = 1, matching the official app's
        ``savePanelProgramWith``.
        """
        user_params: dict[RoborockZeoProtocol, Any] = {}
        if mode is not None:
            user_params[RoborockZeoProtocol.MODE] = mode
        if program is not None:
            user_params[RoborockZeoProtocol.PROGRAM] = program
        for dp, val in (
            (RoborockZeoProtocol.SOAK, soak),
            (RoborockZeoProtocol.TEMP, temp),
            (RoborockZeoProtocol.RINSE_TIMES, rinse),
            (RoborockZeoProtocol.SPIN_LEVEL, spin),
            (RoborockZeoProtocol.DRYING_MODE, drying_mode),
            (RoborockZeoProtocol.DRY_CARE_MODE, dry_care_mode),
            (RoborockZeoProtocol.DRYING_METHOD, drying_method),
            (RoborockZeoProtocol.STEAM_VOLUME, steam_volume),
        ):
            if val is not None:
                user_params[dp] = val
        if total_time_min is not None and total_time_min > 0:
            user_params[RoborockZeoProtocol.TOTAL_TIME] = total_time_min
        payload = await self._build_save_payload()
        payload.update(user_params)
        payload[RoborockZeoProtocol.PANEL_PROGRAM_PARAMS_SET] = 1
        return await send_decoded_command(self._channel, payload, value_encoder=lambda x: x)

    # ── Preset / delayed start ─────────────────────────────────────
    # Corresponds to the official app's ``presetWith`` — same as
    # ``startWith`` but appends DP 217 (countdown) at the end.

    async def start_with_preset(self, countdown_minutes: int) -> dict[RoborockZeoProtocol, Any]:
        """Start the device with a delayed-start countdown.

        Bundles the current wash settings with DP 217 (Preset /
        countdown in minutes) — a zero-countdown is treated as an
        immediate start.
        """
        _LOGGER.debug("Preset start: discovering features and building payload")
        await self._feature_trait.refresh()
        features = self._feature_trait.features
        p = await self._get_start_params()
        dps: dict[RoborockZeoProtocol, Any] = {
            RoborockZeoProtocol.START: "True",
            RoborockZeoProtocol.MODE: p.mode,
            RoborockZeoProtocol.PROGRAM: p.program,
        }
        if self.is_dryer:
            total = self._dps_cache.get(int(RoborockZeoProtocol.TOTAL_TIME))
            if total and int(total) > 0:
                dps[RoborockZeoProtocol.TOTAL_TIME] = int(total)
            for dp in (
                RoborockZeoProtocol.DRYING_MODE,
                RoborockZeoProtocol.DRYING_METHOD,
                RoborockZeoProtocol.STEAM_VOLUME,
            ):
                val = self._dps_cache.get(int(dp))
                if val is not None:
                    dps[dp] = val
        else:
            for dp, val in (
                (RoborockZeoProtocol.TEMP, p.temp),
                (RoborockZeoProtocol.RINSE_TIMES, p.rinse_times),
                (RoborockZeoProtocol.SPIN_LEVEL, p.spin_level),
                (RoborockZeoProtocol.DRYING_MODE, p.drying_mode),
            ):
                if val is not None:
                    dps[dp] = val
        await self._build_feature_gated_dps(features, dps)
        dps[RoborockZeoProtocol.COUNTDOWN] = countdown_minutes
        return await send_decoded_command(self._channel, dps, value_encoder=lambda x: x, qos=MqttQos.AT_LEAST_ONCE)

    # ── Auto-detergent / softener mode detection ──────────────────
    # Mirror WasherDpsCache.isAutoDetergent / isAutoSoftener from
    # module 725.  When ``is_addition_type_control`` is True the device
    # uses DetergentType/SoftenerType to control auto-dispense;
    # otherwise it uses the legacy AutoDetergent/AutoSoftener DPs.
    is_addition_type_control: bool = False

    async def get_auto_detergent(self) -> bool:
        """Return whether automatic detergent dispensing is enabled."""
        result = await send_decoded_command(
            self._channel,
            {
                RoborockZeoProtocol.ID_QUERY: [
                    RoborockZeoProtocol.DETERGENT_TYPE
                    if self.is_addition_type_control
                    else RoborockZeoProtocol.DETERGENT_SET,
                ],
            },
            value_encoder=json.dumps,
        )
        raw = result.get(
            RoborockZeoProtocol.DETERGENT_TYPE if self.is_addition_type_control else RoborockZeoProtocol.DETERGENT_SET
        )
        return parse_bool(raw) if not self.is_addition_type_control else bool(raw and int(raw) > 0)

    async def get_auto_softener(self) -> bool:
        """Return whether automatic softener dispensing is enabled."""
        result = await send_decoded_command(
            self._channel,
            {
                RoborockZeoProtocol.ID_QUERY: [
                    RoborockZeoProtocol.SOFTENER_TYPE
                    if self.is_addition_type_control
                    else RoborockZeoProtocol.SOFTENER_SET,
                ],
            },
            value_encoder=json.dumps,
        )
        raw = result.get(
            RoborockZeoProtocol.SOFTENER_TYPE if self.is_addition_type_control else RoborockZeoProtocol.SOFTENER_SET
        )
        return parse_bool(raw) if not self.is_addition_type_control else bool(raw and int(raw) > 0)

    # ── Feature-gated DP preloading ─────────────────────────────────
    # Exposes the canonical "forceLoad" + "loadFeatureDps" two-phase
    # polling strategy from WasherDpsManager (module 727).

    _FORCE_LOAD_WASHER_DPS: tuple[RoborockZeoProtocol, ...] = (
        RoborockZeoProtocol.START,
        RoborockZeoProtocol.PAUSE,
        RoborockZeoProtocol.STATE,
        RoborockZeoProtocol.SHUTDOWN,
        RoborockZeoProtocol.MODE,
        RoborockZeoProtocol.PROGRAM,
        RoborockZeoProtocol.CHILD_LOCK,
        RoborockZeoProtocol.TEMP,
        RoborockZeoProtocol.RINSE_TIMES,
        RoborockZeoProtocol.SPIN_LEVEL,
        RoborockZeoProtocol.DRYING_MODE,
        RoborockZeoProtocol.DETERGENT_SET,
        RoborockZeoProtocol.COUNTDOWN,
        RoborockZeoProtocol.PRODUCT_INFO,
        RoborockZeoProtocol.DETERGENT_TYPE,
        RoborockZeoProtocol.WASHING_LEFT,
        RoborockZeoProtocol.DOORLOCK_STATE,
        RoborockZeoProtocol.ERROR,
        RoborockZeoProtocol.CUSTOM_PARAM_SAVE,
        RoborockZeoProtocol.CUSTOM_PARAM_GET,
        RoborockZeoProtocol.SOUND_SET,
        RoborockZeoProtocol.TIMES_AFTER_CLEAN,
        RoborockZeoProtocol.DETERGENT_EMPTY,
        RoborockZeoProtocol.WASHING_LOG,
        RoborockZeoProtocol.OTA_NFO,
        RoborockZeoProtocol.F_C,
    )

    _FORCE_LOAD_DRYER_DPS: tuple[RoborockZeoProtocol, ...] = (
        RoborockZeoProtocol.START,
        RoborockZeoProtocol.PAUSE,
        RoborockZeoProtocol.STATE,
        RoborockZeoProtocol.SHUTDOWN,
        RoborockZeoProtocol.MODE,
        RoborockZeoProtocol.PROGRAM,
        RoborockZeoProtocol.CHILD_LOCK,
        RoborockZeoProtocol.DRYING_MODE,
        RoborockZeoProtocol.COUNTDOWN,
        RoborockZeoProtocol.PRODUCT_INFO,
        RoborockZeoProtocol.WASHING_LEFT,
        RoborockZeoProtocol.DOORLOCK_STATE,
        RoborockZeoProtocol.ERROR,
        RoborockZeoProtocol.CUSTOM_PARAM_GET,
        RoborockZeoProtocol.SOUND_SET,
        RoborockZeoProtocol.WASHING_LOG,
        RoborockZeoProtocol.OTA_NFO,
        RoborockZeoProtocol.F_C,
    )

    async def force_load(self) -> dict[RoborockZeoProtocol, Any]:
        """Phase‑1 poll: load all essential DPs (mirrors the app's ``forceLoad``)."""
        await self._ensure_subscribed()
        if self.is_dryer:
            dps = list(self._FORCE_LOAD_DRYER_DPS)
        else:
            dps = list(self._FORCE_LOAD_WASHER_DPS)
        result = await self.query_values(dps)
        # M1 / Muse / Metis series have no softener compartment — the
        # official app skips softener DPs for those models entirely.
        if not self.is_dryer and not self.is_m1_muse_metis:
            optional: list[RoborockZeoProtocol] = [
                RoborockZeoProtocol.SOFTENER_SET,
                RoborockZeoProtocol.SOFTENER_TYPE,
            ]
            if self.is_addition_type_control:
                optional.append(RoborockZeoProtocol.DEFAULT_SETTING)
            optional.append(RoborockZeoProtocol.SOFTENER_EMPTY)
            await self.query_values(optional)
        return result

    async def load_feature_dps(self) -> dict[RoborockZeoProtocol, Any]:
        """Phase‑2 poll: load DPs gated by FeatureBits from DP 237.

        Call :meth:`force_load` first so that DP 237 is in the cache,
        then call this to discover and load feature-specific DPs.
        """
        await self._feature_trait.refresh()
        features = self._feature_trait.features
        if features is None:
            return {}

        wanted: list[RoborockZeoProtocol] = []
        if features.silent_mode:
            wanted += [
                RoborockZeoProtocol.SILENT_MODE_ON,
                RoborockZeoProtocol.SILENT_MODE_START_TIME,
                RoborockZeoProtocol.SILENT_MODE_END_TIME,
            ]
        if features.dry_care:
            wanted.append(RoborockZeoProtocol.DRY_CARE_MODE)
        if features.smile_light:
            wanted.append(RoborockZeoProtocol.SMILE_LIGHT_STATUS)
        if features.expand_softener or features.wool_detergent:
            wanted.append(RoborockZeoProtocol.SOFTENER_EXPANSION_TYPE)
        if features.concentrated_detergent:
            wanted.append(RoborockZeoProtocol.DETERGENT_EXPANSION_TYPE)
        if features.voice_assistant:
            wanted += [
                RoborockZeoProtocol.VOICE_SWITCH,
                RoborockZeoProtocol.VOICE_VOLUME,
                RoborockZeoProtocol.VOICE_RECORD_INFO,
                RoborockZeoProtocol.VOICE_RECORD,
                RoborockZeoProtocol.SND_STATE,
            ]
        if features.fluff_clean_notification:
            wanted.append(RoborockZeoProtocol.IS_NEED_FLUFF_CLEAN)
        wanted.append(RoborockZeoProtocol.UV_LIGHT)
        if features.power_button_indicator_light:
            wanted.append(RoborockZeoProtocol.POWER_LIGHT)
        if features.dirt_detection:
            wanted += [
                RoborockZeoProtocol.DIRT_DETECTION_SWITCH,
                RoborockZeoProtocol.DIRT_DETECTION_STATUS,
            ]
        if features.steam_care:
            wanted += [
                RoborockZeoProtocol.STEAM_VOLUME,
                RoborockZeoProtocol.STEAM_CARE_TIME,
            ]
        if features.wash_dry_linkage:
            wanted += [
                RoborockZeoProtocol.WASH_DRY_LINKED,
                RoborockZeoProtocol.DEVICE_BOUND,
                RoborockZeoProtocol.CLOTH_PUT_IN,
            ]
            if not self.is_dryer:
                wanted += [
                    RoborockZeoProtocol.CLOTH_READY_TO_DRY_COUNT_DOWN,
                    RoborockZeoProtocol.START_DRYER_ERROR,
                ]
        if features.save_panel_program_params:
            wanted.append(RoborockZeoProtocol.WIFI_LINKAGE_RESET)
        if not wanted:
            return {}
        return await self.query_values(wanted)

    # ── Cleanser config (bundled 4-DP set) ───────────────────────────
    # Matches the official app's ``setCleanserConfig`` which sends
    # DetergentExpansionType + SoftenerExpansionType + DetergentType +
    # SoftenerType in a single ``publishDps`` call.

    async def set_cleanser_config(
        self,
        detergent_expansion: int,
        softener_expansion: int,
        detergent_type: int,
        softener_type: int,
    ) -> dict[RoborockZeoProtocol, Any]:
        """Set all four detergent/softener configuration DPs in one call."""
        dps: dict[RoborockZeoProtocol, Any] = {
            RoborockZeoProtocol.DETERGENT_EXPANSION_TYPE: detergent_expansion,
            RoborockZeoProtocol.SOFTENER_EXPANSION_TYPE: softener_expansion,
            RoborockZeoProtocol.DETERGENT_TYPE: detergent_type,
            RoborockZeoProtocol.SOFTENER_TYPE: softener_type,
        }
        result = await send_decoded_command(self._channel, dps, value_encoder=lambda x: x)
        for dp, v in dps.items():
            self._dps_cache[int(dp)] = v
        return result

    # ── Cloud program load ──────────────────────────────────────────
    # Matches the official app's ``loadCloudProgram`` — sends DP 222=1
    # to instruct the device to load the saved custom programme.

    async def load_cloud_program(self) -> dict[RoborockZeoProtocol, Any]:
        """Load the saved cloud programme from the device (DP 222 = 1)."""
        return await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.CUSTOM_PARAM_GET: 1},
            value_encoder=lambda x: x,
        )

    # ── Management utilities ────────────────────────────────────────
    # Thin wrappers around bundle WasherDpsManager methods (module 727).

    async def check_fcc_state(self) -> dict[RoborockZeoProtocol, Any]:
        """Query DP 10001 for FCC compliance state."""
        return await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.ID_QUERY: [RoborockZeoProtocol.F_C]},
            value_encoder=json.dumps,
        )

    async def load_general_info(self) -> dict[RoborockZeoProtocol, Any]:
        """Query DP 10005 (robotInfo) with 10 s timeout."""
        return await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.ID_QUERY: [RoborockZeoProtocol.PRODUCT_INFO]},
            value_encoder=json.dumps,
        )

    async def upload_log(self) -> dict[RoborockZeoProtocol, Any]:
        """Request the device to upload diagnostic logs (RPC call)."""
        import random as _random

        return await send_decoded_command(
            self._channel,
            {
                RoborockZeoProtocol.RPC_REQUEST: {
                    "id": _random.randint(0, 999999),
                    "method": "user_upload_log",
                }
            },
            value_encoder=lambda x: x,
        )

    async def sync_privacy_to_device(self, agreed: bool) -> dict[RoborockZeoProtocol, Any]:
        """Push the user's privacy-agreement state to the device (DP 10006)."""
        return await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.PRIVACY_INFO: {"userAgreementState": 1 if agreed else 0}},
            value_encoder=lambda x: x,
        )

    async def update_sound_package_info(self) -> dict[RoborockZeoProtocol, Any]:
        """Re‑fetch sound-package metadata (DP 10004)."""
        return await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.ID_QUERY: [RoborockZeoProtocol.SND_STATE]},
            value_encoder=json.dumps,
        )

    # ── Voice / Sound JSON wire formats ─────────────────────────────
    # The official app serialises voice DPs as JSON objects on the wire.
    # Return raw dicts here — ``encode_mqtt_payload`` wraps the entire
    # payload in ``json.dumps``, so pre-serialising would double-encode.
    _VOICE_ENCODERS: dict[RoborockZeoProtocol, Callable[[Any], Any]] = {
        RoborockZeoProtocol.SET_SOUND_PACKAGE: lambda v: v,
        RoborockZeoProtocol.VOICE_SWITCH: lambda v: {"speech_switch": 1 if parse_bool(v) else 0},
        RoborockZeoProtocol.VOICE_VOLUME: lambda v: {"snd_volume": int(v)},
        RoborockZeoProtocol.VOICE_RECORD_DELETE: lambda v: {"dialog_delete": int(v)},
    }

    async def set_value(self, protocol: RoborockZeoProtocol, value: Any) -> dict[RoborockZeoProtocol, Any]:
        """Set a value for a specific protocol on the device.

        ── Two categories of DPs ──

        **startWith params** (MODE, PROGRAM, TEMP, RINSE_TIMES, SPIN_LEVEL,
        DRYING_MODE, DRYING_METHOD, STEAM_VOLUME, SOAK, DRY_CARE_MODE,
        TOTAL_TIME) — these are *settings* that the device stages locally.
        They do **not** take effect until ``start()`` (bundle
        ``startWith``) commits them together with START.  Call
        ``set_value`` to choose the programme, then ``start()`` to
        launch.

        **Independent** (CHILD_LOCK, SOUND_SET, UV_LIGHT, SILENT_MODE_ON,
        DETERGENT_SET, PAUSE, SHUTDOWN, DETERGENT_TYPE, …) — these take
        effect immediately via a single MQTT ``publishDps`` call.  No
        ``start()`` is needed.

        Writes the value to the DPS cache after a successful MQTT
        publish so that a subsequent ``start()`` call sees the latest
        setting immediately.  This cache write is PERMANENT and cannot
        be replaced by MQTT push alone: ``start()`` needs the value
        before the device echoes the change back."""
        if protocol == RoborockZeoProtocol.START and parse_bool(value):
            return await self.start()

        # ── Detergent / Softener type auto-enable (washer only) ────
        # The official app uses three different protocols depending on
        # device series.  Default / Hyperion-Halia-Hera are handled
        # here; ``is_addition_type_control`` devices (newer firmware)
        # send DetergentType alone — callers should set that flag to
        # opt in.
        params: dict[RoborockZeoProtocol, Any] = {protocol: value}
        if not self.is_dryer:
            try:
                int_val = int(value)
            except (ValueError, TypeError):
                int_val = 0
            if protocol == RoborockZeoProtocol.DETERGENT_TYPE:
                if self.is_addition_type_control:
                    pass  # Only DetergentType — no AutoDetergent bundling
                elif self.is_hyperion_halia_hera:
                    if int_val == 0:
                        # Mirror the official app: type=0 sends only
                        # AutoDetergent=False, deleting DetergentType
                        # from the payload.  The deleted DP is NOT
                        # cached below — MQTT push will correct it.
                        params[RoborockZeoProtocol.DETERGENT_SET] = False
                        del params[RoborockZeoProtocol.DETERGENT_TYPE]
                    else:
                        params[RoborockZeoProtocol.DETERGENT_SET] = True
                else:
                    params[RoborockZeoProtocol.DETERGENT_SET] = int_val != 0
            elif protocol == RoborockZeoProtocol.SOFTENER_TYPE:
                if self.is_addition_type_control:
                    pass
                elif self.is_hyperion_halia_hera:
                    if int_val == 0:
                        # Mirror the official app: same pattern as
                        # detergent above — SoftenerType is deleted
                        # from the payload and not cached.
                        params[RoborockZeoProtocol.SOFTENER_SET] = False
                        del params[RoborockZeoProtocol.SOFTENER_TYPE]
                    else:
                        params[RoborockZeoProtocol.SOFTENER_SET] = True
                else:
                    params[RoborockZeoProtocol.SOFTENER_SET] = int_val != 0

        # ── Encoder selection ─────────────────────────────────────
        if (voice_enc := self._VOICE_ENCODERS.get(protocol)) is not None:
            encoder = voice_enc
        elif protocol in _ZEO_BOOLEAN_PROTOCOLS:
            encoder = to_dp_bool
        else:
            encoder = None

        result = await send_decoded_command(self._channel, params, value_encoder=encoder)
        # Cache update must mirror exactly what was sent (params may
        # have been mutated above for Hyperion/Halia/Hera series).
        for dp, v in params.items():
            self._dps_cache[int(dp)] = v
        return result


# ── Dryer model detection ────────────────────────────────────────
# Matches the ``target: "dryer"`` entries in the plugin bundle's
# publishConfig (Apollo series: a188, a204, a258, a265).
_DRYER_PRODUCT_IDS: frozenset[str] = frozenset(
    {"roborock.wm.a188", "roborock.wm.a204", "roborock.wm.a258", "roborock.wm.a265"}
)

# ── Device series detection ──────────────────────────────────────
# The plugin bundle branches on device series for detergent/softener
# auto‑enable logic and for force‑load DP selection.  These frozensets
# mirror the ``isHyperionSeries / isHaliaSeries / isHeraSeries`` and
# ``isM1Series / isMuseSeries / isMetisSeries`` guards.
_HYPERION_HALIA_HERA_PRODUCT_IDS: frozenset[str] = frozenset(
    {
        # Hyperion series
        "roborock.wm.a141",
        "roborock.wm.a149",
        "roborock.wm.a207",
        "roborock.wm.a230",
        # Halia series
        "roborock.wm.a240",
        "roborock.wm.a241",
        # Hera series
        "roborock.wm.a227",
        "roborock.wm.a261",
        "roborock.wm.a273",
        "roborock.wm.a268",
        "roborock.wm.a269",
    }
)

_M1_MUSE_METIS_PRODUCT_IDS: frozenset[str] = frozenset(
    {
        # M1 series
        "roborock.wm.a92",
        "roborock.wm.a93",
        "roborock.wm.a133",
        "roborock.wm.a277",
        "roborock.wm.a162",
        "roborock.wm.a233",
        "roborock.wm.a276",
        "roborock.wm.a234",
        "roborock.wm.a218",
        # Muse series
        "roborock.wm.a142",
        "roborock.wm.a215",
        # Metis series
        "roborock.wm.a154",
        "roborock.wm.a214",
    }
)


def _is_dryer(product: HomeDataProduct) -> bool:
    """Return ``True`` when *product* is a known standalone-dryer model."""
    return product.id in _DRYER_PRODUCT_IDS


def _is_hyperion_halia_hera(product: HomeDataProduct) -> bool:
    """Return ``True`` for Hyperion / Halia / Hera series washers."""
    return product.id in _HYPERION_HALIA_HERA_PRODUCT_IDS


def _is_m1_muse_metis(product: HomeDataProduct) -> bool:
    """Return ``True`` for M1 / Muse / Metis series washers."""
    return product.id in _M1_MUSE_METIS_PRODUCT_IDS


def create(product: HomeDataProduct, mqtt_channel: MqttChannel) -> DyadApi | ZeoApi:
    """Create traits for A01 devices."""
    match product.category:
        case RoborockCategory.WET_DRY_VAC:
            return DyadApi(mqtt_channel)
        case RoborockCategory.WASHING_MACHINE:
            return ZeoApi(
                mqtt_channel,
                is_dryer=_is_dryer(product),
                is_hyperion_halia_hera=_is_hyperion_halia_hera(product),
                is_m1_muse_metis=_is_m1_muse_metis(product),
            )
        case _:
            raise NotImplementedError(f"Unsupported category {product.category}")
