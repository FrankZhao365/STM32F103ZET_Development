"""Intel HEX 解析。

记录格式(每行):
    :LLAAAATTDD...CC
      LL  数据字节数
      AAAA 数据起始地址(大端)
      TT  记录类型: 00 数据 / 01 文件结束 / 02 扩展段地址
                    03 起始段地址 / 04 扩展线性地址 / 05 起始线性地址
      DD  数据
      CC  校验和 = 前面所有字节之和的二进制补码

解析后得到一整块连续数据 + 起始地址,便于直接按地址烧写。
地址空洞用填充字节补齐(默认 0xFF),因为跳过的地址通常是已擦除的 flash。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

log = logging.getLogger(__name__)

REC_DATA = 0x00
REC_EOF = 0x01
REC_EXT_SEGMENT = 0x02
REC_START_SEGMENT = 0x03
REC_EXT_LINEAR = 0x04
REC_START_LINEAR = 0x05

_REC_NAMES = {
    REC_DATA: "数据",
    REC_EOF: "文件结束",
    REC_EXT_SEGMENT: "扩展段地址",
    REC_START_SEGMENT: "起始段地址",
    REC_EXT_LINEAR: "扩展线性地址",
    REC_START_LINEAR: "起始线性地址",
}


class HexFileError(Exception):
    """HEX 文件格式错误。"""


@dataclass
class HexImage:
    """解析结果:一整块待下载数据。"""

    data: bytes
    base_address: int
    start_address: int = 0          # 记录中声明的起始地址(有 START 记录时)
    record_count: int = 0
    data_bytes: int = 0
    gaps: List[Tuple[int, int]] = field(default_factory=list)  # (起始地址, 长度)

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def end_address(self) -> int:
        return self.base_address + len(self.data) - 1

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)

    def summary(self) -> str:
        rows = [
            f"数据长度   {len(self.data):,} 字节 ({len(self.data) / 1024:.1f} KB)",
            f"烧写地址   0x{self.base_address:08X} - 0x{self.end_address:08X}",
            f"记录条数   {self.record_count} 条(数据 {self.data_bytes:,} 字节)",
        ]
        if self.start_address:
            rows.append(f"起始地址   0x{self.start_address:08X}(START 记录)")
        if self.gaps:
            rows.append(f"地址空洞   {len(self.gaps)} 处,已用 0xFF 填充")
        return "\n".join(rows)


def _parse_record(line: str, lineno: int) -> Tuple[int, int, int, bytes]:
    """解析一行,返回 (type, address, data)。"""
    s = line.strip()
    if not s:
        raise HexFileError(f"第 {lineno} 行为空")
    if not s.startswith(":"):
        raise HexFileError(f"第 {lineno} 行缺少起始符 ':'")

    body = s[1:]
    if len(body) % 2 != 0:
        raise HexFileError(f"第 {lineno} 行十六进制字符数为奇数")
    try:
        raw = bytes.fromhex(body)
    except ValueError as exc:
        raise HexFileError(f"第 {lineno} 行含非法十六进制字符: {exc}") from exc

    if len(raw) < 5:
        raise HexFileError(f"第 {lineno} 行长度不足(至少 5 字节)")

    count = raw[0]
    if len(raw) != count + 5:
        raise HexFileError(
            f"第 {lineno} 行长度不符: 声明 {count} 字节数据,实际 {len(raw) - 5} 字节"
        )

    # 校验和 = 前 N+4 个字节之和的二进制补码(不含末尾的校验和自身)
    checksum = (-sum(raw[:-1])) & 0xFF
    if checksum != raw[-1]:
        raise HexFileError(
            f"第 {lineno} 行校验和错误: 应为 0x{checksum:02X},实际 0x{raw[-1]:02X}"
        )

    rec_type = raw[3]
    data = raw[4 : 4 + count]
    address = (raw[1] << 8) | raw[2]
    return rec_type, address, data


def parse_intel_hex(path: str, fill: int = 0xFF) -> HexImage:
    """解析 Intel HEX 文件。

    Args:
        path: 文件路径
        fill: 地址空洞的填充字节,默认 0xFF(已擦除的 flash)
    """
    chunks: Dict[int, bytes] = {}
    base = 0            # 段基址(由 EXT_SEGMENT / EXT_LINEAR 记录设置)
    start_linear = None
    record_count = 0
    data_bytes = 0

    with open(path, "r", encoding="ascii", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            rec_type, address, data = _parse_record(line, lineno)
            record_count += 1

            if rec_type == REC_DATA:
                addr = base + address
                chunks[addr] = data
                data_bytes += len(data)
            elif rec_type == REC_EOF:
                break
            elif rec_type == REC_EXT_SEGMENT:
                if len(data) != 2:
                    raise HexFileError(f"第 {lineno} 行: 扩展段地址记录应为 2 字节")
                base = int.from_bytes(data, "big") << 4
            elif rec_type == REC_EXT_LINEAR:
                if len(data) != 2:
                    raise HexFileError(f"第 {lineno} 行: 扩展线性地址记录应为 2 字节")
                base = int.from_bytes(data, "big") << 16
            elif rec_type == REC_START_LINEAR:
                if len(data) != 4:
                    raise HexFileError(f"第 {lineno} 行: 起始线性地址记录应为 4 字节")
                start_linear = int.from_bytes(data, "big")
            elif rec_type == REC_START_SEGMENT:
                if len(data) != 4:
                    raise HexFileError(f"第 {lineno} 行: 起始段地址记录应为 4 字节")
            else:
                log.warning("第 %d 行: 忽略未知记录类型 0x%02X", lineno, rec_type)

    if not chunks:
        raise HexFileError("文件中没有任何数据记录")

    # EXT 记录可能在数据之后才出现,这里统一按最终基址还原真实地址
    min_addr = min(chunks)
    end_exclusive = max(a + len(d) for a, d in chunks.items())

    out = bytearray([fill & 0xFF]) * (end_exclusive - min_addr)
    gaps: List[Tuple[int, int]] = []
    cursor = min_addr
    for addr in sorted(chunks):
        data = chunks[addr]
        if addr > cursor:
            gaps.append((cursor, addr - cursor))
        end = addr + len(data)
        if end > cursor:
            out[addr - min_addr : end - min_addr] = data
            cursor = max(cursor, end)

    image = HexImage(
        data=bytes(out),
        base_address=min_addr,
        start_address=start_linear or 0,
        record_count=record_count,
        data_bytes=data_bytes,
        gaps=gaps,
    )
    log.info("解析 %s: %d 字节 @ 0x%08X", path, len(out), min_addr)
    return image