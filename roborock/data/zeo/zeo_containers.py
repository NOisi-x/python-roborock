"""Data containers for Zeo (washing machine / dryer) devices."""

from dataclasses import dataclass

from ..containers import RoborockBase


@dataclass
class ZeoStartParams(RoborockBase):
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
class ZeoCustomMode(RoborockBase):
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
class ZeoDryerCustomMode(RoborockBase):
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
