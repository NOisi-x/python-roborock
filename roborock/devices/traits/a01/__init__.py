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
    ZeoDetergentType,
    ZeoDryAndCare,
    ZeoDryerStartError,
    ZeoDryingMethod,
    ZeoDryingMode,
    ZeoError,
    ZeoMode,
    ZeoProgram,
    ZeoRinse,
    ZeoSoak,
    ZeoSoftenerType,
    ZeoSpin,
    ZeoState,
    ZeoSteamVolume,
    ZeoTemperature,
)
from roborock.devices.rpc.a01_channel import send_decoded_command
from roborock.devices.traits import Trait
from roborock.devices.transport.mqtt_channel import MqttChannel
from roborock.exceptions import RoborockException
from roborock.mqtt.session import MqttQos
from roborock.roborock_message import RoborockDyadDataProtocol, RoborockZeoProtocol

_LOGGER = logging.getLogger(__name__)

__init__ = [
    "DyadApi",
    "ZeoApi",
]


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
    RoborockDyadDataProtocol.AUTO_DRY: lambda val: bool(val),
    RoborockDyadDataProtocol.MESH_LEFT: lambda val: int(360000 - val * 60),
    RoborockDyadDataProtocol.BRUSH_LEFT: lambda val: int(360000 - val * 60),
    RoborockDyadDataProtocol.ERROR: lambda val: DyadError(val).name,
    RoborockDyadDataProtocol.VOLUME_SET: lambda val: int(val),
    RoborockDyadDataProtocol.STAND_LOCK_AUTO_RUN: lambda val: bool(val),
    RoborockDyadDataProtocol.AUTO_DRY_MODE: lambda val: bool(val),
    RoborockDyadDataProtocol.SILENT_DRY_DURATION: lambda val: int(val),  # in minutes
    RoborockDyadDataProtocol.SILENT_MODE: lambda val: bool(val),
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
    RoborockZeoProtocol.DETERGENT_EMPTY: lambda val: bool(val),
    RoborockZeoProtocol.SOFTENER_EMPTY: lambda val: bool(val),
    RoborockZeoProtocol.DIRT_DETECTION_STATUS: lambda val: int(val),
    RoborockZeoProtocol.TOTAL_TIME: lambda val: int(val),
    RoborockZeoProtocol.FEATURE_BITS: lambda val: int(val),
    RoborockZeoProtocol.SMART_HOSTING_WAITED_TIME: lambda val: int(val),
    RoborockZeoProtocol.FLUFF_CLEANED: lambda val: bool(val),
    RoborockZeoProtocol.IS_NEED_FLUFF_CLEAN: lambda val: bool(val),
    RoborockZeoProtocol.PANEL_PROGRAM_PARAMS_SET_RESULT: lambda val: int(val),
    RoborockZeoProtocol.DEVICE_BOUND: lambda val: bool(val),
    RoborockZeoProtocol.CLOTH_PUT_IN: lambda val: bool(val),
    RoborockZeoProtocol.CLOTH_READY_TO_DRY_COUNT_DOWN: lambda val: int(val),
    RoborockZeoProtocol.START_DRYER_ERROR: lambda val: ZeoDryerStartError(val).name,
    RoborockZeoProtocol.DOORLOCK_STATE: lambda val: bool(val),
    RoborockZeoProtocol.DEFAULT_SETTING: lambda val: int(val),
    RoborockZeoProtocol.LIGHT_SETTING: lambda val: bool(val),
    RoborockZeoProtocol.DETERGENT_VOLUME: lambda val: int(val),
    RoborockZeoProtocol.SOFTENER_VOLUME: lambda val: int(val),
    # read-write
    RoborockZeoProtocol.MODE: lambda val: ZeoMode(val).name,
    RoborockZeoProtocol.PROGRAM: lambda val: ZeoProgram(val).name,
    RoborockZeoProtocol.TEMP: lambda val: ZeoTemperature(val).name,
    RoborockZeoProtocol.RINSE_TIMES: lambda val: ZeoRinse(val).name,
    RoborockZeoProtocol.SPIN_LEVEL: lambda val: ZeoSpin(val).name,
    RoborockZeoProtocol.DRYING_MODE: lambda val: ZeoDryingMode(val).name,
    RoborockZeoProtocol.DETERGENT_TYPE: lambda val: ZeoDetergentType(val).name,
    RoborockZeoProtocol.SOFTENER_TYPE: lambda val: ZeoSoftenerType(val).name,
    RoborockZeoProtocol.SOUND_SET: lambda val: bool(val),
    RoborockZeoProtocol.DIRT_DETECTION_SWITCH: lambda val: bool(val),
    RoborockZeoProtocol.SOAK: lambda val: ZeoSoak(val).name,
    RoborockZeoProtocol.SILENT_MODE_ON: lambda val: bool(val),
    RoborockZeoProtocol.SILENT_MODE_START_TIME: lambda val: int(val),
    RoborockZeoProtocol.SILENT_MODE_END_TIME: lambda val: int(val),
    RoborockZeoProtocol.DRY_CARE_MODE: lambda val: ZeoDryAndCare(val).name,
    RoborockZeoProtocol.WASH_DRY_LINKED: lambda val: bool(val),
    RoborockZeoProtocol.DRYING_METHOD: lambda val: ZeoDryingMethod(val).name,
    RoborockZeoProtocol.STEAM_VOLUME: lambda val: ZeoSteamVolume(val).name,
    RoborockZeoProtocol.ION_DEODORIZATION: lambda val: bool(val),
    RoborockZeoProtocol.UV_LIGHT: lambda val: bool(val),
    RoborockZeoProtocol.SMART_HOSTING: lambda val: bool(val),
    RoborockZeoProtocol.SMART_HOSTING_TIME: lambda val: int(val),
    RoborockZeoProtocol.SOFTENER_EXPANSION_TYPE: lambda val: int(val),
    RoborockZeoProtocol.DETERGENT_EXPANSION_TYPE: lambda val: int(val),
    RoborockZeoProtocol.SMILE_LIGHT_STATUS: lambda val: bool(val),
    RoborockZeoProtocol.POWER_LIGHT: lambda val: bool(val),
    RoborockZeoProtocol.PANEL_PROGRAM_PARAMS_SET: lambda val: int(val),
    RoborockZeoProtocol.PANEL_TIMING_PROGRAM_PARAMS: lambda val: int(val),
    RoborockZeoProtocol.STEAM_CARE_TIME: lambda val: int(val),
    RoborockZeoProtocol.WIFI_LINKAGE_RESET: lambda val: int(val),
    RoborockZeoProtocol.CUSTOM_PROGRAM_CLEANING_TIME: lambda val: int(val),
    RoborockZeoProtocol.SAVE_ADAPTED_CLOUD_PROGRAM: lambda val: int(val),
    RoborockZeoProtocol.CHILD_LOCK: lambda val: bool(val),
    RoborockZeoProtocol.DETERGENT_SET: lambda val: bool(val),
    RoborockZeoProtocol.SOFTENER_SET: lambda val: bool(val),
    RoborockZeoProtocol.APP_AUTHORIZATION: lambda val: bool(val),
}


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
        """Set a value for a specific protocol on the device."""
        params = {protocol: value}
        return await send_decoded_command(self._channel, params)


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

    async def query_values(self, protocols: list[RoborockZeoProtocol]) -> dict[RoborockZeoProtocol, Any]:
        """Query the device for the values of the given protocols."""
        response = await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.ID_QUERY: protocols},
            value_encoder=json.dumps,
        )
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

    async def _get_current_params(self) -> ZeoStartParams:
        """Query the device and return typed start parameters.

        Raises :exc:`RoborockException` if any of the required DPs are not
        returned by the device.
        """
        current = await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.ID_QUERY: list(self._START_PARAM_DPS)},
            value_encoder=json.dumps,
        )
        for dp in self._START_PARAM_DPS:
            if dp not in current:
                raise RoborockException(
                    f"Device did not return required DP {dp.name} ({int(dp)})"
                )
        return ZeoStartParams(
            mode=current[RoborockZeoProtocol.MODE],
            program=current[RoborockZeoProtocol.PROGRAM],
            temp=current.get(RoborockZeoProtocol.TEMP),
            rinse_times=current.get(RoborockZeoProtocol.RINSE_TIMES),
            spin_level=current.get(RoborockZeoProtocol.SPIN_LEVEL),
            drying_mode=current.get(RoborockZeoProtocol.DRYING_MODE),
        )

    async def start(self) -> dict[RoborockZeoProtocol, Any]:
        """Start the device using the current mode and program parameters.

        Queries the device for the current wash settings and sends them
        bundled with the START command. This uses QoS 1 as required by the
        device firmware.
        """
        _LOGGER.debug("Start command: querying current device state")
        p = await self._get_current_params()
        dps: dict[RoborockZeoProtocol, Any] = {
            RoborockZeoProtocol.START: 1,
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
        return await send_decoded_command(
            self._channel, dps, value_encoder=lambda x: x, qos=MqttQos.AT_LEAST_ONCE
        )

    async def set_value(self, protocol: RoborockZeoProtocol, value: Any) -> dict[RoborockZeoProtocol, Any]:
        """Set a value for a specific protocol on the device."""
        if protocol == RoborockZeoProtocol.START and value == 1:
            return await self.start()
        params: dict[RoborockZeoProtocol, Any] = {protocol: value}
        return await send_decoded_command(self._channel, params, value_encoder=lambda x: x)


def create(product: HomeDataProduct, mqtt_channel: MqttChannel) -> DyadApi | ZeoApi:
    """Create traits for A01 devices."""
    match product.category:
        case RoborockCategory.WET_DRY_VAC:
            return DyadApi(mqtt_channel)
        case RoborockCategory.WASHING_MACHINE:
            return ZeoApi(mqtt_channel)
        case _:
            raise NotImplementedError(f"Unsupported category {product.category}")
