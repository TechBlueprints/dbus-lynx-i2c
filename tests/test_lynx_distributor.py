"""Status-byte decoding for the Lynx Distributor."""

from __future__ import annotations

import pytest

from lynx_distributor import ADDRESSES, decode, describe


def test_dip_switch_address_map():
    assert ADDRESSES == {"A": 0x08, "B": 0x09, "C": 0x0A, "D": 0x0B}


def test_all_ok():
    s = decode(0x00)
    assert s.fuses == (False, False, False, False)
    assert s.no_supply is False
    assert s.alarm_active is False
    assert s.blown_fuses == ()
    assert s.unknown_bits == 0


@pytest.mark.parametrize("raw,expected", [
    (0x10, (1,)),
    (0x20, (2,)),
    (0x40, (3,)),
    (0x80, (4,)),
    (0x30, (1, 2)),
    (0xF0, (1, 2, 3, 4)),
])
def test_single_and_multiple_fuse_bits(raw, expected):
    s = decode(raw)
    assert s.blown_fuses == expected
    assert s.alarm_active is True
    assert s.no_supply is False


def test_no_supply_bit():
    s = decode(0x02)
    assert s.no_supply is True
    assert s.blown_fuses == ()
    assert s.alarm_active is True


def test_unpopulated_positions_ignored():
    # Only 2 fuses fitted: positions 3/4 read as "blown" on the wire but
    # must not alarm.
    s = decode(0xC0, num_fuses=2)
    assert s.fuses == (False, False)
    assert s.alarm_active is False
    s = decode(0xD0, num_fuses=2)  # fuse 1 actually blown
    assert s.blown_fuses == (1,)
    assert s.alarm_active is True


def test_zero_fuses_only_supply_monitored():
    s = decode(0xF0, num_fuses=0)
    assert s.fuses == ()
    assert s.alarm_active is False
    assert decode(0x02, num_fuses=0).alarm_active is True


def test_unknown_bits_flagged():
    s = decode(0x0D)  # 0x08 | 0x04 | 0x01 are undocumented
    assert s.unknown_bits == 0x0D
    assert s.alarm_active is False


def test_out_of_range_rejected():
    with pytest.raises(ValueError):
        decode(0x100)
    with pytest.raises(ValueError):
        decode(0x00, num_fuses=5)


def test_describe_strings():
    assert describe(decode(0x00)) == "0x00: all fuses OK"
    assert "fuse 1,2 blown/missing" in describe(decode(0x30))
    assert "busbar has no supply" in describe(decode(0x02))
    assert "unknown bits 0x01" in describe(decode(0x01))
