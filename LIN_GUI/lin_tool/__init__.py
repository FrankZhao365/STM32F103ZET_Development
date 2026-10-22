"""LIN 报文监控上位机 —— 核心包。

模块划分:
    protocol.py  帧格式、受保护 ID、校验和
    transport.py 串口/仿真传输层
    ldf.py       LDF(总线描述文件)解析
    signals.py   信号编解码
    device.py    串口枚举
    models.py    Qt 表格模型
    worker.py    收发工作线程
    logbook.py   CSV 日志
    stats.py     报文统计
"""

from __future__ import annotations

APP_NAME = "LIN 报文监控上位机"
__version__ = "1.0.0"

__all__ = ["APP_NAME", "__version__"]
