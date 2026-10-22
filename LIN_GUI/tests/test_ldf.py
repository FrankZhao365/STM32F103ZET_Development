"""LDF 解析测试:分节语法(LDF 2.1)与结构化语法(LDF 2.2)。"""

from __future__ import annotations

import os

import pytest

from lin_tool.ldf import LdfError, load_ldf, parse_ldf
from lin_tool.protocol import ChecksumType

LDF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ldf")


@pytest.fixture(scope="module")
def db_21():
    return load_ldf(os.path.join(LDF_DIR, "example_21.ldf"))


@pytest.fixture(scope="module")
def db_22():
    return load_ldf(os.path.join(LDF_DIR, "example_22.ldf"))


# ---------------------------------------------------------------- LDF 2.1


def test_21_description(db_21):
    assert db_21.version == "2.1"
    assert db_21.protocol_version == "2.1"
    assert db_21.master == "Master"
    assert db_21.baudrate == 19200


def test_21_nodes(db_21):
    assert "Master" in db_21.nodes
    assert "Motor_1" in db_21.nodes
    assert db_21.nodes["Motor_1"]["configured_nad"] == "0x01"


def test_21_signals(db_21):
    sig = db_21.signals["MotorSpeed"]
    assert sig.size == 8
    assert sig.publisher == "Master"
    assert sig.subscribers == ["Motor_1"]


def test_21_frames(db_21):
    frame = db_21.frame_by_name("MotorControl")
    assert frame is not None
    assert frame.frame_id == 0x11
    assert frame.size == 2
    assert frame.publisher == "Master"
    assert frame.subscribers == ["Motor_1"]
    assert [s.name for s in frame.signals] == ["MotorSpeed", "ErrorFlag"]


def test_21_signal_offsets(db_21):
    frame = db_21.frame_by_name("MotorControl")
    offsets = {s.name: s.bit_offset for s in frame.signals}
    assert offsets == {"MotorSpeed": 0, "ErrorFlag": 8}


def test_21_schedule(db_21):
    assert "MainTable" in db_21.schedules
    entries = db_21.schedules["MainTable"].entries
    assert [(e.frame_name, e.delay_ms) for e in entries] == [
        ("MotorControl", 10.0),
        ("MotorStatus", 10.0),
    ]


def test_21_schedule_specs(db_21):
    specs = db_21.schedule_specs("MainTable")
    assert [s.frame_id for s in specs] == [0x11, 0x12]
    assert [s.length for s in specs] == [2, 2]
    assert all(s.checksum_type is ChecksumType.ENHANCED for s in specs)


def test_21_encodings(db_21):
    flag = db_21.signals["ErrorFlag"]
    assert flag.logical_values == {0: "无故障", 1: "故障"}

    speed = db_21.signals["MotorSpeed"]
    assert speed.factor == pytest.approx(0.4)
    assert speed.unit == "rpm"


# ---------------------------------------------------------------- LDF 2.2


def test_22_description(db_22):
    assert db_22.protocol_version == "2.2"
    assert db_22.baudrate == 19200
    assert db_22.master == "CEM"
    assert db_22.slaves == ["LSM", "RSM"]


def test_22_frames(db_22):
    frame = db_22.frame_by_name("CEM_Frm1")
    assert frame is not None
    assert frame.frame_id == 0x01
    assert frame.publisher == "CEM"
    assert frame.size == 1
    assert [s.name for s in frame.signals] == ["InternalLightsRequest"]
    assert frame.signals[0].bit_offset == 0


def test_22_frame_lookup_by_id(db_22):
    frame = db_22.frame_by_id(0x03)
    assert frame is not None
    assert frame.name == "LSM_Frm2"
    assert len(frame.signals) == 2


def test_22_event_triggered_frame(db_22):
    frame = db_22.frame_by_name("Node_Status_Event")
    assert frame is not None
    assert frame.event_triggered
    assert frame.frame_id == 0x06
    assert frame.associated_frames == ["RSM_Frm1", "LSM_Frm1"]


def test_22_schedule(db_22):
    assert "Normal_Schedule" in db_22.schedules
    entries = db_22.schedules["Normal_Schedule"].entries
    assert [e.frame_name for e in entries] == ["CEM_Frm1", "LSM_Frm2", "RSM_Frm2"]
    assert entries[0].delay_ms == 15.0


def test_22_encodings(db_22):
    lights = db_22.signals["InternalLightsRequest"]
    assert lights.logical_values[1] == "on"
    assert lights.logical_values[2] == "error"

    switch = db_22.signals["RightIntLightsSwitch"]
    assert switch.unit == "lux"
    assert switch.scale == pytest.approx(100.0)


def test_22_node_attributes(db_22):
    assert db_22.nodes["LSM"]["configured_nad"] == "0x21"
    assert db_22.nodes["RSM"]["p2_min"] == "150 ms"


# ---------------------------------------------------------------- 其它


def test_all_specs_skips_event_triggered(db_22):
    ids = [spec.frame_id for spec in db_22.all_specs()]
    assert 0x06 not in ids
    assert 0x01 in ids


def test_empty_ldf_raises():
    with pytest.raises(LdfError):
        parse_ldf("   ")


def test_missing_file_raises():
    with pytest.raises(LdfError):
        load_ldf(os.path.join(LDF_DIR, "不存在的文件.ldf"))
