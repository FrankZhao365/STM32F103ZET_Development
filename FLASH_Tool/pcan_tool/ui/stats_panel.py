"""统计面板:按 ID 汇总收发数量与周期。"""

from __future__ import annotations

from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

COLUMNS = ["ID", "方向", "DLC", "接收", "发送", "合计", "周期(ms)", "频率(Hz)"]


class StatsPanel(QWidget):
    """显示工作线程推送的统计快照。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        self.summary = QLabel("无数据")
        self.summary.setStyleSheet("color:#555;")
        bar.addWidget(self.summary)
        bar.addStretch(1)
        clear_btn = QPushButton("清零统计")
        clear_btn.clicked.connect(lambda: self.apply_snapshot(None))
        bar.addWidget(clear_btn)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

    def apply_snapshot(self, snapshot: dict) -> None:
        """由主窗口定时调用。snapshot 为 None 表示清空。"""
        if snapshot is None:
            self.table.setRowCount(0)
            self.summary.setText("无数据")
            return

        rows: List[dict] = snapshot.get("rows", [])
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            period = row.get("period_ms", 0.0)
            values = [
                f"0x{row['arb_id']:X}" + (" EXT" if row["is_extended"] else ""),
                row["dir"],
                str(row["dlc"]),
                str(row["rx"]),
                str(row["tx"]),
                str(row["total"]),
                f"{period:.1f}" if period else "-",
                f"{row['rate_hz']:.1f}" if row["rate_hz"] else "-",
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c in (2, 3, 4, 5):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 1:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, c, item)

        elapsed = snapshot.get("elapsed", 0.0)
        self.summary.setText(
            f"接收 {snapshot.get('rx', 0)} 帧 / 发送 {snapshot.get('tx', 0)} 帧"
            f" / ID 数 {len(rows)}"
            f" / 用时 {elapsed:.1f} s"
            f" / 发送失败 {snapshot.get('tx_errors', 0)}"
        )