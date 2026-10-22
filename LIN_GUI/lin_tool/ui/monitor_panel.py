"""报文监控面板:帧表格 + 过滤 + 统计摘要 + 导出。"""

from __future__ import annotations

import csv
import time
from typing import List, Optional, Sequence

from PyQt5.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..models import (
    COL_DATA,
    COL_DIR,
    COL_ID,
    COL_NAME,
    LinFrameFilterProxy,
    LinFrameTableModel,
)
from ..protocol import LinFrame, format_data

DIRECTION_FILTERS = [("全部", ""), ("接收 RX", "RX"), ("发送 TX", "TX")]


class MonitorPanel(QWidget):
    """实时报文列表。"""

    frameActivated = pyqtSignal(object)
    """用户选中了一帧(参数为 LinFrame)。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = LinFrameTableModel()
        self._proxy = LinFrameFilterProxy()
        self._proxy.setSourceModel(self._model)
        self._start_ts = time.time()
        self._display_paused = False
        self._pending_frames: List[LinFrame] = []
        self._paused_dropped = 0
        self._build()
        self._wire()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        bar = QHBoxLayout()

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("过滤:ID(十六进制)/帧名/数据,例如 1A")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(320)
        bar.addWidget(self.filter_edit)

        self.direction_combo = QComboBox()
        for text, value in DIRECTION_FILTERS:
            self.direction_combo.addItem(text, value)
        self.direction_combo.setMaximumWidth(110)
        bar.addWidget(self.direction_combo)

        self.errors_check = QCheckBox("仅错误")
        bar.addWidget(self.errors_check)

        self.follow_check = QCheckBox("自动跟随底部")
        self.follow_check.setChecked(True)
        self.follow_check.setToolTip(
            "停留在最新报文时自动滚动；向上查看历史报文时暂停，滚回底部后自动恢复。"
        )
        bar.addWidget(self.follow_check)

        self.pause_button = QPushButton("暂停 log")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip(
            "暂停报文列表显示；后台采集和 CSV 日志继续。恢复时显示暂停期间缓存的最新报文。"
        )
        bar.addWidget(self.pause_button)

        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(100, 200000)
        self.max_rows_spin.setSingleStep(500)
        self.max_rows_spin.setValue(self._model.max_rows)
        self.max_rows_spin.setToolTip("界面上最多保留的报文条数")
        bar.addWidget(self.max_rows_spin)

        export_btn = QPushButton("导出 CSV")
        export_btn.clicked.connect(self.export_csv)
        bar.addWidget(export_btn)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.clear)
        bar.addWidget(clear_btn)

        bar.addStretch(1)
        layout.addLayout(bar)

        self.table = QTableView()
        self.table.setModel(self._proxy)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.summary = QLabel("尚未收到报文")
        self.summary.setStyleSheet("color:#333;")
        layout.addWidget(self.summary)

    def _wire(self) -> None:
        self.filter_edit.textChanged.connect(self._proxy.set_keyword)
        self.direction_combo.currentIndexChanged.connect(
            lambda: self._proxy.set_direction(self.direction_combo.currentData() or "")
        )
        self.errors_check.toggled.connect(self._proxy.set_errors_only)
        self.follow_check.toggled.connect(self._on_follow_toggled)
        self.pause_button.toggled.connect(self._on_pause_toggled)
        self.max_rows_spin.valueChanged.connect(self._on_max_rows_changed)
        self.table.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        self.table.doubleClicked.connect(self._on_selection)

    # ------------------------------------------------------------ 数据

    def append_frames(self, frames: Sequence[LinFrame]) -> None:
        """追加一批报文。"""
        if not frames:
            return
        if self._display_paused:
            self._model.record_frames(frames)
            self._pending_frames.extend(frames)
            self._trim_pending_frames()
            self._update_summary()
            return

        follow_tail = self.follow_check.isChecked()
        # 插入行会改变滚动条范围；屏蔽这些程序性变化，避免误判成用户滚动。
        with QSignalBlocker(self.table.verticalScrollBar()):
            self._model.append_batch(frames)
        if follow_tail:
            self.table.scrollToBottom()
        self._update_summary()

    def clear(self) -> None:
        follow_tail = self.follow_check.isChecked()
        self._pending_frames.clear()
        self._paused_dropped = 0
        with QSignalBlocker(self.table.verticalScrollBar()):
            self._model.clear()
        self._model.reset_counters()
        self._start_ts = time.time()
        if follow_tail:
            self.table.scrollToBottom()
        self._update_summary()

    def _on_pause_toggled(self, paused: bool) -> None:
        """冻结/恢复报文列表显示;后台采集和 CSV 日志不受影响。"""
        self._display_paused = paused
        self.pause_button.setText("继续 log" if paused else "暂停 log")
        if not paused and self._pending_frames:
            frames = self._pending_frames
            self._pending_frames = []
            follow_tail = self.follow_check.isChecked()
            with QSignalBlocker(self.table.verticalScrollBar()):
                self._model.append_batch(frames, count_totals=False)
            if follow_tail:
                self.table.scrollToBottom()
        if not paused:
            self._paused_dropped = 0
        self._update_summary()

    def _on_max_rows_changed(self, value: int) -> None:
        self._model.set_max_rows(value)
        self._trim_pending_frames()
        self._update_summary()

    def _trim_pending_frames(self) -> None:
        """让暂停期间的缓存不超过表格最大行数。"""
        overflow = len(self._pending_frames) - self._model.max_rows
        if overflow > 0:
            del self._pending_frames[:overflow]
            self._paused_dropped += overflow

    def _on_scroll_value_changed(self, value: int) -> None:
        """用户回到最新报文时恢复跟随，滚离底部时暂停。"""
        bar = self.table.verticalScrollBar()
        at_bottom = value >= bar.maximum() - max(1, self.table.verticalHeader().defaultSectionSize())
        if self.follow_check.isChecked() != at_bottom:
            self.follow_check.setChecked(at_bottom)

    def _on_follow_toggled(self, enabled: bool) -> None:
        """手动启用跟随时立即定位到最新报文。"""
        if enabled:
            self.table.scrollToBottom()

    def _on_selection(self, *args) -> None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return
        source_index = self._proxy.mapToSource(indexes[0])
        frame = self._model.frame_at(source_index.row())
        if frame is not None:
            self.frameActivated.emit(frame)

    def _update_summary(self) -> None:
        elapsed = max(1e-6, time.time() - self._start_ts)
        total = self._model.rx_total + self._model.tx_total
        rate = total / elapsed
        self.summary.setText(
            f"共 {total} 帧 | 接收 {self._model.rx_total} | 发送 {self._model.tx_total} | "
            f"错误 {self._model.error_total} | 平均 {rate:.1f} 帧/s | "
            f"显示 {self.table.model().rowCount()} 行"
            + (
                f" | 显示暂停，缓存 {len(self._pending_frames)} 帧"
                + (f"，已丢弃较早的 {self._paused_dropped} 帧" if self._paused_dropped else "")
                if self._display_paused
                else ""
            )
        )

    # ------------------------------------------------------------ 导出

    def export_csv(self) -> None:
        """把当前表格内容导出成 CSV。"""
        default = f"lin_frames_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出报文", default, "CSV 文件 (*.csv)"
        )
        if not path:
            return

        rows = self.table.model()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["time", "dir", "id_hex", "name", "dlc", "data_hex", "checksum", "note"]
            )
            for row in range(rows.rowCount()):
                index = rows.index(row, 0)
                frame = rows.data(index, Qt.UserRole)
                if frame is None:
                    continue
                writer.writerow(
                    [
                        time.strftime("%H:%M:%S", time.localtime(frame.timestamp)),
                        frame.direction,
                        f"0x{frame.frame_id:02X}",
                        frame.display_name,
                        frame.dlc,
                        format_data(frame.data),
                        f"0x{frame.checksum:02X}",
                        frame.note,
                    ]
                )
