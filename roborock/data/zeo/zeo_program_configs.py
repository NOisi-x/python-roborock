"""Programme-config driven START parameter validation for Zeo appliances."""

from dataclasses import dataclass
from typing import Type

from .zeo_code_mappings import (
    RoborockEnum,
    ZeoDryingMode,
    ZeoMode,
    ZeoProgram,
    ZeoRinse,
    ZeoSoak,
    ZeoSpin,
    ZeoTemperature,
)
from .zeo_containers import ZeoStartParams
from .zeo_program_config_data import ZEO_PROGRAM_RAW_DATA

__all__ = [
    "ZeoLevelRange",
    "ZeoProgramSupports",
    "ZeoProgramConfig",
    "get_program_config",
    "all_program_configs",
    "validate_start_params",
    "default_start_params",
]


@dataclass(frozen=True)
class ZeoLevelRange:
    """Supported level positions and default for one START parameter.

    Positions are 1-based protocol enum values (e.g. ``ZeoSpin.mid`` is
    position 4).  The physical values in the raw d[26] table (RPM, degrees,
    minutes...) are display-only and never sent.
    """

    default: int | None
    support: tuple[int, ...]


@dataclass(frozen=True)
class ZeoProgramSupports:
    """Per-programme capability flags from the d[26] table."""

    reservation: bool
    auto_detergent: bool
    auto_softener: bool
    uvc: bool


@dataclass(frozen=True)
class ZeoProgramConfig:
    """Resolved d[26] mode entry for one ``(mode, program)`` pair.

    A ``None`` range means the programme does not support that parameter at
    all (e.g. washing programmes have no ``drying_mode`` on this model).
    """

    mode_id: int
    mode: ZeoMode
    program: ZeoProgram
    priority: int
    program_name: str
    is_in_app: bool
    supports: ZeoProgramSupports
    temperature: ZeoLevelRange | None = None
    rinse: ZeoLevelRange | None = None
    spin: ZeoLevelRange | None = None
    drying_mode: ZeoLevelRange | None = None
    soak: ZeoLevelRange | None = None


# (field prefix, number of positions) for each level group in the d[26] table.
_LEVEL_GROUPS: tuple[tuple[str, int], ...] = (
    ("soak", 6),
    ("temperature", 7),
    ("rinse", 5),
    ("spin_speed", 7),
    ("dry", 3),
)


def _build_range(data: dict, prefix: str, count: int) -> ZeoLevelRange | None:
    """Build a level range from the flat d[26] fields of one mode entry."""
    support = tuple(
        i for i in range(1, count + 1) if data.get(f"{prefix}_level{i}") is not None
    )
    if not support:
        return None
    default = data.get(f"default_{prefix}_level")
    if default is not None and default not in support:
        # Defensive: never advertise a default outside the supported set.
        default = None
    return ZeoLevelRange(default=default, support=support)


def _parse(data: dict) -> ZeoProgramConfig:
    """Resolve one raw d[26] mode entry into a :class:`ZeoProgramConfig`."""
    ranges = {prefix: _build_range(data, prefix, count) for prefix, count in _LEVEL_GROUPS}
    return ZeoProgramConfig(
        mode_id=int(data["id"]),
        mode=ZeoMode(int(data["program_type"])),
        program=ZeoProgram(int(data["dp"])),
        priority=int(data["priority"]),
        program_name=str(data["program_name"]),
        is_in_app=bool(data["is_in_app"]),
        supports=ZeoProgramSupports(
            reservation=bool(data.get("is_support_reservation", False)),
            auto_detergent=bool(data.get("is_support_auto_detergent", False)),
            auto_softener=bool(data.get("is_support_auto_softener", False)),
            uvc=bool(data.get("is_support_uvc", False)),
        ),
        soak=ranges["soak"],
        temperature=ranges["temperature"],
        rinse=ranges["rinse"],
        spin=ranges["spin_speed"],
        drying_mode=ranges["dry"],
    )


_CONFIGS: tuple[ZeoProgramConfig, ...] = tuple(_parse(d) for d in ZEO_PROGRAM_RAW_DATA)

_BY_MODE_PROGRAM: dict[tuple[ZeoMode, ZeoProgram], ZeoProgramConfig] = {
    (config.mode, config.program): config for config in _CONFIGS
}


def get_program_config(mode: ZeoMode, program: ZeoProgram) -> ZeoProgramConfig | None:
    """Return the resolved configuration for a ``(mode, program)`` pair.

    Returns ``None`` when the pair is not present in the d[26] table.
    """
    return _BY_MODE_PROGRAM.get((mode, program))


def all_program_configs(mode: ZeoMode) -> list[ZeoProgramConfig]:
    """Return every program configuration for the given mode."""
    return [config for config in _CONFIGS if config.mode == mode]


# :class:`ZeoStartParams` attributes (and their matching
# :class:`ZeoProgramConfig` attributes) that can be validated against the
# d[26] table.
_VALIDATED_FIELDS: tuple[str, ...] = (
    "temperature",
    "rinse",
    "spin",
    "drying_mode",
    "soak",
)

# Enum type used to construct each validated param from a level position.
_FIELD_ENUMS: dict[str, Type[RoborockEnum]] = {
    "temperature": ZeoTemperature,
    "rinse": ZeoRinse,
    "spin": ZeoSpin,
    "drying_mode": ZeoDryingMode,
    "soak": ZeoSoak,
}


def validate_start_params(
    params: ZeoStartParams,
    config: ZeoProgramConfig | None = None,
) -> list[str]:
    """Validate START params against a programme configuration.

    Returns a list of human-readable error strings; an empty list means the
    params are valid.

    Semantics:

    * A ``None`` param and an enum member whose int value is ``0`` are
      treated as "not set" and skipped, matching the send path (zero-valued
      empty enum members are not emitted by ``build_param_dps``).
    * When ``config`` is omitted it is resolved from ``params.mode`` and
      ``params.program``.  If no configuration exists (pair absent from the
      d[26] table), no validation is performed and an empty list is returned;
      the caller decides whether to trust the input.
    * Parameters that the d[26] table does not model (``drying_method``,
      ``steam_volume``, ``dry_and_care``, ``total_time``...) are not checked.
    """
    if config is None:
        config = get_program_config(params.mode, params.program)
    if config is None:
        return []
    errors: list[str] = []
    for field in _VALIDATED_FIELDS:
        value = getattr(params, field)
        if value is None or int(value) == 0:
            continue
        level_range: ZeoLevelRange | None = getattr(config, field)
        if level_range is None:
            errors.append(
                f"{field}={int(value)} is not supported by programme "
                f"{config.program_name!r} (mode {config.mode.name})"
            )
        elif int(value) not in level_range.support:
            supported = ", ".join(str(i) for i in level_range.support)
            errors.append(
                f"{field}={int(value)} is not supported by programme "
                f"{config.program_name!r}; supported levels: {supported}"
            )
    return errors


def default_start_params(mode: ZeoMode, program: ZeoProgram) -> ZeoStartParams:
    """Build START params with every supported parameter at its default.

    Parameters without a configured default (or without a range at all) are
    left unset.  The result is a valid input for ``start_with``.
    """
    config = get_program_config(mode, program)
    kwargs: dict[str, object] = {"mode": mode, "program": program}
    if config is not None:
        for field in _VALIDATED_FIELDS:
            level_range: ZeoLevelRange | None = getattr(config, field)
            if level_range is None or level_range.default is None:
                continue
            kwargs[field] = _FIELD_ENUMS[field](level_range.default)
    return ZeoStartParams(**kwargs)
