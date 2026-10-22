"""按帧 ID 统计报文数量、周期与错误数。

由工作线程更新(它能看到全部流量),界面按定时器取快照,
这样"清空显示"不会影响统计,统计始终反映真实总线负载。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .protocol import LinFrame, format_data


@dataclass
class FrameStat:
    """单个 (帧 ID, 方向) 的统计。"""

    frame_id: int = 0
    direction: str = "RX"
    count: int = 0
    errors: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    last_data: bytes = b""
    name: str = ""

    @property
    def period_ms(self) -> float:
        """首尾两次之间的平均周期(ms)。"""
        if self.count < 2:
            return 0.0
        span = self.last_ts - self.first_ts
        if span <= 0:
            return 0.0
        return span * 1000.0 / (self.count - 1)


@dataclass
class FrameStats:
    """统计集合,只在工作线程里被 add()/reset()。"""

    _data: Dict[Tuple[int, str], FrameStat] = field(
        default_factory=lambda: defaultdict(FrameStat)
    )
    total_rx: int = 0
    total_tx: int = 0
    total_errors: int = 0
    start_ts: float = 0.0
    last_ts: float = 0.0

    def add(self, frame: LinFrame) -> None:
        """累加一帧。"""
        key = (frame.frame_id, frame.direction)
        stat = self._data[key]
        stat.frame_id = frame.frame_id
        stat.direction = frame.direction
        if stat.first_ts == 0.0:
            stat.first_ts = frame.timestamp
            if self.start_ts == 0.0:
                self.start_ts = frame.timestamp
        stat.last_ts = frame.timestamp
        stat.last_data = bytes(frame.data)
        if frame.name:
            stat.name = frame.name
        stat.count += 1
        if frame.is_error:
            stat.errors += 1
            self.total_errors += 1

        if frame.direction == "TX":
            self.total_tx += 1
        else:
            self.total_rx += 1
        self.last_ts = frame.timestamp

    def reset(self) -> None:
        self._data.clear()
        self.total_rx = 0
        self.total_tx = 0
        self.total_errors = 0
        self.start_ts = 0.0
        self.last_ts = 0.0

    def snapshot(self, newest_first: bool = True) -> List[dict]:
        """返回统计快照,供界面展示。"""
        rows: List[dict] = []
        for stat in self._data.values():
            period = stat.period_ms
            rows.append(
                {
                    "frame_id": stat.frame_id,
                    "name": stat.name,
                    "direction": stat.direction,
                    "count": stat.count,
                    "errors": stat.errors,
                    "period_ms": period,
                    "rate_hz": (1000.0 / period) if period > 0 else 0.0,
                    "data": format_data(stat.last_data),
                }
            )
        rows.sort(key=lambda r: (r["frame_id"], r["direction"]), reverse=newest_first)
        return rows

    def elapsed_s(self) -> float:
        if self.start_ts <= 0.0 or self.last_ts <= self.start_ts:
            return 0.0
        return self.last_ts - self.start_ts
