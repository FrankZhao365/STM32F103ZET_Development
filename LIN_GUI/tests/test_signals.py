"""信号编解码测试。"""

from __future__ import annotations

import os

import pytest

from lin_tool.ldf import load_ldf
from lin_tool.signals import decode_frame, encode_frame, extract_bits, insert_bits

LDF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ldf")


@pytest.fixture(scope="module")
def db():
    return load_ldf(os.path.join(LDF_DIR, "example_21.ldf"))


def test_extract_bits_lsb_first():
    data = bytes([0b1010_0101, 0b0000_0011])
    assert extract_bits(data, 0, 8) == 0xA5
    assert extract_bits(data, 8, 8) == 0x03
    # 0xA5 = 1010_0101,最低位在前:bit0=1, bit1=0, bit2=1
    assert extract_bits(data, 0, 1) == 1
    assert extract_bits(data, 1, 1) == 0
    assert extract_bits(data, 2, 1) == 1


def test_extract_bits_spanning_bytes():
    data = bytes([0xFF, 0x00])
    assert extract_bits(data, 4, 8) == 0x0F


def test_insert_extract_round_trip():
    buf = bytearray(4)
    insert_bits(buf, 0, 8, 0xA5)
    insert_bits(buf, 8, 8, 0x3C)
    insert_bits(buf, 16, 5, 0x15)
    assert extract_bits(buf, 0, 8) == 0xA5
    assert extract_bits(buf, 8, 8) == 0x3C
    assert extract_bits(buf, 16, 5) == 0x15


def test_insert_bits_grows_buffer():
    buf = bytearray()
    insert_bits(buf, 8, 8, 0xFF)
    assert len(buf) == 2
    assert buf[1] == 0xFF


def test_decode_frame(db):
    frame = db.frame_by_name("MotorControl")
    # MotorSpeed = 0x64 (100), ErrorFlag = 1
    data = bytes([0x64, 0x01])
    signals = decode_frame(frame, data)
    by_name = {s.name: s for s in signals}

    assert by_name["MotorSpeed"].raw == 100
    assert by_name["MotorSpeed"].physical == pytest.approx(40.0)  # 100 * 0.4
    assert by_name["MotorSpeed"].unit == "rpm"
    assert by_name["ErrorFlag"].raw == 1
    assert by_name["ErrorFlag"].text == "故障"


def test_encode_frame_round_trip(db):
    frame = db.frame_by_name("MotorControl")
    data = encode_frame(frame, {"MotorSpeed": 40.0, "ErrorFlag": 1.0})
    signals = {s.name: s for s in decode_frame(frame, data)}
    assert signals["MotorSpeed"].physical == pytest.approx(40.0)
    assert signals["ErrorFlag"].raw == 1


def test_encode_frame_uses_signal_size(db):
    frame = db.frame_by_name("MotorControl")
    data = encode_frame(frame, {"ErrorFlag": 1.0})
    # 超出位宽的值会被截断到 1 位
    assert data[1] & 0x01 == 0x01
