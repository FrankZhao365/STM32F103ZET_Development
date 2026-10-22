"""信号编解码。

LIN 的位序:帧内字节按序号递增,每个字节内低位在前(LSB first),
所以"第 N 位"= 第 N//8 个字节的第 N%8 位。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .ldf import LdfFrame, LdfSignal


@dataclass
class DecodedSignal:
    """一个解码出来的信号。"""

    name: str
    raw: int
    physical: float
    text: str = ""
    unit: str = ""
    bit_offset: int = 0
    size: int = 0

    @property
    def value_text(self) -> str:
        """显示用的物理值文本。"""
        if self.physical == int(self.physical):
            value = str(int(self.physical))
        else:
            value = f"{self.physical:g}"
        if self.unit:
            value = f"{value} {self.unit}"
        if self.text:
            value = f"{value} ({self.text})"
        return value


def extract_bits(data: Sequence[int], bit_offset: int, size: int) -> int:
    """从帧数据里取出指定起始位、指定长度的位串。"""
    value = 0
    for i in range(max(0, size)):
        index = bit_offset + i
        byte_index = index // 8
        if byte_index >= len(data):
            break
        bit = (int(data[byte_index]) >> (index % 8)) & 1
        value |= bit << i
    return value


def insert_bits(buf: bytearray, bit_offset: int, size: int, value: int) -> None:
    """把位串写进帧数据(与 `extract_bits` 互为逆操作)。"""
    for i in range(max(0, size)):
        index = bit_offset + i
        byte_index = index // 8
        while len(buf) <= byte_index:
            buf.append(0)
        bit = (int(value) >> i) & 1
        if bit:
            buf[byte_index] |= 1 << (index % 8)
        else:
            buf[byte_index] &= ~(1 << (index % 8)) & 0xFF


def decode_frame(frame: LdfFrame, data: Sequence[int]) -> List[DecodedSignal]:
    """按 LDF 定义解码一帧里的所有信号。"""
    out: List[DecodedSignal] = []
    for sig in frame.signals:
        raw = extract_bits(data, sig.bit_offset, sig.size)
        out.append(
            DecodedSignal(
                name=sig.name,
                raw=raw,
                physical=sig.physical(raw),
                text=sig.text(raw),
                unit=sig.unit,
                bit_offset=sig.bit_offset,
                size=sig.size,
            )
        )
    out.sort(key=lambda s: s.bit_offset)
    return out


def encode_frame(frame: LdfFrame, values: Dict[str, float]) -> bytes:
    """按 LDF 定义把物理值编码成帧数据。"""
    buf = bytearray(frame.size)
    for sig in frame.signals:
        if sig.name not in values:
            continue
        physical = float(values[sig.name])
        raw = int(round((physical - sig.scale) / sig.factor)) if sig.factor else 0
        raw = max(0, min(raw, (1 << sig.size) - 1)) if sig.size else 0
        insert_bits(buf, sig.bit_offset, sig.size, raw)
    return bytes(buf)


def signal_lookup(signal: LdfSignal, raw: int) -> str:
    """查逻辑值说明。"""
    return signal.logical_values.get(int(raw), "")
