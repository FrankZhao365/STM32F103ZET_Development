"""LIN 协议核心:帧格式、受保护 ID(Protected ID)与校验和。

帧结构(ISO 17987 / LIN 2.x)
----------------------------
    帧头(主节点发出):BREAK(≥11 个位时间的低电平) + SYNC(0x55) + 受保护 ID
    帧响应(从节点发出):数据字节(0~8 个) + 校验和字节

受保护 ID
---------
帧头里的 ID 字节 = 2 个奇偶校验位 + 6 位帧 ID:

    P0 = ID0 ⊕ ID1 ⊕ ID2 ⊕ ID4
    P1 = ¬(ID1 ⊕ ID3 ⊕ ID4 ⊕ ID5)
    受保护 ID = (P1 << 7) | (P0 << 6) | (ID & 0x3F)

这套定义与上游 Linux LIN 驱动(lin-bus/linux-lin, sllin.c)里的官方
奇偶校验表逐项一致,本模块用两种算法互相校验,见 tests/test_protocol.py。

校验和
------
    经典校验和(LIN 1.3)   : 只累加数据字节
    增强校验和(LIN 2.0+)  : 还要累加受保护 ID 字节
    累加时进位回卷(每加一个字节后 >255 就减 255),最后按位取反。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, Sequence

# ---------------------------------------------------------------- 基本常量

SYNC_BYTE = 0x55
"""同步字节,固定值 0x55。"""

BREAK_MIN_BITS = 11
"""BREAK 域最短位时间(规范要求 ≥11,工程上常用 13)。"""

MAX_DATA_BYTES = 8
"""一帧最多 8 个数据字节。"""

FRAME_ID_MASK = 0x3F
"""帧 ID 为 6 位。"""

# 特殊帧 ID
DIAG_MASTER_REQUEST = 0x3C
"""主节点请求帧(诊断)。"""

DIAG_SLAVE_RESPONSE = 0x3D
"""从节点应答帧(诊断)。"""

USER_DEFINED_FRAME = 0x3E
"""用户自定义帧。"""

RESERVED_FRAME = 0x3F
"""保留帧。"""

SPECIAL_FRAME_NAMES = {
    DIAG_MASTER_REQUEST: "主节点请求(诊断)",
    DIAG_SLAVE_RESPONSE: "从节点应答(诊断)",
    USER_DEFINED_FRAME: "用户自定义帧",
    RESERVED_FRAME: "保留帧",
}

#: 上游 Linux LIN 驱动(sllin.c)中的官方受保护 ID 奇偶校验表。
#: 下标是 6 位帧 ID,值是奇偶校验位(位于字节的 bit7 / bit6)。
ID_PARITY_TABLE: Sequence[int] = (
    0x80, 0xC0, 0x40, 0x00, 0xC0, 0x80, 0x00, 0x40,
    0x00, 0x40, 0xC0, 0x80, 0x40, 0x00, 0x80, 0xC0,
    0x40, 0x00, 0x80, 0xC0, 0x00, 0x40, 0xC0, 0x80,
    0xC0, 0x80, 0x00, 0x40, 0x80, 0xC0, 0x40, 0x00,
    0x00, 0x40, 0xC0, 0x80, 0x40, 0x00, 0x80, 0xC0,
    0x80, 0xC0, 0x40, 0x00, 0xC0, 0x80, 0x00, 0x40,
    0xC0, 0x80, 0x00, 0x40, 0x80, 0xC0, 0x40, 0x00,
    0x40, 0x00, 0x80, 0xC0, 0x00, 0x40, 0xC0, 0x80,
)


class ChecksumType(enum.Enum):
    """校验和类型。"""

    CLASSIC = "classic"
    """经典校验和:只累加数据字节(LIN 1.3 行为)。"""

    ENHANCED = "enhanced"
    """增强校验和:额外累加受保护 ID(LIN 2.0+ 行为)。"""

    @property
    def label(self) -> str:
        return "经典" if self is ChecksumType.CLASSIC else "增强"


# ---------------------------------------------------------------- 受保护 ID


def protected_id(frame_id: int) -> int:
    """计算帧 ID 对应的受保护 ID 字节。"""
    fid = int(frame_id) & FRAME_ID_MASK
    b = [(fid >> i) & 1 for i in range(6)]
    p0 = b[0] ^ b[1] ^ b[2] ^ b[4]
    p1 = (~(b[1] ^ b[3] ^ b[4] ^ b[5])) & 1
    return (p1 << 7) | (p0 << 6) | fid


def parity_bits(frame_id: int) -> int:
    """只返回受保护 ID 里的两个奇偶校验位(bit7/bit6)。"""
    return protected_id(frame_id) & 0xC0


def frame_id_of(protected: int) -> int:
    """从受保护 ID 字节还原 6 位帧 ID。"""
    return int(protected) & FRAME_ID_MASK


def is_valid_protected_id(protected: int) -> bool:
    """校验受保护 ID 字节的奇偶位是否正确。"""
    value = int(protected) & 0xFF
    return protected_id(value & FRAME_ID_MASK) == value


# ---------------------------------------------------------------- 校验和


def checksum(
    data: Sequence[int],
    protected: Optional[int] = None,
    checksum_type: ChecksumType = ChecksumType.CLASSIC,
) -> int:
    """计算 LIN 帧的校验和字节。

    `data` 是数据字节(不含校验和本身);
    `protected` 只有在增强校验和时才参与计算。
    """
    total = 0
    if checksum_type is ChecksumType.ENHANCED and protected is not None:
        total += int(protected) & 0xFF
        if total > 255:
            total -= 255
    for byte in data:
        total += int(byte) & 0xFF
        if total > 255:
            total -= 255
    return (~total) & 0xFF


def verify_checksum(
    data: Sequence[int],
    received: int,
    protected: Optional[int] = None,
    checksum_type: ChecksumType = ChecksumType.CLASSIC,
) -> bool:
    """校验收到的校验和字节是否正确。"""
    return checksum(data, protected, checksum_type) == (int(received) & 0xFF)


# ---------------------------------------------------------------- 帧模型


@dataclass
class LinFrame:
    """一帧 LIN 报文。"""

    timestamp: float
    """时间戳(秒,Unix 时间)。"""

    frame_id: int
    """6 位帧 ID。"""

    data: bytes = b""
    """数据字节(不含校验和)。"""

    direction: str = "RX"
    """方向:"RX" 收到的,"TX" 本机发出的。"""

    checksum: int = 0
    """收到的校验和字节。"""

    checksum_valid: bool = True
    """校验和是否正确。"""

    checksum_type: ChecksumType = ChecksumType.CLASSIC
    """使用的校验和类型。"""

    name: str = ""
    """帧名(来自 LDF;没有 LDF 时留空)。"""

    note: str = ""
    """备注(错误原因等)。"""

    protected_id: int = 0
    """受保护 ID 字节。"""

    @property
    def dlc(self) -> int:
        """数据长度。"""
        return len(self.data)

    @property
    def display_name(self) -> str:
        """帧名,没有 LDF 名称时用特殊帧 ID 的通用名。"""
        if self.name:
            return self.name
        return SPECIAL_FRAME_NAMES.get(self.frame_id, "")

    @property
    def is_error(self) -> bool:
        """是否为错误帧(校验和错或超时)。"""
        return not self.checksum_valid or bool(self.note)


@dataclass
class FrameSpec:
    """一个被轮询/监听的帧的描述(来自 LDF 或界面手工配置)。"""

    frame_id: int
    """6 位帧 ID。"""

    length: int
    """数据字节数。"""

    name: str = ""
    """帧名。"""

    checksum_type: ChecksumType = ChecksumType.ENHANCED
    """校验和类型。"""

    delay_ms: float = 10.0
    """该帧在调度表中的槽位时间(ms)。"""

    def __post_init__(self) -> None:
        self.frame_id = int(self.frame_id) & FRAME_ID_MASK
        self.length = max(0, min(int(self.length), MAX_DATA_BYTES))


def format_data(data: Sequence[int]) -> str:
    """把数据格式化成 `01 02 03`。"""
    return " ".join(f"{b & 0xFF:02X}" for b in data)


def describe_frame_id(frame_id: int) -> str:
    """帧 ID 的文字说明(用于没有 LDF 时的提示)。"""
    name = SPECIAL_FRAME_NAMES.get(frame_id)
    if name:
        return name
    return f"无条件帧 0x{frame_id:02X}"
