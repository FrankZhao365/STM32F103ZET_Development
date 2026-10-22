"""按 CAN ID 统计报文数量与周期。

由 CAN 工作线程更新(它能看到全部流量),界面按定时器取快照。
这样"清空显示"不会影响统计,统计始终反映真实总线负载。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class IdStat:
    """单个 (ID, 标准/扩展, 收/发) 组合的统计信息。"""

    rx_count: int = 0
    tx_count: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    last_dlc: int = 0

    @property
    def total(self) -> int:
        return self.rx_count + self.tx_count

    def period_ms(self) -> float:
        """首尾两次之间的平均周期(ms)。"""
        if self.rx_count < 2:
            return 0.0
        span = self.last_ts - self.first_ts
        return span * 1000.0 / (self.rx_count - 1)


@dataclass
class IdStats:
    """ID 统计集合。

    只在 CAN 工作线程里被 add()/reset(),界面侧只读 snapshot()。
    """

    _data: Dict[Tuple[int, bool, bool], IdStat] = field(default_factory=lambda: defaultdict(IdStat))
    total_rx: int = 0
    total_tx: int = 0
    start_ts: float = 0.0
    last_ts: float = 0.0

    def add(self, msg) -> None:
        """累加一条报文。`msg` 需具备 python-can Message 的字段。"""
        key = (msg.arbitration_id, msg.is_extended_id, not msg.is_rx)
        stat = self._data[key]
        if stat.first_ts == 0.0:
            stat.first_ts = msg.timestamp
            if self.start_ts == 0.0:
                self.start_ts = msg.timestamp
        stat.last_ts = msg.timestamp
        stat.last_dlc = len(msg.data)

        if msg.is_rx:
            stat.rx_count += 1
            stat.rx_bytes += len(msg.data)
            self.total_rx += 1
        else:
            stat.tx_count += 1
            stat.tx_bytes += len(msg.data)
            self.total_tx += 1
        self.last_ts = msg.timestamp

    def reset(self) -> None:
        self._data.clear()
        self.total_rx = 0
        self.total_tx = 0
        self.start_ts = 0.0
        self.last_ts = 0.0

    def snapshot(self, newest_first: bool = True) -> List[dict]:
        """返回统计快照,供界面展示。"""
        rows = []
        for (arb_id, is_ext, is_tx), stat in self._data.items():
            rows.append(
                {
                    "arb_id": arb_id,
                    "is_extended": is_ext,
                    "dir": "TX" if is_tx else "RX",
                    "rx": stat.rx_count,
                    "tx": stat.tx_count,
                    "total": stat.total,
                    "dlc": stat.last_dlc,
                    "period_ms": stat.period_ms(),
                    "rate_hz": (1000.0 / stat.period_ms()) if stat.period_ms() > 0 else 0.0,
                }
            )
        rows.sort(key=lambda r: (r["arb_id"], r["dir"]), reverse=newest_first)
        return rows

    def elapsed_s(self) -> float:
        if self.start_ts <= 0.0 or self.last_ts <= self.start_ts:
            return 0.0
        return self.last_ts - self.start_ts