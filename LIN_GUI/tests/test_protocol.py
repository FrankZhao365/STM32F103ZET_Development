"""协议层测试:受保护 ID 与校验和。

受保护 ID 的期望值直接取自上游 Linux LIN 驱动(lin-bus/linux-lin, sllin.c)
里的官方奇偶校验表,这是最权威的一份交叉验证。
"""

from __future__ import annotations

import random

import pytest

from lin_tool.protocol import (
    DIAG_MASTER_REQUEST,
    DIAG_SLAVE_RESPONSE,
    ID_PARITY_TABLE,
    SYNC_BYTE,
    ChecksumType,
    FrameSpec,
    checksum,
    frame_id_of,
    is_valid_protected_id,
    protected_id,
    verify_checksum,
)


def _upstream_checksum(data, protected=None, enhanced=False):
    """照抄上游驱动 sllin_checksum() 的算法,作为独立实现交叉验证。"""
    csum = 0
    items = list(data)
    if enhanced and protected is not None:
        items = [protected] + items
    for byte in items:
        csum += byte
        if csum > 255:
            csum -= 255
    return ~csum & 0xFF


# ---------------------------------------------------------------- 受保护 ID


def test_protected_id_matches_official_table():
    """64 个帧 ID 的受保护 ID 必须与官方奇偶校验表完全一致。"""
    for frame_id in range(64):
        expected = ID_PARITY_TABLE[frame_id] | frame_id
        assert protected_id(frame_id) == expected, f"帧 ID 0x{frame_id:02X}"


def test_protected_id_round_trip():
    for frame_id in range(64):
        pid = protected_id(frame_id)
        assert frame_id_of(pid) == frame_id
        assert is_valid_protected_id(pid)


def test_protected_id_rejects_wrong_parity():
    pid = protected_id(0x11)
    assert not is_valid_protected_id(pid ^ 0x40)
    assert not is_valid_protected_id(pid ^ 0x80)


def test_diagnostic_frame_ids():
    """诊断帧:主节点请求 0x3C 的受保护 ID 就是 0x3C(奇偶位全 0)。"""
    assert protected_id(DIAG_MASTER_REQUEST) == 0x3C
    assert protected_id(DIAG_SLAVE_RESPONSE) == 0x7D


def test_sync_byte():
    assert SYNC_BYTE == 0x55


# ---------------------------------------------------------------- 校验和


def test_classic_checksum_vectors():
    assert checksum([0x00]) == 0xFF
    assert checksum([0x01]) == 0xFE
    assert checksum([0xFF, 0xFF]) == 0x00
    assert checksum([0x01, 0x02, 0x03]) == 0xF9


def test_enhanced_checksum_uses_protected_id():
    pid = protected_id(0x11)
    data = [0x01, 0x02]
    classic = checksum(data, pid, ChecksumType.CLASSIC)
    enhanced = checksum(data, pid, ChecksumType.ENHANCED)
    assert classic != enhanced
    assert enhanced == _upstream_checksum(data, pid, True)
    assert classic == _upstream_checksum(data, pid, False)


def test_checksum_matches_upstream_on_random_data():
    rng = random.Random(20261022)
    for _ in range(200):
        length = rng.randint(0, 8)
        data = [rng.randint(0, 255) for _ in range(length)]
        frame_id = rng.randint(0, 63)
        pid = protected_id(frame_id)
        assert checksum(data, pid, ChecksumType.CLASSIC) == _upstream_checksum(data)
        assert checksum(data, pid, ChecksumType.ENHANCED) == _upstream_checksum(
            data, pid, True
        )


def test_verify_checksum():
    data = [0x10, 0x20, 0x30]
    pid = protected_id(0x12)
    cks = checksum(data, pid, ChecksumType.ENHANCED)
    assert verify_checksum(data, cks, pid, ChecksumType.ENHANCED)
    assert not verify_checksum(data, cks ^ 0xFF, pid, ChecksumType.ENHANCED)
    assert not verify_checksum(data, cks, pid, ChecksumType.CLASSIC)


# ---------------------------------------------------------------- 其它


def test_frame_spec_clamps_values():
    spec = FrameSpec(0x1FF, 99, delay_ms=-5)
    assert spec.frame_id == 0x3F
    assert spec.length == 8
    assert spec.delay_ms == -5  # 周期交给上层兜底


@pytest.mark.parametrize("frame_id", [0x00, 0x11, 0x3C, 0x3F])
def test_frame_spec_round_trip(frame_id):
    assert FrameSpec(frame_id, 4).frame_id == frame_id
