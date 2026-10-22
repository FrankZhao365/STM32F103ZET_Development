"""设备面板:枚举 PCAN 通道、选择波特率、配置日志、打开/关闭通道。"""

from __future__ import annotations

import os
from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..device import BITRATES, PcanDevice, list_pcan_devices
from ..logbook import default_log_dir, default_stem


class DevicePanel(QWidget):
    """PCAN 通道连接控制。"""

    connectRequested = pyqtSignal(object, int, bool, int)  # device, bitrate, fd, data_bitrate
    disconnectRequested = pyqtSignal()
    logSettingsChanged = pyqtSignal(bool, str, str)  # enabled, csv_path, blf_path

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._devices: List[PcanDevice] = []
        self._current: Optional[PcanDevice] = None
        self._build()
        self.refresh_devices()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # --- 设备选择 ---
        dev_box = QGroupBox("PCAN 设备")
        dev_form = QFormLayout(dev_box)

        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(240)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setFixedWidth(60)
        self.refresh_btn.clicked.connect(self.refresh_devices)
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.refresh_btn)
        dev_form.addRow("通道", row)

        self.device_info = QLabel("-")
        self.device_info.setWordWrap(True)
        self.device_info.setStyleSheet("color:#555;")
        dev_form.addRow("信息", self.device_info)

        self.bitrate_combo = QComboBox()
        for rate in BITRATES:
            self.bitrate_combo.addItem(f"{rate:,} bit/s".replace(",", " "), rate)
        self.bitrate_combo.setCurrentIndex(BITRATES.index(500000))
        dev_form.addRow("波特率", self.bitrate_combo)

        self.fd_check = QCheckBox("启用 CAN FD")
        self.fd_check.toggled.connect(self._on_fd_toggled)
        dev_form.addRow("", self.fd_check)

        self.data_bitrate_combo = QComboBox()
        for rate in (500000, 1000000, 2000000, 4000000, 5000000):
            self.data_bitrate_combo.addItem(f"{rate:,} bit/s".replace(",", " "), rate)
        self.data_bitrate_combo.setCurrentIndex(0)
        self.data_bitrate_combo.setEnabled(False)
        dev_form.addRow("数据段速率", self.data_bitrate_combo)

        layout.addWidget(dev_box)

        # --- 日志 ---
        log_box = QGroupBox("日志记录")
        log_form = QFormLayout(log_box)

        self.log_check = QCheckBox("记录收发报文")
        self.log_check.setChecked(True)
        self.log_check.toggled.connect(self._emit_log_settings)
        log_form.addRow("", self.log_check)

        stem_row = QHBoxLayout()
        self.log_path_edit = QLineEdit(default_stem())
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.clicked.connect(self._browse_log_path)
        stem_row.addWidget(self.log_path_edit, 1)
        stem_row.addWidget(browse)
        log_form.addRow("文件名", stem_row)

        fmt_row = QHBoxLayout()
        self.csv_check = QCheckBox("CSV 文本")
        self.csv_check.setChecked(True)
        self.blf_check = QCheckBox("BLF (PCAN-View)")
        self.blf_check.setChecked(True)
        for cb in (self.csv_check, self.blf_check):
            cb.toggled.connect(self._emit_log_settings)
            fmt_row.addWidget(cb)
        fmt_row.addStretch(1)
        log_form.addRow("格式", fmt_row)

        self.log_dir_label = QLabel(f"日志目录: {default_log_dir()}")
        self.log_dir_label.setStyleSheet("color:#555;")
        self.log_dir_label.setWordWrap(True)
        log_form.addRow("", self.log_dir_label)

        layout.addWidget(log_box)

        # --- 连接 ---
        self.connect_btn = QPushButton("打开通道")
        self.connect_btn.setMinimumHeight(34)
        self.connect_btn.setStyleSheet("QPushButton{font-weight:bold;}")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        layout.addWidget(self.connect_btn)

        self.state_label = QLabel("未连接")
        self.state_label.setAlignment(Qt.AlignCenter)
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        layout.addStretch(1)

    # ------------------------------------------------------------ 设备枚举

    def refresh_devices(self) -> None:
        """重新扫描 PCAN 设备。"""
        was_channel = self._current.channel if self._current else None
        self._devices = list_pcan_devices()

        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for dev in self._devices:
            self.device_combo.addItem(dev.display, dev)
        self.device_combo.blockSignals(False)

        if not self._devices:
            self._current = None
            self.device_info.setText("未检测到 PCAN 设备\n请确认驱动已安装、设备已插好")
            self.connect_btn.setEnabled(False)
        else:
            idx = 0
            if was_channel:
                for i, dev in enumerate(self._devices):
                    if dev.channel == was_channel:
                        idx = i
                        break
            self.device_combo.setCurrentIndex(idx)
            self._on_device_changed(idx)

        if was_channel and self._current and self._current.channel != was_channel:
            self.device_info.setText(f"原通道 {was_channel} 已断开,请重新选择")
        elif self._current is None and was_channel:
            self.device_info.setText(f"原通道 {was_channel} 已断开\n未检测到 PCAN 设备")

    def _on_device_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._devices):
            return
        dev = self._devices[index]
        self._current = dev
        self.device_info.setText(dev.describe())
        self.fd_check.setEnabled(dev.supports_fd)
        if not dev.supports_fd:
            self.fd_check.setChecked(False)
        self.data_bitrate_combo.setEnabled(dev.supports_fd and self.fd_check.isChecked())

    def _on_fd_toggled(self, checked: bool) -> None:
        self.data_bitrate_combo.setEnabled(checked and bool(self._current and self._current.supports_fd))

    # ------------------------------------------------------------ 连接

    def _on_connect_clicked(self) -> None:
        if self.connected:
            self.disconnectRequested.emit()
            return
        if self._current is None:
            return
        bitrate = self.bitrate_combo.currentData()
        data_bitrate = self.data_bitrate_combo.currentData()
        self.connectRequested.emit(self._current, int(bitrate), self.fd_check.isChecked(), int(data_bitrate))

    def set_connected(self, connected: bool, text: str = "") -> None:
        """由主窗口在通道状态变化时调用。"""
        self.connect_btn.setText("关闭通道" if connected else "打开通道")
        if connected:
            self.device_combo.setEnabled(False)
            self.bitrate_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
            for cb in (self.csv_check, self.blf_check, self.log_check):
                cb.setEnabled(False)
            self.fd_check.setEnabled(False)
        else:
            self.device_combo.setEnabled(True)
            self.bitrate_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            for cb in (self.csv_check, self.blf_check, self.log_check):
                cb.setEnabled(True)
            idx = self.device_combo.currentIndex()
            if idx >= 0:
                self._on_device_changed(idx)
        self.state_label.setText(text or ("已连接" if connected else "未连接"))

    @property
    def connected(self) -> bool:
        return self.connect_btn.text() == "关闭通道"

    # ------------------------------------------------------------ 日志

    def _browse_log_path(self) -> None:
        start_dir = self.log_path_edit.text() or default_stem()
        path, _ = QFileDialog.getSaveFileName(
            self, "选择日志文件", start_dir, "日志文件 (*.csv *.blf);;所有文件 (*.*)"
        )
        if not path:
            return
        stem = os.path.splitext(path)[0]
        if os.path.splitext(path)[1].lower() not in (".csv", ".blf"):
            stem = path
        self.log_path_edit.setText(stem)
        self._emit_log_settings()

    def _emit_log_settings(self) -> None:
        stem = self.log_path_edit.text().strip() or default_stem()
        csv_path = f"{stem}.csv" if self.csv_check.isChecked() else ""
        blf_path = f"{stem}.blf" if self.blf_check.isChecked() else ""
        self.logSettingsChanged.emit(self.log_check.isChecked(), csv_path, blf_path)

    def log_config(self) -> tuple:
        """返回 (enabled, csv_path, blf_path)。"""
        stem = self.log_path_edit.text().strip() or default_stem()
        csv_path = f"{stem}.csv" if self.csv_check.isChecked() else ""
        blf_path = f"{stem}.blf" if self.blf_check.isChecked() else ""
        return self.log_check.isChecked(), csv_path, blf_path