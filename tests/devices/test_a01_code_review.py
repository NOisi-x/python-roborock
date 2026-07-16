"""Regression tests for A01/Zeo code-review findings.

These guard the two highest-risk findings from the review against the
official Roborock Washer app plugin bundle:

1.  Boolean decode/encode must survive the ``bool("False") is True`` trap and
    must serialise booleans as the strings ``"True"`` / ``"False"`` (the
    official ``DPBoolean`` enum), never ``1`` / ``0`` or JSON booleans.
2.  ``ZeoFeatureBits`` bit positions must match the bundle exactly; a single
    off-by-one bit shift silently corrupts every device-capability gate.
"""

from dataclasses import fields
from unittest.mock import AsyncMock, Mock, patch

import pytest

from roborock.data.zeo.zeo_code_mappings import ZeoFeatureBits
from roborock.device_features import ZeoFeatures
from roborock.devices.traits.a01 import (
    _ZEO_BOOLEAN_PROTOCOLS,
    DyadApi,
    ZeoApi,
    parse_bool,
    to_dp_bool,
)
from roborock.roborock_message import RoborockDyadDataProtocol, RoborockZeoProtocol

_ID_QUERY_INT = int(RoborockZeoProtocol.ID_QUERY)


@pytest.fixture
def mock_channel():
    channel = Mock()
    channel.send_command = AsyncMock()
    channel.subscribe = AsyncMock(return_value=Mock())
    return channel


# --------------------------------------------------------------------------- #
# Pure-function guards: boolean decode/encode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("True", True),
        ("true", True),
        ("False", False),  # the trap: bool("False") == True
        ("false", False),
        (1, True),
        (0, False),
        (True, True),
        (False, False),
        ("1", True),
        ("0", False),
    ],
)
def test_parse_bool(raw, expected):
    assert parse_bool(raw) is expected


def test_parse_bool_trap_is_avoided():
    # This is the exact bug class the review caught: a naive bool() on the
    # wire string "False" would evaluate to True.
    assert parse_bool("False") is False
    assert bool("False") is True  # documents why parse_bool is needed


@pytest.mark.parametrize(
    ("val", "expected"),
    [(True, "True"), (False, "False"), (1, "True"), (0, "False"), ("True", "True"), ("False", "False")],
)
def test_to_dp_bool(val, expected):
    assert to_dp_bool(val) == expected, f"to_dp_bool({val!r}) should be {expected}"


def test_boolean_protocol_sets_are_consistent():
    # Every protocol whose decoder is parse_bool must be in the boolean set,
    # and vice-versa.  Drift here means a boolean would be serialised wrong.
    assert RoborockZeoProtocol.UV_LIGHT in _ZEO_BOOLEAN_PROTOCOLS
    assert RoborockZeoProtocol.WASH_DRY_LINKED in _ZEO_BOOLEAN_PROTOCOLS
    assert RoborockZeoProtocol.ION_DEODORIZATION in _ZEO_BOOLEAN_PROTOCOLS
    # Non-boolean protocols must NOT be coerced to strings.
    assert RoborockZeoProtocol.MODE not in _ZEO_BOOLEAN_PROTOCOLS
    assert RoborockZeoProtocol.TEMP not in _ZEO_BOOLEAN_PROTOCOLS


# --------------------------------------------------------------------------- #
# Pure-function guard: feature-bit bit order (the most severe bug class)
# --------------------------------------------------------------------------- #


def test_zeo_feature_bits_match_bundle_positions():
    """Every ZeoFeatureBits member must have the correct integer value.

    These are the exact bit offsets extracted from the official Roborock
    Washer app plugin bundle (index.ios.bundle, module 726).  A single
    off-by-one shift silently corrupts every device-capability gate.
    """
    expected = {
        ZeoFeatureBits.smart_hosting: 0,
        ZeoFeatureBits.silent_mode: 1,
        ZeoFeatureBits.new_custom_program: 2,
        ZeoFeatureBits.dry_care: 3,
        ZeoFeatureBits.set_uvc_in_appointment: 4,
        ZeoFeatureBits.detect_door_status: 5,
        ZeoFeatureBits.expand_softener: 6,
        ZeoFeatureBits.set_params_in_working: 7,
        ZeoFeatureBits.thirty_min_soak: 8,
        ZeoFeatureBits.smile_light: 9,
        ZeoFeatureBits.set_uvc_in_pause: 10,
        ZeoFeatureBits.concentrated_detergent: 11,
        ZeoFeatureBits.wool_detergent: 12,
        ZeoFeatureBits.voice_assistant: 13,
        ZeoFeatureBits.adapted_custom_program: 14,
        ZeoFeatureBits.voice_assistant_record: 15,
        ZeoFeatureBits.fluff_clean_notification: 16,
        ZeoFeatureBits.power_button_indicator_light: 17,
        ZeoFeatureBits.dirt_detection: 18,
        ZeoFeatureBits.deep_self_clean: 19,
        ZeoFeatureBits.save_panel_program_params: 20,
        ZeoFeatureBits.steam_care: 21,
        ZeoFeatureBits.wash_dry_linkage: 22,
        ZeoFeatureBits.ion_deodorization: 23,
    }
    for member, expected_pos in expected.items():
        assert int(member) == expected_pos, f"{member.name} is at bit {int(member)}, expected {expected_pos}"


