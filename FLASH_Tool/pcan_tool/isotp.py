"""ISO 15765-2 (ISO-TP) 传输层。

经典 CAN(8 字节帧)下的帧结构:

    SF(单帧)   [0x0N][data...]          N=1..7,载荷 ≤7
    FF(首帧)   [0x10][lenHi][lenLo][0x21][data x4]   载荷 4
    CF(连续帧) [0x2N][data x7]          N=序列号 0..15 循环,载荷 7
    FC(流控)   [0x30][FS][BS][STmin]

12 位长度字段决定单个 ISO-TP 消息最大 4095 字节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional

PCI_SF = 0x00
PCI_FF = 0x10
PCI_CF = 0x20
PCI_FC = 0x30

MAX_PAYLOAD = 0xFFF  # 12 位长度字段上限

# 经典 CAN(8 字节帧)下每帧能带的数据量
FIRST_FRAME_DATA = 6        # 8 - 2 字节 PCI(类型+12 位长度)
CONSECUTIVE_FRAME_DATA = 7  # 8 - 1 字节 PCI(类型+序列号)
FIRST_CONSECUTIVE_SEQ = 1   # 首帧不带序列号,第一个连续帧从 1 开始


class FlowStatus(IntEnum):
    """FC 的 FS 字段。"""

    CONTINUE = 0x00
    WAIT = 0x01
    OVERFLOW = 0x02


class IsoTpError(Exception):
    """ISO-TP 协议错误(收到非法帧或对端中止传输)。"""


class IsoTpOverflow(IsoTpError):
    """对端回了 Overflow,要求中止传输。"""


class IsoTpTimeout(IsoTpError):
    """等待 FC 或对端响应超时。"""


# --------------------------------------------------------------- 发送方向


def build_single_frame(payload: bytes) -> bytes:
    """构造单帧。载荷超过 7 字节时应改用首帧 + 连续帧。"""
    if not 1 <= len(payload) <= 7:
        raise IsoTpError(f"单帧载荷必须是 1..7 字节,实际 {len(payload)}")
    return bytes([PCI_SF | len(payload)]) + payload


def build_first_frame(payload: bytes) -> bytes:
    """构造首帧。

    PCI 只有 2 字节:byte0 = 0x1|长度高 4 位,byte1 = 长度低 8 位。
    首帧**不带序列号**,数据从 byte2 开始,经典 CAN 下可带 6 字节。
    """
    if len(payload) < 8:
        raise IsoTpError("多帧传输的载荷至少 8 字节")
    if len(payload) > MAX_PAYLOAD:
        raise IsoTpError(f"载荷 {len(payload)} 字节超过 ISO-TP 上限 {MAX_PAYLOAD}")
    header = bytes([PCI_FF | ((len(payload) >> 8) & 0x0F), len(payload) & 0xFF])
    return header + payload[:FIRST_FRAME_DATA]


def build_consecutive_frame(payload: bytes, seq: int) -> bytes:
    """构造连续帧。seq 取值 0..15 循环。"""
    if len(payload) > CONSECUTIVE_FRAME_DATA:
        raise IsoTpError(
            f"连续帧载荷最多 {CONSECUTIVE_FRAME_DATA} 字节,实际 {len(payload)}"
        )
    return bytes([PCI_CF | (seq & 0x0F)]) + payload


def build_flow_control(fs: FlowStatus = FlowStatus.CONTINUE, block_size: int = 0,
                       st_min: int = 0) -> bytes:
    """构造流控帧(接收方使用)。

    PCI 共 3 字节:byte0 = 0x3|FS,byte1 = BS,byte2 = STmin。
    """
    return bytes([PCI_FC | (int(fs) & 0x0F), block_size & 0xFF, st_min & 0xFF])


def needs_multi_frame(payload: bytes) -> bool:
    """判断载荷是否必须走多帧。"""
    return len(payload) > 7


@dataclass
class FlowControl:
    """对端发来的流控参数。"""

    status: FlowStatus = FlowStatus.CONTINUE
    block_size: int = 0     # 每多少个连续帧要再等一次 FC;0 = 不限
    st_min_ms: float = 0.0  # 连续帧之间的最小间隔(已换算成毫秒)


def parse_flow_control(frame: bytes) -> FlowControl:
    """解析 FC 帧。STmin 字段 0x00-0x7F 表示毫秒,0xF1-0xF9 表示 100-900 微秒。"""
    if len(frame) < 3 or (frame[0] & 0xF0) != PCI_FC:
        raise IsoTpError(f"不是有效的流控帧: {frame.hex(' ')}")

    raw_status = frame[0] & 0x0F
    if raw_status > 0x02:
        raise IsoTpError(f"非法 FS 值 0x{raw_status:02X}")
    status = FlowStatus(raw_status)

    raw_st = frame[2]
    if raw_st <= 0x7F:
        st_ms = float(raw_st)
    elif 0xF1 <= raw_st <= 0xF9:
        st_ms = (raw_st - 0xF0) / 10.0  # 100..900 微秒
    else:
        st_ms = 127.0  # 保留值按上限处理

    return FlowControl(status=status, block_size=frame[1], st_min_ms=st_ms)


# --------------------------------------------------------------- 接收方向


@dataclass
class IsoTpReassembler:
    """把收到的 CAN 帧拼回完整 ISO-TP 消息。

    只处理"发送方向"用到的部分:收到 FF 后必须先有 FC,再收 CF。
    """

    expected: Optional[int] = None
    buffer: bytearray = field(default_factory=bytearray)
    next_seq: int = 0
    active: bool = False
    completed: List[bytes] = field(default_factory=list)

    def start(self) -> None:
        self.expected = None
        self.buffer.clear()
        self.next_seq = 0
        self.active = False

    def feed(self, frame: bytes) -> Optional[bytes]:
        """喂入一帧。

        返回值:
            None    —— 数据还不够,继续收
            bytes   —— 拼好的完整消息
        """
        if not frame:
            return None
        pci_type = frame[0] & 0xF0

        if pci_type == PCI_SF:
            length = frame[0] & 0x0F
            if length == 0 or len(frame) < 1 + length:
                raise IsoTpError(f"单帧长度非法: {frame.hex(' ')}")
            self.start()
            return bytes(frame[1 : 1 + length])

        if pci_type == PCI_FF:
            if len(frame) < 2:
                raise IsoTpError(f"首帧长度不足: {frame.hex(' ')}")
            total = ((frame[0] & 0x0F) << 8) | frame[1]
            if total == 0:
                raise IsoTpError("首帧长度字段为 0(本实现不支持 >4095 的转义格式)")
            if total > MAX_PAYLOAD:
                raise IsoTpError(f"首帧声明长度 {total} 超过上限 {MAX_PAYLOAD}")

            self.start()
            self.active = True
            self.expected = total
            self.buffer.extend(frame[2:])
            self.next_seq = FIRST_CONSECUTIVE_SEQ

            if len(self.buffer) >= self.expected:
                return self._finish()
            return None

        if pci_type == PCI_CF:
            if not self.active:
                raise IsoTpError("收到连续帧,但此前没有首帧")
            seq = frame[0] & 0x0F
            if seq != self.next_seq:
                raise IsoTpError(
                    f"连续帧序列号错误: 期望 {self.next_seq},收到 {seq}"
                )
            self.buffer.extend(frame[1:])
            self.next_seq = (self.next_seq + 1) & 0x0F
            if len(self.buffer) >= self.expected:
                return self._finish()
            return None

        if pci_type == PCI_FC:
            # 流控帧不参与重组,交给上层处理
            return None

        raise IsoTpError(f"未知的 PCI 类型 0x{frame[0]:02X}")

    def _finish(self) -> bytes:
        payload = bytes(self.buffer[: self.expected])
        self.start()
        return payload


def split_frames(payload: bytes) -> List[bytes]:
    """把完整载荷拆成可直接发出的 CAN 帧序列(不含流控)。"""
    if not payload:
        raise IsoTpError("载荷不能为空")
    if len(payload) <= 7:
        return [build_single_frame(payload)]

    frames = [build_first_frame(payload)]
    rest = payload[FIRST_FRAME_DATA:]
    seq = FIRST_CONSECUTIVE_SEQ
    for i in range(0, len(rest), CONSECUTIVE_FRAME_DATA):
        frames.append(
            build_consecutive_frame(rest[i : i + CONSECUTIVE_FRAME_DATA], seq)
        )
        seq = (seq + 1) & 0x0F
    return frames