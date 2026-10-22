"""CSV / BLF 日志落盘。

CSV 给人直接看(Excel / grep),BLF 给 PCAN-View 回放分析。
两者共用 python-can 的 `can.Message`,收发方向由 `msg.is_rx` 标记,
BLFWriter 会把它写成 BLF 的 DIR 标志,PCAN-View 里能正确区分收发。
"""

from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime
from typing import List, Optional, Sequence

import can

log = logging.getLogger(__name__)

CSV_HEADER = [
    "time",
    "time_s",
    "dir",
    "id_type",
    "id_hex",
    "dlc",
    "data_hex",
    "flags",
]

_FLUSH_EVERY = 200
_FLUSH_SECONDS = 1.0
# BLF 容器攒到这个大小就落盘(见 _open_blf)
_BLF_FLUSH_BYTES = 64 * 1024


def default_log_dir(base: Optional[str] = None) -> str:
    """默认日志目录: <程序目录>/logs,不存在则创建。"""
    base = base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "logs")
    os.makedirs(path, exist_ok=True)
    return path


def default_stem(d: Optional[str] = None) -> str:
    """按时间戳生成日志文件名主干,如 canlog_20261013_234501。"""
    d = d or default_log_dir()
    return os.path.join(d, datetime.now().strftime("canlog_%Y%m%d_%H%M%S"))


class LogBook:
    """同时写 CSV 与 BLF 的日志器。

    只在 CAN 工作线程里调用,因此内部无需加锁。
    """

    def __init__(
        self,
        csv_path: Optional[str] = None,
        blf_path: Optional[str] = None,
        append: bool = False,
    ) -> None:
        self.csv_path = csv_path
        self.blf_path = blf_path
        self._csv_file = None
        self._csv_writer: Optional[csv.writer] = None
        self._blf = None
        self._pending = 0
        self._last_flush = time.monotonic()
        self._count = 0
        self._rx_count = 0
        self._tx_count = 0
        self._closed = False

        if csv_path:
            self._open_csv(csv_path, append)
        if blf_path:
            self._open_blf(blf_path, append)

    # ------------------------------------------------------------------ 打开

    def _open_csv(self, path: str, append: bool) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        self._csv_file = open(path, "a" if append else "w", newline="", encoding="utf-8-sig")
        self._csv_writer = csv.writer(self._csv_file)
        # 追加到已有文件时不重复写表头
        if not (append and exists):
            self._csv_writer.writerow(CSV_HEADER)

    def _open_blf(self, path: str, append: bool) -> None:
        from can.io.blf import BLFWriter

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # 调小容器上限,让 BLF 更频繁地刷盘。python-can 默认要攒够约 1 MB
        # 才落盘,一旦进程被强杀或断电,这段数据就整个丢了;
        # 64 KB 大约合几千帧,把最坏情况下的丢失量压到很小。
        self._blf = BLFWriter(
            path, append=append, channel=1, max_container_size=_BLF_FLUSH_BYTES
        )

    # ------------------------------------------------------------------ 写入

    @property
    def enabled(self) -> bool:
        return self._csv_writer is not None or self._blf is not None

    @property
    def count(self) -> int:
        return self._count

    def write(self, messages: Sequence[can.Message]) -> None:
        """批量写入一批报文。"""
        if self._closed or not messages:
            return

        writer = self._csv_writer
        for msg in messages:
            self._count += 1
            if msg.is_rx:
                self._rx_count += 1
            else:
                self._tx_count += 1

            if writer is not None:
                writer.writerow(self._csv_row(msg))
            if self._blf is not None:
                self._blf.on_message_received(msg)

        self._pending += len(messages)
        now = time.monotonic()
        if self._pending >= _FLUSH_EVERY or (now - self._last_flush) >= _FLUSH_SECONDS:
            self.flush()

    def write_one(self, msg: can.Message) -> None:
        """发送失败时也把报文记进日志,保证日志与实际尝试一致。"""
        self.write([msg])

    @staticmethod
    def _csv_row(msg: can.Message) -> List[str]:
        flags = []
        if msg.is_remote_frame:
            flags.append("RTR")
        if msg.is_error_frame:
            flags.append("ERR")
        if msg.is_fd:
            flags.append("FD")
        if msg.bitrate_switch:
            flags.append("BRS")
        return [
            datetime.fromtimestamp(msg.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            f"{msg.timestamp:.6f}",
            "RX" if msg.is_rx else "TX",
            "EXT" if msg.is_extended_id else "STD",
            f"0x{msg.arbitration_id:X}",
            str(len(msg.data)),
            " ".join(f"{b:02X}" for b in msg.data),
            "|".join(flags),
        ]

    # ------------------------------------------------------------------ 收尾

    def flush(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
            except ValueError:
                pass  # 文件已关闭
        self._pending = 0
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                log.exception("关闭 CSV 失败")
            finally:
                self._csv_file = None
                self._csv_writer = None
        if self._blf is not None:
            try:
                self._blf.stop()
            except Exception:
                log.exception("关闭 BLF 失败")
            finally:
                self._blf = None

    def __enter__(self) -> "LogBook":
        return self

    def __exit__(self, *exc) -> None:
        self.close()