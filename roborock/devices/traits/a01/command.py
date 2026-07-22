"""Zeo command trait"""

import json
import logging
from collections.abc import Callable
from typing import Any

from roborock.data.zeo.zeo_containers import ZeoStartParams
from roborock.devices.rpc.a01_channel import send_decoded_command
from roborock.devices.traits.a01.device_features import ZeoFeatureTrait
from roborock.devices.transport.mqtt_channel import MqttChannel
from roborock.mqtt.session import MqttQos
from roborock.roborock_message import RoborockZeoProtocol

_LOGGER = logging.getLogger(__name__)

_START_PARAM_DPS_WASHER: list[RoborockZeoProtocol] = [
    RoborockZeoProtocol.MODE,
    RoborockZeoProtocol.PROGRAM,
    RoborockZeoProtocol.TEMP,
    RoborockZeoProtocol.RINSE_TIMES,
    RoborockZeoProtocol.SPIN_LEVEL,
    RoborockZeoProtocol.DRYING_MODE,
    RoborockZeoProtocol.DETERGENT_SET,
    RoborockZeoProtocol.SOFTENER_SET,
    RoborockZeoProtocol.COUNTDOWN,
    RoborockZeoProtocol.SOAK,
]

_START_PARAM_DPS_DRYER: list[RoborockZeoProtocol] = [
    RoborockZeoProtocol.MODE,
    RoborockZeoProtocol.PROGRAM,
    RoborockZeoProtocol.DRYING_MODE,
    RoborockZeoProtocol.TOTAL_TIME,
    RoborockZeoProtocol.DRYING_METHOD,
    RoborockZeoProtocol.STEAM_VOLUME,
    RoborockZeoProtocol.COUNTDOWN,
]

_FIELD_TO_DP: dict[str, RoborockZeoProtocol] = {
    "mode": RoborockZeoProtocol.MODE,
    "program": RoborockZeoProtocol.PROGRAM,
    "temperature": RoborockZeoProtocol.TEMP,
    "rinse": RoborockZeoProtocol.RINSE_TIMES,
    "spin": RoborockZeoProtocol.SPIN_LEVEL,
    "drying_mode": RoborockZeoProtocol.DRYING_MODE,
    "drying_method": RoborockZeoProtocol.DRYING_METHOD,
    "steam_volume": RoborockZeoProtocol.STEAM_VOLUME,
    "total_time": RoborockZeoProtocol.TOTAL_TIME,
    "soak": RoborockZeoProtocol.SOAK,
    "dry_and_care": RoborockZeoProtocol.DRY_CARE_MODE,
}

_FEATURE_GATED_DPS: dict[RoborockZeoProtocol, str] = {
    RoborockZeoProtocol.ION_DEODORIZATION: "ion_deodorization",
    RoborockZeoProtocol.WASH_DRY_LINKED: "wash_dry_linkage",
    RoborockZeoProtocol.SMART_HOSTING: "smart_hosting",
}


class ZeoCommandTrait:
    """Trait for sending commands to Zeo devices."""

    def __init__(
        self,
        *,
        channel: MqttChannel,
        dps_cache: dict[int, Any],
        feature_trait: ZeoFeatureTrait,
        proto_entries: dict[RoborockZeoProtocol, Callable],
    ) -> None:
        """Initialize the command trait."""

        self._channel = channel
        self._dps_cache = dps_cache
        self._feature_trait = feature_trait
        self._proto_entries = proto_entries

    def _convert_value(self, protocol: RoborockZeoProtocol, value: Any) -> Any:
        """Convert a protocol value using the injected entries table."""
        if (converter := self._proto_entries.get(protocol)) is not None:
            try:
                return converter(value)
            except (ValueError, TypeError):
                return None
        return None

    async def start_program(self) -> dict[RoborockZeoProtocol, Any]:
        """Start the device, bundling the current programme parameters."""
        features = self._feature_trait.features
        p = await self._get_start_params()
        dps: dict[RoborockZeoProtocol, Any] = {RoborockZeoProtocol.START: "True"}
        for field_name, dp in _FIELD_TO_DP.items():
            val = getattr(p, field_name)
            if val is not None:
                dps[dp] = val
        for dp, attr_name in _FEATURE_GATED_DPS.items():
            if features is not None and getattr(features, attr_name, False):
                val = self._dps_cache.get(int(dp))
                if val is not None:
                    dps[dp] = val
        await send_decoded_command(
            self._channel,
            dps,
            qos=MqttQos.AT_LEAST_ONCE,
            value_encoder=lambda x: x,
        )
        for dp, v in dps.items():
            self._dps_cache[int(dp)] = v
        return {proto: self._convert_value(proto, dps.get(proto)) for proto in dps}

    async def pause(self) -> dict[RoborockZeoProtocol, Any]:
        """Pause the current programme (DP 201 = "True")."""
        dps = {RoborockZeoProtocol.PAUSE: "True"}
        result = await send_decoded_command(self._channel, dps)
        self._dps_cache[int(RoborockZeoProtocol.PAUSE)] = 1
        return result

    async def resume(self) -> dict[RoborockZeoProtocol, Any]:
        """Start/continue a paused programme (DP 200 = "True").
        Only works while the device is powered on.
        """
        dps = {RoborockZeoProtocol.START: "True"}
        result = await send_decoded_command(self._channel, dps)
        self._dps_cache[int(RoborockZeoProtocol.START)] = 1
        return result

    async def shutdown(self) -> dict[RoborockZeoProtocol, Any]:
        """Power off the device (DP 202 = "True").
        Only works while the device is powered on.
        """
        dps = {RoborockZeoProtocol.SHUTDOWN: "True"}
        result = await send_decoded_command(self._channel, dps)
        self._dps_cache[int(RoborockZeoProtocol.SHUTDOWN)] = 1
        return result

    async def _get_start_params(self) -> ZeoStartParams:
        """Read programme settings, querying the device on cache miss."""
        cache = self._dps_cache
        wanted = _START_PARAM_DPS_DRYER if self._feature_trait.is_dryer else _START_PARAM_DPS_WASHER
        need_refresh = int(RoborockZeoProtocol.MODE) not in cache or int(RoborockZeoProtocol.PROGRAM) not in cache
        if need_refresh:
            raw = await send_decoded_command(
                self._channel,
                {RoborockZeoProtocol.ID_QUERY: wanted},
                value_encoder=json.dumps,
            )
            for dp in wanted:
                if (val := raw.get(dp)) is not None:
                    cache[int(dp)] = val
        return ZeoStartParams(
            mode=cache[int(RoborockZeoProtocol.MODE)],
            program=cache[int(RoborockZeoProtocol.PROGRAM)],
            temperature=cache.get(int(RoborockZeoProtocol.TEMP)),
            rinse=cache.get(int(RoborockZeoProtocol.RINSE_TIMES)),
            spin=cache.get(int(RoborockZeoProtocol.SPIN_LEVEL)),
            drying_mode=cache.get(int(RoborockZeoProtocol.DRYING_MODE)),
            drying_method=cache.get(int(RoborockZeoProtocol.DRYING_METHOD)),
            steam_volume=cache.get(int(RoborockZeoProtocol.STEAM_VOLUME)),
            total_time=cache.get(int(RoborockZeoProtocol.TOTAL_TIME)),
            soak=cache.get(int(RoborockZeoProtocol.SOAK)),
            dry_and_care=cache.get(int(RoborockZeoProtocol.DRY_CARE_MODE)),
        )
