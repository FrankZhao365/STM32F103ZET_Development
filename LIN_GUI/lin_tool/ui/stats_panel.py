"""报文统计面板:按帧 ID 显示次数、周期与错误数。"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..models import StatsModel


class StatsPanel(QWidget):
    """统计表。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = StatsModel()
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.table = QTableView()
        self.table.setModel(self._model)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.summary = QLabel("尚无数据")
        self.summary.setStyleSheet("color:#333;")
        layout.addWidget(self.summary)

    def apply_snapshot(self, snapshot: dict) -> None:
        """应用工作线程广播的统计快照。"""
        self._model.set_rows(snapshot.get("rows", []))
        elapsed = float(snapshot.get("elapsed", 0.0))
        rx = int(snapshot.get("rx", 0))
        tx = int(snapshot.get("tx", 0))
        errors = int(snapshot.get("errors", 0))
        rate = (rx + tx) / elapsed if elapsed > 0 else 0.0
        self.summary.setText(
            f"接收 {rx} | 发送 {tx} | 错误 {errors} | "
            f"运行 {elapsed:.1f} s | 平均 {rate:.1f} 帧/s"
        )