def test_zeo_features_fields_map_to_feature_bits():
    """Every field on ZeoFeatures must have a corresponding ZeoFeatureBits member.

    A field without a matching bit-position member would cause
    ZeoFeatures.from_feature_bits() to raise AttributeError.
    """
    for f in fields(ZeoFeatures):
        assert hasattr(ZeoFeatureBits, f.name), f"ZeoFeatures.{f.name} has no matching ZeoFeatureBits member"


def test_zeo_features_from_feature_bits_decodes_correct_bits():
    """Setting a single bit only affects its field; all others stay False."""
    # Set bit 0 (smart_hosting) and bit 22 (wash_dry_linkage) only.
    raw = (1 << int(ZeoFeatureBits.smart_hosting)) | (1 << int(ZeoFeatureBits.wash_dry_linkage))
    features = ZeoFeatures.from_feature_bits(raw)
    assert features.smart_hosting is True
    assert features.wash_dry_linkage is True
    # Adjacent / unrelated bits must stay False — this is exactly what the
    # original off-by-one bit order got wrong.
    assert features.silent_mode is False
    assert features.ion_deodorization is False
    assert features.new_custom_program is False


def test_zeo_features_all_bits_set_decodes_all_true():
    """Raw = all bits set → every field must be True."""
    all_bits = (1 << len(ZeoFeatureBits)) - 1
    features = ZeoFeatures.from_feature_bits(all_bits)
    for f in fields(ZeoFeatures):
        assert getattr(features, f.name) is True, f"{f.name} should be True"


def test_zeo_features_all_bits_clear_decodes_all_false():
    """Raw = 0 → every field must be False."""
    features = ZeoFeatures.from_feature_bits(0)
    for f in fields(ZeoFeatures):
        assert getattr(features, f.name) is False, f"{f.name} should be False"


# --------------------------------------------------------------------------- #
# Write-path guards: boolean SET serialisation and START value
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_zeo_set_value_bool_serialises_as_string(mock_channel):
    with patch("roborock.devices.traits.a01.send_decoded_command", new_callable=AsyncMock) as mock_send:
        api = ZeoApi(mock_channel)
        for py_val, wire in ((True, "True"), (False, "False"), (1, "True"), (0, "False")):
            await api.set_value(RoborockZeoProtocol.UV_LIGHT, py_val)
            args, kwargs = mock_send.call_args
            params = args[1]
            encoder = kwargs.get("value_encoder")
            encoded = {k: (encoder(v) if encoder else v) for k, v in params.items()}
            assert encoded[RoborockZeoProtocol.UV_LIGHT] == wire
            mock_send.reset_mock()


@pytest.mark.asyncio
async def test_dyad_set_value_bool_serialises_as_string(mock_channel):
    with patch("roborock.devices.traits.a01.send_decoded_command", new_callable=AsyncMock) as mock_send:
        api = DyadApi(mock_channel)
        await api.set_value(RoborockDyadDataProtocol.SILENT_MODE, True)
        args, kwargs = mock_send.call_args
        params = args[1]
        encoder = kwargs.get("value_encoder")
        encoded = {k: (encoder(v) if encoder else v) for k, v in params.items()}
        assert encoded[RoborockDyadDataProtocol.SILENT_MODE] == "True"


