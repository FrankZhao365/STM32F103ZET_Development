"""发送面板:单条发送、批量 ID 发送、周期发送、发送历史。"""

from __future__ import annotations

import time
from typing import List

import can
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .utils import format_data, parse_can_id, parse_data

MAX_HISTORY = 200


class SendPanel(QWidget):
    """CAN 报文发送。"""

    sendRequested = pyqtSignal(object)  # can.Message
    cyclicRequested = pyqtSignal(float, object, int)  # interval_ms, msg, repeat
    stopCyclicRequested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._history: List[can.Message] = []
        self._connected = False
        self._build()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # --- 报文编辑 ---
        box = QGroupBox("发送报文")
        form = QFormLayout(box)

        id_row = QHBoxLayout()
        self.id_edit = QLineEdit("0x100")
        id_font = QFont("Consolas", 11)
        id_font.setBold(True)
        self.id_edit.setFont(id_font)
        self.id_edit.setPlaceholderText("十六进制,多个 ID 用逗号分隔")
        self.id_edit.returnPressed.connect(self._on_send)
        id_row.addWidget(self.id_edit, 1)

        self.ext_check = QCheckBox("扩展帧")
        self.ext_check.toggled.connect(self._on_ext_toggled)
        id_row.addWidget(self.ext_check)
        form.addRow("CAN ID", id_row)

        self.data_edit = QLineEdit("01 02 03 04")
        self.data_edit.setFont(QFont("Consolas", 11))
        self.data_edit.setPlaceholderText("如 01 02 03 04,最多 8 字节")
        self.data_edit.returnPressed.connect(self._on_send)
        form.addRow("数据", self.data_edit)

        self.rtr_check = QCheckBox("远程帧 (RTR)")
        form.addRow("", self.rtr_check)

        dlc_row = QHBoxLayout()
        self.dlc_combo = QComboBox()
        self.dlc_combo.addItems([str(i) for i in range(9)])
        self.dlc_combo.setCurrentIndex(4)
        self.dlc_combo.setEnabled(False)
        dlc_row.addWidget(self.dlc_combo)
        dlc_row.addWidget(QLabel("默认按数据实际长度自动填充"))
        dlc_row.addStretch(1)
        form.addRow("DLC", dlc_row)

        btn_row = QHBoxLayout()
        self.send_btn = QPushButton("发送")
        self.send_btn.setMinimumHeight(32)
        self.send_btn.setStyleSheet("QPushButton{font-weight:bold;}")
        self.send_btn.clicked.connect(self._on_send)
        btn_row.addWidget(self.send_btn)

        self.fill_zero_btn = QPushButton("填 0")
        self.fill_zero_btn.clicked.connect(lambda: self._fill_data(b"\x00" * self._current_dlc()))
        btn_row.addWidget(self.fill_zero_btn)

        self.fill_ff_btn = QPushButton("填 FF")
        self.fill_ff_btn.clicked.connect(lambda: self._fill_data(b"\xff" * self._current_dlc()))
        btn_row.addWidget(self.fill_ff_btn)
        form.addRow("", btn_row)

        layout.addWidget(box)

        # --- 周期发送 ---
        cyc_box = QGroupBox("周期发送")
        cyc_form = QFormLayout(cyc_box)

        interval_row = QHBoxLayout()
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(1.0, 60000.0)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setValue(100.0)
        self.interval_spin.setSuffix(" ms")
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch(1)
        cyc_form.addRow("周期", interval_row)

        repeat_row = QHBoxLayout()
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(0, 1000000)
        self.repeat_spin.setValue(0)
        self.repeat_spin.setSpecialValueText("无限")
        repeat_row.addWidget(self.repeat_spin)
        repeat_row.addStretch(1)
        cyc_form.addRow("次数", repeat_row)

        cyc_btn_row = QHBoxLayout()
        self.cyclic_start_btn = QPushButton("开始周期发送")
        self.cyclic_start_btn.clicked.connect(self._on_cyclic_start)
        self.cyclic_stop_btn = QPushButton("停止")
        self.cyclic_stop_btn.setEnabled(False)
        self.cyclic_stop_btn.clicked.connect(self.stopCyclicRequested.emit)
        cyc_btn_row.addWidget(self.cyclic_start_btn)
        cyc_btn_row.addWidget(self.cyclic_stop_btn)
        cyc_form.addRow("", cyc_btn_row)

        self.cyclic_state = QLabel("未运行")
        self.cyclic_state.setStyleSheet("color:#555;")
        cyc_form.addRow("", self.cyclic_state)

        layout.addWidget(cyc_box)

        # --- 发送历史 ---
        hist_box = QGroupBox("发送历史 (双击可载入)")
        hist_layout = QVBoxLayout(hist_box)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["时间", "方向", "ID", "数据"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setMaximumHeight(190)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.cellDoubleClicked.connect(self._on_history_double_click)
        hist_layout.addWidget(self.history_table)

        hist_btn_row = QHBoxLayout()
        clear_hist = QPushButton("清空历史")
        clear_hist.clicked.connect(self.clear_history)
        hist_btn_row.addWidget(clear_hist)
        hist_btn_row.addStretch(1)
        hist_layout.addLayout(hist_btn_row)
        layout.addWidget(hist_box)

        layout.addStretch(1)

    # ------------------------------------------------------------ 内部

    def _on_ext_toggled(self, extended: bool) -> None:
        """扩展 ID 时数据上限仍按经典 CAN 算;这里只更新提示文案。"""
        self.data_edit.setPlaceholderText(
            "如 01 02 03 04,最多 8 字节" if not extended else "如 01 02 03 04,最多 8 字节(扩展帧)"
        )

    def _current_dlc(self) -> int:
        return self.dlc_combo.currentIndex() if self.dlc_combo.isEnabled() else 8

    def _fill_data(self, data: bytes) -> None:
        self.data_edit.setText(format_data(data))

    def _build_message(self) -> List[can.Message]:
        """按当前输入构造待发送报文(可能多帧)。"""
        extended = self.ext_check.isChecked()
        ids = parse_can_id(self.id_edit.text(), extended)
        data = parse_data(self.data_edit.text())
        is_rtr = self.rtr_check.isChecked()

        msgs = []
        for arb_id in ids:
            msgs.append(
                can.Message(
                    timestamp=time.time(),
                    arbitration_id=arb_id,
                    is_extended_id=extended,
                    is_remote_frame=is_rtr,
                    is_rx=False,
                    data=b"" if is_rtr else data,
                )
            )
        return msgs

    # ------------------------------------------------------------ 动作

    def _on_send(self) -> None:
        if not self._connected:
            self.cyclic_state.setText("请先打开 CAN 通道")
            return
        try:
            msgs = self._build_message()
        except ValueError as exc:
            self.cyclic_state.setText(f"输入有误: {exc}")
            return
        for msg in msgs:
            self.sendRequested.emit(msg)
        self.cyclic_state.setText(f"已提交发送 {len(msgs)} 帧")

    def _on_cyclic_start(self) -> None:
        if not self._connected:
            self.cyclic_state.setText("请先打开 CAN 通道")
            return
        try:
            msgs = self._build_message()
        except ValueError as exc:
            self.cyclic_state.setText(f"输入有误: {exc}")
            return

        interval = self.interval_spin.value()
        repeat = self.repeat_spin.value()
        for msg in msgs:
            self.cyclicRequested.emit(interval, msg, repeat)
        self.cyclic_start_btn.setEnabled(False)
        self.cyclic_stop_btn.setEnabled(True)
        self.cyclic_state.setText(
            f"运行中: 每 {interval:g} ms 一帧" + (" (持续发送)" if repeat <= 0 else f" × {repeat} 次")
        )

    def on_cyclic_stopped(self) -> None:
        self.cyclic_start_btn.setEnabled(True)
        self.cyclic_stop_btn.setEnabled(False)
        self.cyclic_state.setText("已停止")

    def _on_history_double_click(self, row: int, _col: int) -> None:
        item = self.history_table.item(row, 2)
        if item is None:
            return
        self.id_edit.setText(item.text())
        if item.data(Qt.UserRole) == "EXT":
            self.ext_check.setChecked(True)
        data_item = self.history_table.item(row, 3)
        if data_item:
            self.data_edit.setText(data_item.text())

    # ------------------------------------------------------------ 历史

    def add_history(self, msg: can.Message) -> None:
        """主窗口在收到发送成功的记录时调用。"""
        self._history.insert(0, msg)
        if len(self._history) > MAX_HISTORY:
            self._history.pop()

        row = self.history_table.rowCount()
        self.history_table.insertRow(0)
        stamp = time.strftime("%H:%M:%S", time.localtime(msg.timestamp))
        cells = [
            stamp,
            "TX",
            f"0x{msg.arbitration_id:X}",
            format_data(msg.data) if msg.data else ("RTR" if msg.is_remote_frame else "-"),
        ]
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if col == 2:
                item.setData(Qt.UserRole, "EXT" if msg.is_extended_id else "STD")
            self.history_table.setItem(0, col, item)

    def clear_history(self) -> None:
        self._history.clear()
        self.history_table.setRowCount(0)

    # ------------------------------------------------------------ 状态

    def set_connected(self, connected: bool) -> None:
            """通道开关时调用。未连接时禁用发送按钮,输入区仍可预填。"""
            self._connected = connected
            self.send_btn.setEnabled(connected)
            self.cyclic_start_btn.setEnabled(connected)
            self.cyclic_stop_btn.setEnabled(False)
            self.id_edit.setEnabled(True)
            self.data_edit.setEnabled(True)
            if not connected:
                self.cyclic_state.setText("未运行")