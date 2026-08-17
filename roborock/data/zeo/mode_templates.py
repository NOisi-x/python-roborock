"""Mode-template resolution for Zeo (washing machine / dryer) devices.

Replicates the official iOS app's mode-template selection logic, extracted
verbatim from ``index.ios.bundle``:

1. ``deviceModel`` string → series name (46 prefixes, longest-first match).
2. series + region → data-module id (the ``$()`` function, 41 branches).
3. data-module id → list of mode templates (4212 modes across 70 modules).

Each mode template maps a program to its parameter capabilities, including
the exact protocol-level (DP) enum values the device expects — e.g. soak
sends ``ZeoSoak`` (0..5), temperature sends ``ZeoTemperature`` (1..7), etc.
This mirrors the ``le(e)`` mapping function in the bundle, which turns the
flat ``*_levelN`` fields into the structured ``config`` object the app uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .zeo_code_mappings import (
    ZeoDryAndCare,
    ZeoDryingMethod,
    ZeoDryingMode,
    ZeoFeatureBits,
    ZeoMode,
    ZeoProgram,
    ZeoRinse,
    ZeoSoak,
    ZeoSpin,
    ZeoSteamVolume,
    ZeoTemperature,
)

_DATA_DIR = Path(__file__).parent / "mode_data"

# ── 字段 → DP 档位映射 ───────────────────────────────────────────────────
# 每个 ``*_levelN`` 字段（N 为档位编号）对应一个协议层枚举成员。
# 来源：bundle 的 ``le(e)`` 映射 + 各 ``dpXxx`` getter（UI 枚举 → DP 枚举）。

# soak_level1~6 → ZeoSoak(0~5)。N-1 即 DP 档位（SoakLevel.Normal=0 → DPSoak.L0=0）。
_SOAK_FIELDS = {
    1: ZeoSoak.normal,  # 0, 0min
    2: ZeoSoak.low,  # 1, 5min
    3: ZeoSoak.medium,  # 2, 10min
    4: ZeoSoak.high,  # 3, 15min
    5: ZeoSoak.max,  # 4, 20min
    6: ZeoSoak.very_max,  # 5, 30min
}


def _soak_level_range(feature_bits: int) -> range:
    """soak_level6 (30 min) only exists when ``ThirtyMinSoak`` is enabled.

    Mirrors the bundle's ``isSupport(ThirtyMinSoak) ? level6 : null`` guard in
    the ``le(e)`` mapping: without ``ZeoFeatureBits.thirty_min_soak`` (bit 8)
    the 30-minute level is never offered even if the mode data has a value.
    """
    if feature_bits & (1 << int(ZeoFeatureBits.thirty_min_soak)):
        return range(1, 7)
    return range(1, 6)

# temperature_level1~7 → ZeoTemperature(1~7)。N 即 DP 档位。
# 注意字段顺序在 bundle 里是乱的（level1,level6,level2,...），因为 UI 按
# 温度值排序（0,20,30,40,60,90,95），但 DP 档位编号仍是 1~7。
_TEMPERATURE_FIELDS = {
    1: ZeoTemperature.normal,  # L1, 0C
    2: ZeoTemperature.low,  # L2, 30C
    3: ZeoTemperature.medium,  # L3, 40C
    4: ZeoTemperature.high,  # L4, 60C
    5: ZeoTemperature.max,  # L5, 90C
    6: ZeoTemperature.twenty_c,  # L6, 20C
    7: ZeoTemperature.ninety_c,  # L7, 95C
}

# rinse_level0~5 → ZeoRinse(0~5)。N 即 DP 档位。
_RINSE_FIELDS = {
    0: ZeoRinse.none,
    1: ZeoRinse.min,
    2: ZeoRinse.low,
    3: ZeoRinse.mid,
    4: ZeoRinse.high,
    5: ZeoRinse.max,
}

# spin_speed_level1~7 → ZeoSpin(1~7)。N 即 DP 档位。
_SPIN_FIELDS = {
    1: ZeoSpin.none,  # 0 RPM
    2: ZeoSpin.very_low,  # 400 RPM
    3: ZeoSpin.low,  # 600 RPM
    4: ZeoSpin.mid,  # 800 RPM
    5: ZeoSpin.high,  # 1000 RPM
    6: ZeoSpin.very_high,  # 1200 RPM
    7: ZeoSpin.max,  # 1400 RPM
}

# dry_level1~3 → ZeoDryingMode（非线性的交叉映射）。
# 来源：bundle ``dpDryMode`` getter：
#   DryDegree.Low → DPDryingMode.Iron, Mid → Quick, High → Store.
_DRY_FIELDS = {
    1: ZeoDryingMode.iron,  # Low → Iron(2)
    2: ZeoDryingMode.quick,  # Mid → Quick(1)
    3: ZeoDryingMode.store,  # High → Store(3)
}

# dry_and_care_level1~2 → ZeoDryAndCare(1~2)。N 即 DP 档位。
_DRY_AND_CARE_FIELDS = {
    1: ZeoDryAndCare.soft,
    2: ZeoDryAndCare.normal,
}

# dry_method_level1~3 → ZeoDryingMethod(1~3)。N 即 DP 档位。
_DRY_METHOD_FIELDS = {
    1: ZeoDryingMethod.l1,  # Saving
    2: ZeoDryingMethod.l2,  # Standard
    3: ZeoDryingMethod.l3,  # SuperFast
}

# steam_treatment_level1~5 → ZeoSteamVolume(0~4)。N-1 即 DP 档位。
_STEAM_FIELDS = {
    1: ZeoSteamVolume.none,  # L0, None
    2: ZeoSteamVolume.low,  # L1, Min
    3: ZeoSteamVolume.medium,  # L2, Low
    4: ZeoSteamVolume.high,  # L3, Mid
    5: ZeoSteamVolume.max,  # L4, High
}


# ── 数据结构 ────────────────────────────────────────────────────────────


@dataclass
class ZeoParamConfig:
    """One parameter's available levels (mirrors the bundle ``config`` object).

    ``default`` and ``support`` hold protocol-level enum members (what the
    device actually expects).  ``raw_values`` holds the physical display
    values extracted verbatim from the mode data (minutes, °C, RPM, ...).
    """

    default: Any | None = None
    """Default protocol-level level (a ``ZeoXxx`` enum member), or ``None``."""

    support: list[Any] = field(default_factory=list)
    """All available protocol-level levels, in ascending field order."""

    raw_values: list[Any] = field(default_factory=list)
    """Physical display values for each supported level, same order as ``support``."""


@dataclass
class ZeoModeConfig:
    """A program's parameter capabilities (mirrors the bundle ``config`` object)."""

    soak: ZeoParamConfig | None = None
    temperature: ZeoParamConfig | None = None
    rinse: ZeoParamConfig | None = None
    spin: ZeoParamConfig | None = None
    dry: ZeoParamConfig | None = None
    dry_and_care: ZeoParamConfig | None = None
    dry_method: ZeoParamConfig | None = None
    steam_volume: ZeoParamConfig | None = None

    is_support_auto_detergent: bool = False
    is_support_auto_softener: bool = False
    is_support_uvc: bool = False
    is_support_reservation: bool = False
    is_support_dirt_detection: bool = False
    is_ion_on: bool = False
    default_ion_status: bool = False


