"""Zeo device feature discovery trait.

A dedicated trait that queries DP 237 (FEATURE_BITS) and resolves the
product model against known ID lists — both computed once per session
and cached.
"""

import json
import logging
from dataclasses import dataclass, fields

from roborock.data.zeo.zeo_code_mappings import ZeoFeatureBits
from roborock.devices.rpc.a01_channel import send_decoded_command
from roborock.devices.traits import Trait
from roborock.devices.transport.mqtt_channel import MqttChannel
from roborock.roborock_message import RoborockZeoProtocol

_LOGGER = logging.getLogger(__name__)

# ── Product model detection ──────────────────────────────────────

_DRYER_PRODUCT_IDS: frozenset[str] = frozenset(
    {
        "roborock.wm.a188",
        "roborock.wm.a204",
        "roborock.wm.a258",
        "roborock.wm.a265",
    }
)

_HYPERION_HALIA_HERA_PRODUCT_IDS: frozenset[str] = frozenset(
    {
        "roborock.wm.a141",
        "roborock.wm.a149",
        "roborock.wm.a207",
        "roborock.wm.a230",
        "roborock.wm.a240",
        "roborock.wm.a241",
        "roborock.wm.a227",
        "roborock.wm.a261",
        "roborock.wm.a273",
        "roborock.wm.a268",
        "roborock.wm.a269",
    }
)

_M1_MUSE_METIS_PRODUCT_IDS: frozenset[str] = frozenset(
    {
        "roborock.wm.a92",
        "roborock.wm.a93",
        "roborock.wm.a133",
        "roborock.wm.a277",
        "roborock.wm.a162",
        "roborock.wm.a233",
        "roborock.wm.a276",
        "roborock.wm.a234",
        "roborock.wm.a218",
        "roborock.wm.a142",
        "roborock.wm.a215",
        "roborock.wm.a154",
        "roborock.wm.a214",
    }
)


@dataclass
class ZeoFeatures:
    """Device capability flags for Zeo devices, parsed from DP 237 (FEATURE_BITS)."""

    adapted_custom_program: bool = False
    concentrated_detergent: bool = False
    deep_self_clean: bool = False
    detect_door_status: bool = False
    dirt_detection: bool = False
    dry_care: bool = False
    expand_softener: bool = False
    fluff_clean_notification: bool = False
    ion_deodorization: bool = False
    new_custom_program: bool = False
    power_button_indicator_light: bool = False
    save_panel_program_params: bool = False
    set_params_in_working: bool = False
    set_uvc_in_appointment: bool = False
    set_uvc_in_pause: bool = False
    silent_mode: bool = False
    smart_hosting: bool = False
    smile_light: bool = False
    steam_care: bool = False
    thirty_min_soak: bool = False
    voice_assistant: bool = False
    voice_assistant_record: bool = False
    wash_dry_linkage: bool = False
    wool_detergent: bool = False

    @classmethod
    def from_feature_bits(cls, raw: int) -> "ZeoFeatures":
        """Parse a raw FEATURE_BITS integer into a ZeoFeatures instance.

        Field names must match members of :class:`ZeoFeatureBits`
        one-to-one, so the mapping is derived by name reflection
        instead of a hardcoded lookup table.
        """
        kwargs: dict[str, bool] = {}
        for f in fields(cls):
            bit_pos = getattr(ZeoFeatureBits, f.name)
            kwargs[f.name] = bool(raw & (1 << int(bit_pos)))
        return cls(**kwargs)


class ZeoFeatureTrait(Trait):
    """Discovers and caches Zeo device capabilities.

    Two sources of capability information are resolved:

    * **Product model ID** (from ``HomeDataProduct.id``) — determines
      whether the device is a standalone dryer, Hyperion/Halia/Hera
      series, or M1/Muse/Metis series.  These are static per model and
      do not change across sessions.
    * **FEATURE_BITS** (DP 237) — a 24‑bit mask of runtime feature
      flags, queried once from the device and parsed into
      :class:`ZeoFeatures`.
    """

    name = "zeo_features"

    def __init__(self, channel: MqttChannel, product_id: str | None = None) -> None:
        """Initialize the feature trait.

        Args:
            channel: The MQTT channel for sending commands.
            product_id: The ``HomeDataProduct.id`` string (e.g.
                ``"roborock.wm.a234"``).  Used for static product‑type
                detection.  Pass ``None`` when the product is unknown.
        """
        self._channel = channel
        self._features: ZeoFeatures | None = None
        self._product_id = product_id or ""

    # ----------------------------------------------------------------
    #  Product type queries — determined from static model ID,
    #  available immediately without any device round‑trip.
    # ----------------------------------------------------------------

    @property
    def is_dryer(self) -> bool:
        """Return ``True`` for standalone dryers."""
        return self._product_id in _DRYER_PRODUCT_IDS

    @property
    def is_hyperion_halia_hera(self) -> bool:
        """Return ``True`` for Hyperion / Halia / Hera series washers."""
        return self._product_id in _HYPERION_HALIA_HERA_PRODUCT_IDS

    @property
    def is_m1_muse_metis(self) -> bool:
        """Return ``True`` for M1 / Muse / Metis series washers."""
        return self._product_id in _M1_MUSE_METIS_PRODUCT_IDS

    # ----------------------------------------------------------------
    #  DP 237 feature bits — queried once, cached in memory.
    # ----------------------------------------------------------------

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
