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

Note that these APIs fetch data directly from the device upon request and do not
cache state internally.
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
from roborock.protocols.a01_protocol import decode_rpc_response
from roborock.devices.traits.a01.device_features import ZeoFeatureTrait
from roborock.devices.transport.mqtt_channel import MqttChannel
from roborock.exceptions import RoborockException
from roborock.mqtt.session import MqttQos
from roborock.roborock_message import RoborockDyadDataProtocol, RoborockMessage, RoborockMessageProtocol, RoborockZeoProtocol

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DyadApi",
    "ZeoApi",
    "ZeoFeatureTrait",
]


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


def to_dp_bool(val: Any) -> str:
    """Encode a Zeo/Dyad boolean DP value in the official wire format.

    The official app serialises booleans as the strings ``"True"`` /
    ``"False"`` (its ``DPBoolean`` enum) on SET commands — not as ``1`` / ``0``
    or JSON ``true`` / ``false``.  Mirror that exactly so the device receives
    what it expects.  :func:`parse_bool` is the inverse used when decoding.
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
    RoborockZeoProtocol.COUNTDOWN: lambda val: int(val),
    RoborockZeoProtocol.WASHING_LEFT: lambda val: int(val),
    RoborockZeoProtocol.ERROR: lambda val: ZeoError(val).name,
    RoborockZeoProtocol.TIMES_AFTER_CLEAN: lambda val: int(val),
    RoborockZeoProtocol.DETERGENT_EMPTY: parse_bool,
    RoborockZeoProtocol.SOFTENER_EMPTY: parse_bool,
    RoborockZeoProtocol.DIRT_DETECTION_STATUS: lambda val: ZeoDirtDetectionStatus(val).name,
    RoborockZeoProtocol.TOTAL_TIME: lambda val: int(val),
    RoborockZeoProtocol.FEATURE_BITS: lambda val: int(val),
    RoborockZeoProtocol.SMART_HOSTING_WAITED_TIME: lambda val: int(val),
    RoborockZeoProtocol.FLUFF_CLEANED: parse_bool,
    RoborockZeoProtocol.IS_NEED_FLUFF_CLEAN: parse_bool,
    RoborockZeoProtocol.PANEL_PROGRAM_PARAMS_SET_RESULT: lambda val: int(val),
    RoborockZeoProtocol.DEVICE_BOUND: parse_bool,
    RoborockZeoProtocol.CLOTH_PUT_IN: parse_bool,
    RoborockZeoProtocol.CLOTH_READY_TO_DRY_COUNT_DOWN: lambda val: int(val),
    RoborockZeoProtocol.START_DRYER_ERROR: lambda val: ZeoDryerStartError(val).name,
    RoborockZeoProtocol.DOORLOCK_STATE: parse_bool,
    RoborockZeoProtocol.DEFAULT_SETTING: lambda val: int(val),
    RoborockZeoProtocol.LIGHT_SETTING: parse_bool,
    RoborockZeoProtocol.DETERGENT_VOLUME: lambda val: int(val),
    RoborockZeoProtocol.SOFTENER_VOLUME: lambda val: int(val),
    # meta — read-only
    RoborockZeoProtocol.VOICE_RECORD_INFO: lambda val: val,  # JSON
    RoborockZeoProtocol.VOICE_RECORD: lambda val: val,  # JSON
    # read-write
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
    RoborockZeoProtocol.SMART_HOSTING_TIME: lambda val: int(val),
    RoborockZeoProtocol.SOFTENER_EXPANSION_TYPE: lambda val: int(val),
    RoborockZeoProtocol.DETERGENT_EXPANSION_TYPE: lambda val: int(val),
    RoborockZeoProtocol.SMILE_LIGHT_STATUS: parse_bool,
    RoborockZeoProtocol.POWER_LIGHT: parse_bool,
    RoborockZeoProtocol.PANEL_PROGRAM_PARAMS_SET: lambda val: int(val),
    RoborockZeoProtocol.PANEL_TIMING_PROGRAM_PARAMS: lambda val: int(val),
    RoborockZeoProtocol.STEAM_CARE_TIME: lambda val: int(val),
    RoborockZeoProtocol.WIFI_LINKAGE_RESET: lambda val: int(val),
    RoborockZeoProtocol.CUSTOM_PROGRAM_CLEANING_TIME: lambda val: int(val),
    RoborockZeoProtocol.SAVE_ADAPTED_CLOUD_PROGRAM: lambda val: int(val),
    RoborockZeoProtocol.CHILD_LOCK: parse_bool,
    RoborockZeoProtocol.DETERGENT_SET: parse_bool,
    RoborockZeoProtocol.SOFTENER_SET: parse_bool,
    RoborockZeoProtocol.APP_AUTHORIZATION: parse_bool,
    # meta — read-write
    RoborockZeoProtocol.SET_SOUND_PACKAGE: lambda val: val,  # JSON
    RoborockZeoProtocol.VOICE_VOLUME: lambda val: val,  # int or JSON
    RoborockZeoProtocol.VOICE_SWITCH: parse_bool,
    RoborockZeoProtocol.VOICE_RECORD_DELETE: lambda val: int(val),
}


# Protocols whose value is a boolean.  Derived from the entry tables so the
# write path (set_value / start) always encodes them as "True"/"False",
# matching the official app's DPBoolean wire format.
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
    """Water temperature."""

    rinse_times: int | None = None
    """Number of rinse cycles."""

    spin_level: int | None = None
    """Spin speed (RPM)."""

    drying_mode: int | None = None
    """Drying mode (e.g. quick, iron, store)."""


class ZeoApi(Trait):
    """API for interacting with Zeo devices."""

    name = "zeo"

    def __init__(self, channel: MqttChannel) -> None:
        """Initialize the Zeo API."""
        self._channel = channel
        self._feature_trait = ZeoFeatureTrait(channel)
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

    # The DP IDs that must be bundled with START commands. These are the
    # universally-supported core parameters common to all Zeo devices.
    _START_PARAM_DPS: tuple[RoborockZeoProtocol, ...] = (
        RoborockZeoProtocol.MODE,
        RoborockZeoProtocol.PROGRAM,
        RoborockZeoProtocol.TEMP,
        RoborockZeoProtocol.RINSE_TIMES,
        RoborockZeoProtocol.SPIN_LEVEL,
        RoborockZeoProtocol.DRYING_MODE,
    )

    # DPs that are only included in START when the device reports
    # the corresponding FeatureBit in DP 237.
    _FEATURE_GATED_DPS: tuple[tuple[RoborockZeoProtocol, str], ...] = (
        (RoborockZeoProtocol.WASH_DRY_LINKED, "wash_dry_linkage"),
        (RoborockZeoProtocol.ION_DEODORIZATION, "ion_deodorization"),
    )

    async def _get_start_params(self) -> ZeoStartParams:
        """Return start parameters, using cache when available.

        The DPS cache is populated by MQTT push updates and ``query_values()``
        calls.  If the required core DPs are present in the cache, they are
        returned immediately with zero latency.  Otherwise a single MQTT
        query fills the cache as a fallback.
        """
        await self._ensure_subscribed()
        required_ids = [int(dp) for dp in self._START_PARAM_DPS]
        if all(dp_id in self._dps_cache for dp_id in required_ids):
            _LOGGER.debug("Using cached start parameters")
        else:
            _LOGGER.debug("Cache miss, querying start parameters")
            current = await send_decoded_command(
                self._channel,
                {RoborockZeoProtocol.ID_QUERY: list(self._START_PARAM_DPS)},
                value_encoder=json.dumps,
            )
            for dp, raw_val in current.items():
                if raw_val is not None:
                    self._dps_cache[int(dp)] = raw_val
        cache = self._dps_cache
        for dp in self._START_PARAM_DPS:
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
        """Start the device using the current mode and program parameters.

        Discovers device capabilities via DP 237 on first call, then
        bundles the current wash settings with the START command at
        QoS 1 as required by the device firmware.
        """
        _LOGGER.debug("Start command: discovering features and building payload")
        await self._feature_trait.refresh()
        features = self._feature_trait.features
        p = await self._get_start_params()
        # START is an action DP: the official app sends DPBoolean.True ("True"),
        # not the integer 1.  Mirror the reference wire format exactly.
        dps: dict[RoborockZeoProtocol, Any] = {
            RoborockZeoProtocol.START: "True",
            RoborockZeoProtocol.MODE: p.mode,
            RoborockZeoProtocol.PROGRAM: p.program,
        }
        for dp, val in (
            (RoborockZeoProtocol.TEMP, p.temp),
            (RoborockZeoProtocol.RINSE_TIMES, p.rinse_times),
            (RoborockZeoProtocol.SPIN_LEVEL, p.spin_level),
            (RoborockZeoProtocol.DRYING_MODE, p.drying_mode),
        ):
            if val is not None:
                dps[dp] = val
        # Feature-gated DPs: only include when device reports support.
        # Collect all uncached DPs first, then issue a single batch query
        # instead of N sequential round trips.
        if features is not None:
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
                for dp_id, raw_val in current.items():
                    if raw_val is not None:
                        self._dps_cache[dp_id] = raw_val
            for dp, attr in self._FEATURE_GATED_DPS:
                if not getattr(features, attr, False):
                    continue
                dp_id = int(dp)
                if dp_id in self._dps_cache:
                    # Booleans must be serialised as "True"/"False" (DPBoolean).
                    dps[dp] = to_dp_bool(self._dps_cache[dp_id])
        return await send_decoded_command(self._channel, dps, value_encoder=lambda x: x, qos=MqttQos.AT_LEAST_ONCE)

    async def set_value(self, protocol: RoborockZeoProtocol, value: Any) -> dict[RoborockZeoProtocol, Any]:
        """Set a value for a specific protocol on the device.

        Writes the value to the DPS cache after a successful MQTT
        publish so that a subsequent ``start()`` call sees the latest
        setting immediately.  This cache write is PERMANENT and cannot
        be replaced by MQTT push alone: ``start()`` needs the value
        before the device echoes the change back.
        """
        if protocol == RoborockZeoProtocol.START and parse_bool(value):
            return await self.start()
        # Booleans are serialised as "True"/"False" (DPBoolean) on the wire.
        encoder = to_dp_bool if protocol in _ZEO_BOOLEAN_PROTOCOLS else None
        params: dict[RoborockZeoProtocol, Any] = {protocol: value}
        result = await send_decoded_command(self._channel, params, value_encoder=encoder)
        self._dps_cache[int(protocol)] = value
        return result


def create(product: HomeDataProduct, mqtt_channel: MqttChannel) -> DyadApi | ZeoApi:
    """Create traits for A01 devices."""
    match product.category:
        case RoborockCategory.WET_DRY_VAC:
            return DyadApi(mqtt_channel)
        case RoborockCategory.WASHING_MACHINE:
            return ZeoApi(mqtt_channel)
        case _:
            raise NotImplementedError(f"Unsupported category {product.category}")