@dataclass
class ZeoModeTemplate:
    """One program template (a single mode object from the data module)."""

    id: int
    dp: ZeoProgram
    program_type: ZeoMode
    program_name: str
    is_in_app: bool
    config: ZeoModeConfig

    # Raw flat fields, kept verbatim for advanced consumers.
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# ── 映射逻辑（对应 bundle 的 ``le(e)``）────────────────────────────────


def _build_param(
    raw: dict[str, Any],
    field_prefix: str,
    level_range: range,
    field_map: dict[int, Any],
    default_field: str,
    default_to_level: Any,
) -> ZeoParamConfig | None:
    """Build a :class:`ZeoParamConfig` from flat ``*_levelN`` fields.

    ``default_to_level`` maps the ``default_*`` field value to a level number
    ``N`` used as the ``field_map`` key.  Callers pass a callable that takes
    the raw default value and returns the level number (e.g. ``lambda d: d``
    for rinse where the default is already 0-based, or ``lambda d: d - 1``
    for soak where the default is 1-based).
    """
    support: list[Any] = []
    raw_values: list[Any] = []
    for n in level_range:
        v = raw.get(f"{field_prefix}{n}")
        if v is not None and n in field_map:
            support.append(field_map[n])
            raw_values.append(v)

    if not support:
        return None

    default: Any | None = None
    d = raw.get(default_field)
    if d is not None:
        level_n = default_to_level(d)
        # Only trust the default when its level is actually part of the
        # support set (e.g. default_soak_level=6 is meaningless when the
        # 30-minute level is filtered out by the ThirtyMinSoak feature bit).
        if level_n in field_map and level_n in level_range:
            default = field_map[level_n]

    return ZeoParamConfig(default=default, support=support, raw_values=raw_values)


