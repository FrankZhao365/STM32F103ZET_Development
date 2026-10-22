"""PCAN 设备枚举。

只加载 PCAN 后端,避免 `can.detect_available_configs()` 导入全部后端时刷屏。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)

# 经典 CAN 波特率,供界面下拉框使用
BITRATES: List[int] = [
    10000, 20000, 50000, 83300, 100000,
    125000, 250000, 500000, 800000, 1000000,
]

# 通道条件 (PCAN_CHANNEL_CONDITION)
CONDITION_TEXT = {
    0x00: "未知",
    0x01: "未初始化",
    0x02: "初始化完成",
    0x03: "已连接",
    0x04: "总线关闭 (bus-off)",
    0x05: "总线被动 (bus passive)",
    0x06: "总线警告 (bus warning)",
    0x07: "错误被动 (error passive)",
    0x08: "设备被占用",
    0x09: "设备不可用",
}


@dataclass
class PcanDevice:
    """一个已枚举到的 PCAN 通道。"""

    channel: str
    device_name: str = "未知设备"
    device_id: int = 0
    supports_fd: bool = False
    device_features: int = 0
    channel_condition: int = 0
    controller_number: int = 0

    @property
    def condition_text(self) -> str:
        return CONDITION_TEXT.get(self.channel_condition, f"0x{self.channel_condition:02X}")

    @property
    def display(self) -> str:
        fd = " / FD" if self.supports_fd else ""
        return f"{self.channel} - {self.device_name} (ID {self.device_id}{fd}) - {self.condition_text}"

    def describe(self) -> str:
        return (
            f"通道 {self.channel}\n"
            f"设备名称   {self.device_name}\n"
            f"设备 ID    {self.device_id}\n"
            f"CAN FD     {'支持' if self.supports_fd else '不支持'}\n"
            f"通道状态   {self.condition_text}"
        )


def list_pcan_devices() -> List[PcanDevice]:
    """枚举当前连接的 PCAN 通道。

    优先走 PCAN 后端自带的探测函数(只加载 PCAN,输出干净);
    若该私有 API 不可用则回退到 python-can 的通用探测。
    """
    from can.interfaces.pcan.pcan import PcanBus

    raw: List[dict] = []
    detect = getattr(PcanBus, "_detect_available_configs", None)
    if callable(detect):
        try:
            raw = [c for c in detect() if c.get("interface") == "pcan"]
        except Exception:
            log.exception("PCAN 后端探测失败,尝试通用探测")
            raw = []

    if not raw:
        try:
            import can

            raw = [c for c in can.detect_available_configs() if c.get("interface") == "pcan"]
        except Exception:
            log.exception("通用 CAN 探测失败")
            raw = []

    devices = [
        PcanDevice(
            channel=str(cfg.get("channel", "")),
            device_name=str(cfg.get("device_name", "") or "PCAN"),
            device_id=int(cfg.get("device_id", 0) or 0),
            supports_fd=bool(cfg.get("supports_fd", False)),
            device_features=int(cfg.get("device_features", 0) or 0),
            channel_condition=int(cfg.get("channel_condition", 0) or 0),
            controller_number=int(cfg.get("controller_number", 0) or 0),
        )
        for cfg in raw
        if cfg.get("channel")
    ]
    devices.sort(key=lambda d: d.channel)
    return devices


def find_device(devices: List[PcanDevice], channel: str) -> Optional[PcanDevice]:
    """按通道名查找设备。"""
    for dev in devices:
        if dev.channel == channel:
            return dev
    return None