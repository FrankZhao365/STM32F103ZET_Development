"""CSV 日志落盘。

CSV 给人直接看(Excel / grep / 后续脚本处理),
列与 PCAN 工具保持一致的风格,方便两个工具的日志一起分析。
"""

from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime
from typing import List, Optional, Sequence

from .protocol import LinFrame

log = logging.getLogger(__name__)

CSV_HEADER = [
    "time",
    "time_s",
    "dir",
    "id_hex",
    "name",
    "dlc",
    "data_hex",
    "checksum",
    "checksum_type",
    "checksum_ok",
    "note",
]

_FLUSH_EVERY = 200
_FLUSH_SECONDS = 1.0


def default_log_dir(base: Optional[str] = None) -> str:
    """默认日志目录:<程序目录>/logs。"""
    base = base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def default_stem(directory: Optional[str] = None) -> str:
    """按时间戳生成日志文件名主干,如 linlog_20261022_184530。"""
    directory = directory or default_log_dir()
    return os.path.join(directory, datetime.now().strftime("linlog_%Y%m%d_%H%M%S"))


class LogBook:
    """LIN 报文日志器(只在工作线程里调用,内部不加锁)。"""

    def __init__(self, csv_path: Optional[str] = None, append: bool = False) -> None:
        self.csv_path = csv_path
        self._csv_file = None
        self._csv_writer: Optional[csv.writer] = None
        self._pending = 0
        self._last_flush = time.monotonic()
        self._count = 0
        self._rx_count = 0
        self._tx_count = 0
        self._error_count = 0
        self._closed = False
        if csv_path:
            self._open_csv(csv_path, append)

    # ------------------------------------------------------------------ 打开

    def _open_csv(self, path: str, append: bool) -> None:
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        self._csv_file = open(path, "a" if append else "w", newline="", encoding="utf-8-sig")
        self._csv_writer = csv.writer(self._csv_file)
        if not (append and exists):
            self._csv_writer.writerow(CSV_HEADER)

    # ------------------------------------------------------------------ 写入

    @property
    def enabled(self) -> bool:
        return self._csv_writer is not None

    @property
    def count(self) -> int:
        return self._count

    @property
    def rx_count(self) -> int:
        return self._rx_count

    @property
    def tx_count(self) -> int:
        return self._tx_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def write(self, frames: Sequence[LinFrame]) -> None:
        """批量写入一批报文。"""
        if self._closed or not frames:
            return
        writer = self._csv_writer
        for frame in frames:
            self._count += 1
            if frame.direction == "TX":
                self._tx_count += 1
            else:
                self._rx_count += 1
            if frame.is_error:
                self._error_count += 1
            if writer is None:
                continue
            writer.writerow(self._row(frame))

        self._pending += len(frames)
        now = time.monotonic()
        if self._pending >= _FLUSH_EVERY or (now - self._last_flush) >= _FLUSH_SECONDS:
            self.flush()

    def write_one(self, frame: LinFrame) -> None:
        """写入单帧。"""
        self.write([frame])

    @staticmethod
    def _row(frame: LinFrame) -> List[str]:
        return [
            datetime.fromtimestamp(frame.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            f"{frame.timestamp:.6f}",
            frame.direction,
            f"0x{frame.frame_id:02X}",
            frame.name or frame.display_name,
            str(frame.dlc),
            " ".join(f"{b:02X}" for b in frame.data),
            f"0x{frame.checksum:02X}",
            frame.checksum_type.value,
            "1" if frame.checksum_valid else "0",
            frame.note,
        ]

    # ------------------------------------------------------------------ 收尾

    def flush(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
            except ValueError:
                pass
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

    def __enter__(self) -> "LogBook":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
