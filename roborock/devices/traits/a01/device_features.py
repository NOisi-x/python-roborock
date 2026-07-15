"""Zeo device feature discovery trait.

Mirrors the V1 :class:`roborock.devices.traits.v1.device_features.DeviceFeaturesTrait`
pattern: a dedicated trait that queries DP 237 (FEATURE_BITS) once per session
and caches the parsed result.
"""

import json
import logging

from roborock.device_features import ZeoFeatures
from roborock.devices.rpc.a01_channel import send_decoded_command
from roborock.devices.traits import Trait
from roborock.devices.transport.mqtt_channel import MqttChannel
from roborock.roborock_message import RoborockZeoProtocol

_LOGGER = logging.getLogger(__name__)


class ZeoFeatureTrait(Trait):
    """Discovers and caches Zeo device capabilities from DP 237.

    Device features are immutable during a session (they only change
    after a firmware update) so the result is cached in memory after
    the first query.
    """

    name = "zeo_features"

    def __init__(self, channel: MqttChannel) -> None:
        """Initialize the feature trait."""
        self._channel = channel
        self._features: ZeoFeatures | None = None

    @property
    def features(self) -> ZeoFeatures | None:
        """The cached device features, or ``None`` if not yet discovered."""
        return self._features

    async def refresh(self) -> ZeoFeatures:
        """Query DP 237 and parse into a :class:`ZeoFeatures` instance.

        On the first call this sends an MQTT query; subsequent calls
        return the cached result.
        """
        if self._features is not None:
            return self._features
        _LOGGER.debug("Discovering Zeo device features")
        current = await send_decoded_command(
            self._channel,
            {RoborockZeoProtocol.ID_QUERY: [RoborockZeoProtocol.FEATURE_BITS]},
            value_encoder=json.dumps,
        )
        raw = current.get(RoborockZeoProtocol.FEATURE_BITS, 0)
        self._features = ZeoFeatures.from_feature_bits(raw)
        _LOGGER.debug("Device features: %s", self._features)
        return self._features
