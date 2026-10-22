"""串口枚举。

LIN 上位机一般通过 USB 转串口 + LIN 收发器(或 USB-LIN 适配器)接入,
所以这里只需要把串口列出来,并对常见的适配器芯片给出提示。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

log = logging.getLogger(__name__)

#: 常见 LIN 总线波特率
BAUDRATES: List[int] = [9600, 19200, 10400, 4800, 2400, 20000]

#: 常见的 USB 转串口芯片,用于在界面上给点提示
KNOWN_CHIPS = {
    "CH340": "CH340 串口芯片",
    "CH341": "CH341 串口芯片",
    "CP210": "CP210x 串口芯片",
    "FTDI": "FTDI 串口芯片",
    "PL2303": "PL2303 串口芯片",
    "USB-SERIAL": "通用 USB 串口",
    "LIN": "疑似 LIN 适配器",
}


@dataclass
class SerialPortInfo:
    """一个可用串口。"""

    device: str
    """端口号,例如 COM3。"""

    description: str = ""
    """设备描述。"""

    hwid: str = ""
    """硬件 ID。"""

    manufacturer: str = ""
    """厂商。"""

    @property
    def hint(self) -> str:
        """根据描述猜测的芯片类型提示。"""
        blob = f"{self.description} {self.hwid} {self.manufacturer}".upper()
        for key, label in KNOWN_CHIPS.items():
            if key in blob:
                return label
        return ""

    @property
    def display(self) -> str:
        """下拉框里显示的文本。"""
        text = f"{self.device} - {self.description or '串口'}"
        hint = self.hint
        if hint:
            text = f"{text} [{hint}]"
        return text

    def describe(self) -> str:
        return (
            f"端口     {self.device}\n"
            f"描述     {self.description or '未知'}\n"
            f"厂商     {self.manufacturer or '未知'}\n"
            f"硬件 ID  {self.hwid or '未知'}"
        )


def list_serial_ports() -> List[SerialPortInfo]:
    """枚举当前系统的串口。"""
    try:
        from serial.tools import list_ports
    except ImportError:
        log.warning("未安装 pyserial,无法枚举串口")
        return []

    ports: List[SerialPortInfo] = []
    try:
        for item in list_ports.comports():
            ports.append(
                SerialPortInfo(
                    device=str(item.device),
                    description=str(item.description or ""),
                    hwid=str(item.hwid or ""),
                    manufacturer=str(getattr(item, "manufacturer", "") or ""),
                )
            )
    except Exception:
        log.exception("枚举串口失败")
    ports.sort(key=lambda p: p.device)
    return ports


def find_port(ports: List[SerialPortInfo], device: str) -> SerialPortInfo | None:
    """按端口号查找。"""
    for port in ports:
        if port.device == device:
            return port
    return None