@pytest.mark.asyncio
async def test_zeo_start_sends_true_and_bundles_core_params(mock_channel):
    """start() must send START="True" (DPBoolean.True), not an integer."""
    mock_channel.subscribe = AsyncMock(return_value=Mock())

    captured = []

    async def _side_effect(channel, params, **kwargs):
        captured.append((params, kwargs))
        if _ID_QUERY_INT in {int(k) for k in params}:
            queried = params[_ID_QUERY_INT]
            if RoborockZeoProtocol.FEATURE_BITS in queried:
                return {int(RoborockZeoProtocol.FEATURE_BITS): 0}  # no features
            return {
                int(RoborockZeoProtocol.MODE): 1,
                int(RoborockZeoProtocol.PROGRAM): 1,
                int(RoborockZeoProtocol.TEMP): 30,
                int(RoborockZeoProtocol.RINSE_TIMES): 2,
                int(RoborockZeoProtocol.SPIN_LEVEL): 800,
                int(RoborockZeoProtocol.DRYING_MODE): 1,
            }
        return {}

    with (
        patch("roborock.devices.traits.a01.send_decoded_command", new_callable=AsyncMock, side_effect=_side_effect),
        patch(
            "roborock.devices.traits.a01.device_features.send_decoded_command",
            new_callable=AsyncMock,
            side_effect=_side_effect,
        ),
    ):
        api = ZeoApi(mock_channel)
        await api.start()

    # The SET (non-query) command is the one that carries START.
    set_calls = [params for params, _ in captured if _ID_QUERY_INT not in {int(k) for k in params}]
    assert set_calls, "start() should issue a SET command"
    start_params = set_calls[-1]
    assert start_params[RoborockZeoProtocol.START] == "True"
    # Core numeric params travel as integers, not strings.
    assert start_params[RoborockZeoProtocol.MODE] == 1
    assert start_params[RoborockZeoProtocol.PROGRAM] == 1


@pytest.mark.asyncio
async def test_zeo_start_feature_gated_dps_batched(mock_channel):
    """Feature-gated DPs must be queried in a single batch, not one-by-one."""
    mock_channel.subscribe = AsyncMock(return_value=Mock())

    query_call_count = 0

    async def _side_effect(channel, params, **kwargs):
        nonlocal query_call_count
        if _ID_QUERY_INT in {int(k) for k in params}:
            queried = params[_ID_QUERY_INT]
            if RoborockZeoProtocol.FEATURE_BITS in queried:
                query_call_count += 1
                # Enable both wash_dry_linkage (bit 22) and ion_deodorization (bit 23)
                raw_bits = (1 << 22) | (1 << 23)
                return {int(RoborockZeoProtocol.FEATURE_BITS): raw_bits}
            # Feature-gated batch query — capture it and return empty
            query_call_count += 1
            return {}
        # SET — let through
        query_call_count += 1
        return {}

    # Pre-populate cache with START params so _get_start_params() doesn't
    # issue an extra query that would distort the count.
    with (
        patch("roborock.devices.traits.a01.send_decoded_command", new_callable=AsyncMock, side_effect=_side_effect),
        patch(
            "roborock.devices.traits.a01.device_features.send_decoded_command",
            new_callable=AsyncMock,
            side_effect=_side_effect,
        ),
    ):
        api = ZeoApi(mock_channel)
        # Seed the DPS cache with required START params so only FEATURE_BITS
        # and feature-gated queries are counted.
        api._dps_cache.update(
            {
                int(RoborockZeoProtocol.MODE): 1,
                int(RoborockZeoProtocol.PROGRAM): 1,
                int(RoborockZeoProtocol.TEMP): 30,
                int(RoborockZeoProtocol.RINSE_TIMES): 2,
                int(RoborockZeoProtocol.SPIN_LEVEL): 800,
                int(RoborockZeoProtocol.DRYING_MODE): 1,
            }
        )
        await api.start()

    # FEATURE_BITS query (1) + batch gated-DP query (1) + SET (1) = 3 total calls.
    # If not batched, there would be 1 + 2 + 1 = 4 calls.
    assert query_call_count == 3, f"Expected 3 queries (batched), got {query_call_count}"


def test_zeo_api_close_unsubscribes_from_channel(mock_channel):
    """close() must call the unsubscribe function and clear it."""
    unsub_mock = Mock()
    mock_channel.subscribe.return_value = unsub_mock

    import asyncio

    async def _init():
        api = ZeoApi(mock_channel)
        await api._ensure_subscribed()
        assert api._dps_unsub is not None
        api.close()
        unsub_mock.assert_called_once()
        assert api._dps_unsub is None

    asyncio.run(_init())


def test_zeo_api_close_idempotent(mock_channel):
    """Calling close() multiple times must not fail."""
    mock_channel.subscribe.return_value = Mock()

    import asyncio

    async def _init():
        api = ZeoApi(mock_channel)
        await api._ensure_subscribed()
        api.close()
        api.close()  # Should not raise

    asyncio.run(_init())