def _build_config(raw: dict[str, Any], feature_bits: int = 0) -> ZeoModeConfig:
    """Turn a flat mode object into a structured :class:`ZeoModeConfig`."""
    return ZeoModeConfig(
        soak=_build_param(
            raw, "soak_level", _soak_level_range(feature_bits), _SOAK_FIELDS,
            "default_soak_level", lambda d: d,
        ),
        temperature=_build_param(
            raw, "temperature_level", range(1, 8), _TEMPERATURE_FIELDS,
            "default_temperature_level", lambda d: d,
        ),
        rinse=_build_param(
            raw, "rinse_level", range(0, 6), _RINSE_FIELDS,
            "default_rinse_level", lambda d: d,
        ),
        spin=_build_param(
            raw, "spin_speed_level", range(1, 8), _SPIN_FIELDS,
            "default_spin_speed_level", lambda d: d,
        ),
        dry=_build_param(
            raw, "dry_level", range(1, 4), _DRY_FIELDS,
            "default_dry_level", lambda d: d,
        ),
        dry_and_care=_build_param(
            raw, "dry_and_care_level", range(1, 3), _DRY_AND_CARE_FIELDS,
            "default_dry_and_care_level", lambda d: d,
        ),
        dry_method=_build_param(
            raw, "dry_method_level", range(1, 4), _DRY_METHOD_FIELDS,
            "default_dry_method_level", lambda d: d,
        ),
        steam_volume=_build_param(
            raw, "steam_treatment_level", range(1, 6), _STEAM_FIELDS,
            "default_steam_treatment_level", lambda d: d,
        ),
        is_support_auto_detergent=bool(raw.get("is_support_auto_detergent")),
        is_support_auto_softener=bool(raw.get("is_support_auto_softener")),
        is_support_uvc=bool(raw.get("is_support_uvc")),
        is_support_reservation=bool(raw.get("is_support_reservation")),
        is_support_dirt_detection=bool(raw.get("is_support_dirt_detection")),
        is_ion_on=bool(raw.get("is_ion_on")),
        default_ion_status=bool(raw.get("default_ion_status")),
    )


def _mode_from_raw(raw: dict[str, Any], feature_bits: int = 0) -> ZeoModeTemplate:
    """Turn a flat mode object into a :class:`ZeoModeTemplate`."""
    return ZeoModeTemplate(
        id=int(raw["id"]),
        dp=ZeoProgram(int(raw["dp"])),
        program_type=ZeoMode(int(raw["program_type"])),
        program_name=str(raw.get("program_name") or ""),
        is_in_app=bool(raw.get("is_in_app")),
        config=_build_config(raw, feature_bits),
        raw=raw,
    )


# ── 数据加载 ────────────────────────────────────────────────────────────


