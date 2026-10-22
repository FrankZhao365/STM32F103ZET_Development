"""界面输入解析与格式化辅助。"""

from __future__ import annotations

import re
from typing import List

_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_SEP_RE = re.compile(r"[\s,;_\-]+")

MAX_STD_ID = 0x7FF
MAX_EXT_ID = 0x1FFFFFFF
MAX_CLASSIC_DLC = 8


def parse_can_id(text: str, extended: bool = False) -> List[int]:
    """解析 CAN ID 列表。支持 `1A2`、`0x1A2` 两种写法。

    允许一次粘贴多个 ID(用逗号/分号/空格分隔),**总是返回列表**:
    输入单个 ID 时返回长度为 1 的列表,便于调用方统一遍历。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("CAN ID 不能为空")

    values: List[int] = []
    for token in _SEP_RE.split(raw):
        if not token:
            continue
        token = token.lower().removeprefix("0x")
        if not _HEX_RE.match(token):
            raise ValueError(f"非法的 CAN ID: {token!r}(只接受十六进制)")
        values.append(int(token, 16))

    if not values:
        raise ValueError("CAN ID 不能为空")

    limit = MAX_EXT_ID if extended else MAX_STD_ID
    for v in values:
        if v > limit:
            raise ValueError(
                f"CAN ID 0x{v:X} 超出范围 (标准帧最大 0x{MAX_STD_ID:X},扩展帧最大 0x{MAX_EXT_ID:X})"
            )
    return values


def parse_data(text: str, max_len: int = MAX_CLASSIC_DLC) -> bytes:
    """解析数据段。支持 `01 02`、`0102`、`01,02` 多种写法。"""
    raw = (text or "").strip()
    if not raw:
        return b""

    tokens = [t for t in _SEP_RE.split(raw) if t]
    # 支持连续写法 "0102" -> 01 02
    if len(tokens) == 1 and len(tokens[0]) > 2:
        tokens = [tokens[0][i : i + 2] for i in range(0, len(tokens[0]), 2)]

    out = bytearray()
    for token in tokens:
        t = token.lower().removeprefix("0x")
        if not _HEX_RE.match(t):
            raise ValueError(f"非法的数据字节: {token!r}(只接受十六进制)")
        if len(t) > 2:
            raise ValueError(f"数据字节必须是 2 位十六进制: {token!r}")
        out.append(int(t, 16))

    if len(out) > max_len:
        raise ValueError(f"数据长度 {len(out)} 超过上限 {max_len} 字节")
    return bytes(out)


def format_data(data: bytes) -> str:
    """把数据格式化成 `01 02 03`。"""
    return " ".join(f"{b:02X}" for b in data)


def parse_int(text: str, default: int, lo: int, hi: int) -> int:
    """解析整数并夹到 [lo, hi]。空输入用 default。"""
    raw = (text or "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw, 0)
        except ValueError:
            raise ValueError(f"不是合法的整数: {raw!r}")
    return max(lo, min(hi, value))