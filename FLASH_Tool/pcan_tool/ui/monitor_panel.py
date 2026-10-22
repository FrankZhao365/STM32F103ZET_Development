"""监控面板:实时报文表格 + 过滤 + 导出。"""

from __future__ import annotations

import csv
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..models import MessageFilterProxy, MessageTableModel

EXPORT_HEADER = ["time", "dir", "id_type", "id_hex", "dlc", "data_hex"]


class MonitorPanel(QWidget):
    """收发报文实时监控。"""

    messageActivated = pyqtSignal(object)  # can.Message,供发送面板载入

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._paused = False
        self._pending: list = []
        self._last_batch_ts = 0.0
        self._fps = 0.0
        self._build()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # --- 工具条 ---
        bar = QHBoxLayout()

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        bar.addWidget(self.pause_btn)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self.clear)
        bar.addWidget(self.clear_btn)

        self.reset_count_btn = QPushButton("计数清零")
        self.reset_count_btn.clicked.connect(self._on_reset_count)
        bar.addWidget(self.reset_count_btn)

        self.auto_scroll_check = QCheckBox("自动滚动")
        self.auto_scroll_check.setChecked(True)
        bar.addWidget(self.auto_scroll_check)

        self.show_rtr_check = QCheckBox("含 RTR")
        self.show_rtr_check.setChecked(True)
        self.show_rtr_check.toggled.connect(self._reapply_filter)
        bar.addWidget(self.show_rtr_check)

        self.export_btn = QPushButton("导出 CSV")
        self.export_btn.clicked.connect(self.export_csv)
        bar.addWidget(self.export_btn)

        bar.addStretch(1)

        bar.addWidget(QLabel("过滤"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("ID 或数据关键字,如 1A3 / 01 02")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)
        self.filter_edit.setMinimumWidth(200)
        bar.addWidget(self.filter_edit)

        self.dir_combo = QComboBox()
        self.dir_combo.addItems(["全部", "仅接收", "仅发送"])
        self.dir_combo.currentIndexChanged.connect(self._on_dir_changed)
        bar.addWidget(self.dir_combo)

        layout.addLayout(bar)

        # --- 表格 ---
        self.model = MessageTableModel()
        self.proxy = MessageFilterProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(-1)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setFont(QFont("Consolas", 9))
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.doubleClicked.connect(self._on_double_click)

        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        for col, width in enumerate((60, 100, 50, 90, 50, 40, 0)):
            self.table.setColumnWidth(col, width)
        self.table.setColumnWidth(6, 320)
        layout.addWidget(self.table, 1)

        # --- 状态条 ---
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color:#555;")
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------ 数据入口

    def append_messages(self, messages: list) -> None:
        """主窗口收到批量报文时调用。"""
        if not messages:
            return

        if self._paused:
            self._pending.extend(messages)
            self._update_status(paused_extra=len(self._pending))
            return

        now = time.monotonic()
        if self._last_batch_ts:
            elapsed = now - self._last_batch_ts
            if elapsed > 0:
                instant = len(messages) / elapsed
                self._fps = instant if self._fps <= 0 else self._fps * 0.7 + instant * 0.3
        self._last_batch_ts = now

        self.model.append_batch(messages)
        self._auto_scroll()
        self._update_status()

    def clear(self) -> None:
        self.model.clear()
        self._pending.clear()
        self._fps = 0.0
        self._last_batch_ts = 0.0
        self._update_status()

    def _on_reset_count(self) -> None:
        self.model.reset_counters()
        self._update_status()

    def _auto_scroll(self) -> None:
        if not self.auto_scroll_check.isChecked():
            return
        view = self.table
        sb = view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _update_status(self, paused_extra: int = 0) -> None:
        proxy = self.proxy
        shown = proxy.rowCount()
        total = self.model.rowCount()
        parts = [
            f"显示 {shown}/{total} 行",
            f"接收 {self.model.rx_total}",
            f"发送 {self.model.tx_total}",
            f"{self._fps:.0f} 帧/秒",
        ]
        if paused_extra:
            parts.append(f"暂停缓存 {paused_extra} 帧")
        self.status_label.setText("  |  ".join(parts))

    # ------------------------------------------------------------ 交互

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self.pause_btn.setText("继续" if paused else "暂停")
        if not paused and self._pending:
            pending, self._pending = self._pending, []
            self.append_messages(pending)
        self._update_status(paused_extra=len(self._pending))

    def _on_filter_changed(self, text: str) -> None:
        self.proxy.set_keyword(text)
        self._update_status()

    def _on_dir_changed(self, index: int) -> None:
        self.proxy.set_direction({1: "RX", 2: "TX"}.get(index, ""))
        self._update_status()

    def _reapply_filter(self) -> None:
        self.proxy.invalidateFilter()
        self._update_status()

    def _selected_messages(self) -> list:
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        out = []
        for r in sorted(rows):
            src = self.proxy.mapToSource(self.proxy.index(r, 0))
            msg = self.model.message_at(src.row())
            if msg is not None:
                out.append(msg)
        return out

    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        copy_act = menu.addAction("复制所选行")
        load_act = menu.addAction("载入到发送框")
        menu.addSeparator()
        clear_act = menu.addAction("清空表格")
        chosen = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is copy_act:
            self._copy_selected()
        elif chosen is load_act:
            msgs = self._selected_messages()
            if msgs:
                self.messageActivated.emit(msgs[-1])
            else:
                QMessageBox.information(self, "提示", "请先选中至少一行")
        elif chosen is clear_act:
            self.clear()

    def _on_double_click(self, index) -> None:
        src = self.proxy.mapToSource(index)
        msg = self.model.message_at(src.row())
        if msg is not None:
            self.messageActivated.emit(msg)

    def _copy_selected(self) -> None:
        msgs = self._selected_messages()
        if not msgs:
            return
        lines = [
            f"{'RX' if m.is_rx else 'TX'} 0x{m.arbitration_id:X} "
            f"[{len(m.data)}] {' '.join(f'{b:02X}' for b in m.data)}"
            for m in msgs
        ]
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(lines))

    # ------------------------------------------------------------ 导出

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出报文", "can_export.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        msgs = self._selected_messages() or self.model.all_messages()
        if not msgs:
            QMessageBox.information(self, "提示", "没有可导出的报文")
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(EXPORT_HEADER)
                for m in msgs:
                    writer.writerow(
                        [
                            f"{m.timestamp:.6f}",
                            "RX" if m.is_rx else "TX",
                            "EXT" if m.is_extended_id else "STD",
                            f"0x{m.arbitration_id:X}",
                            len(m.data),
                            " ".join(f"{b:02X}" for b in m.data),
                        ]
                    )
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", f"已导出 {len(msgs)} 帧到\n{path}")