class _ModeDataStore:
    """Lazily loads the three extracted data files."""

    def __init__(self) -> None:
        self._prefixes: dict[str, str] | None = None
        self._selector: list[dict] | None = None
        self._modules: dict[str, list[dict]] | None = None

    @property
    def prefixes(self) -> dict[str, str]:
        if self._prefixes is None:
            with (_DATA_DIR / "device_model_prefixes.json").open(encoding="utf-8") as f:
                self._prefixes = json.load(f)
        return self._prefixes

    @property
    def selector(self) -> list[dict]:
        if self._selector is None:
            with (_DATA_DIR / "mode_selector_map.json").open(encoding="utf-8") as f:
                self._selector = json.load(f)
        return self._selector

    @property
    def modules(self) -> dict[str, list[dict]]:
        if self._modules is None:
            with (_DATA_DIR / "mode_data_all_normalized.json").open(encoding="utf-8") as f:
                self._modules = json.load(f)
        return self._modules


_store = _ModeDataStore()


# ── 解析函数 ────────────────────────────────────────────────────────────


def _match_prefix(device_model: str) -> str | None:
    """Longest-prefix match of ``device_model`` against the 46 known prefixes."""
    matched: list[tuple[int, str]] = []
    for prefix, series in _store.prefixes.items():
        if device_model.startswith(prefix):
            matched.append((len(prefix), series))
    if not matched:
        return None
    matched.sort(reverse=True)
    return matched[0][1]


def _series_matches(branch_series: str, series: str) -> bool:
    """Return True if ``branch_series`` (e.g. ``"isM1|isM1Overseas"``) covers ``series``."""
    return series in branch_series.split("|")


def _resolve_module(series: str | None, location: str | None, feature_bits: int = 0) -> int:
    """Resolve the data-module id for a series + region + feature bits.

    Mirrors the bundle ``$()`` function's ordered ternary chain: the first
    matching branch wins.  ``location=None`` (or an unknown region) falls
    through to each branch's default module.

    One branch (``isPoseidonPro``) has a ``feature`` condition — it requires
    the ``deep_self_clean`` feature bit (``ZeoFeatureBits.deep_self_clean``,
    bit 19); without it the plain ``isPoseidonPro`` branch is used instead.
    """
    for branch in _store.selector:
        branch_series = branch["series"]
        if series is not None and _series_matches(branch_series, series):
            feature = branch.get("feature")
            if feature == "deep_self_clean":
                if not (feature_bits & (1 << 19)):
                    continue  # condition not met → try the next branch
            regions = branch.get("regions", {})
            if location is not None and location in regions:
                return int(regions[location])
            return int(branch["default"])
    # fallback branch
    return int(_store.selector[-1]["default"])


def resolve_mode_templates(
    device_model: str,
    location: str | None = None,
    feature_bits: int = 0,
) -> list[ZeoModeTemplate]:
    """Resolve the mode templates for a device.

    Args:
        device_model: The device model string, e.g. ``"roborock.wm.a92"`` (M1S).
        location: Optional two-letter region code (``jp``, ``tw``, ``kr``,
            ``de``, ``au``, ``ru``, ``ch``, ``no``, ``my``, ``th``).  ``None``
            selects each branch's default (mainland China) module.
        feature_bits: The device's ``FEATURE_BITS`` (DP 237) value, used for
            the ``isPoseidonPro`` + ``deep_self_clean`` branch selection and
            for gating the 30-minute soak level
            (``ZeoFeatureBits.thirty_min_soak``, bit 8).  ``0`` is a safe
            default (no deep self-clean, no 30-minute soak).

    Returns:
        A list of :class:`ZeoModeTemplate` — one per program the device
        supports, in the app's native order.
    """
    series = _match_prefix(device_model)
    module_id = _resolve_module(series, location, feature_bits)
    raw_modes = _store.modules.get(str(module_id), [])
    return [_mode_from_raw(raw, feature_bits) for raw in raw_modes]
