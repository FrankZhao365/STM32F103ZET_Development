"""设备面板:选择串口/波特率/工作模式,加载 LDF,配置日志,打开或关闭通道。"""

from __future__ import annotations

from dataclasses import dataclass
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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..device import BAUDRATES, SerialPortInfo, list_serial_ports
from ..ldf import LdfDatabase, LdfError, load_ldf
from ..logbook import default_log_dir, default_stem
from ..worker import MODE_LABELS, MODE_MASTER, MODE_SIM, MODE_SNIFF

BREAK_MODES = [
    ("auto", "自动(优先用控制位)"),
    ("control", "UART BREAK 控制位"),
    ("baud", "波特率切换"),
]


@dataclass
class ConnectionSettings:
    """连接参数。"""

    mode: str
    port: str
    baudrate: int
    break_mode: str
    response_timeout_ms: float
    log_enabled: bool
    log_path: str


class DevicePanel(QWidget):
    """LIN 通道连接控制。"""

    connectRequested = pyqtSignal(object)
    disconnectRequested = pyqtSignal()
    logSettingsChanged = pyqtSignal(bool, str)
    ldfLoaded = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ports: List[SerialPortInfo] = []
        self._ldf: Optional[LdfDatabase] = None
        self._build()
        self.refresh_ports()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # --- 通道 ---
        dev_box = QGroupBox("LIN 通道")
        dev_form = QFormLayout(dev_box)

        row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(200)
        refresh_btn = QPushButton("刷新")
        refresh_btn.setFixedWidth(60)
        refresh_btn.clicked.connect(self.refresh_ports)
        row.addWidget(self.port_combo, 1)
        row.addWidget(refresh_btn)
        dev_form.addRow("串口", row)

        self.port_info = QLabel("-")
        self.port_info.setWordWrap(True)
        self.port_info.setStyleSheet("color:#555;")
        dev_form.addRow("信息", self.port_info)

        self.baud_combo = QComboBox()
        for rate in BAUDRATES:
            self.baud_combo.addItem(f"{rate:,} bit/s".replace(",", " "), rate)
        self.baud_combo.setCurrentIndex(BAUDRATES.index(19200))
        dev_form.addRow("波特率", self.baud_combo)

        self.mode_combo = QComboBox()
        for mode in (MODE_MASTER, MODE_SNIFF, MODE_SIM):
            self.mode_combo.addItem(MODE_LABELS[mode], mode)
        self.mode_combo.setToolTip(
            "主节点:本机发送帧头并接收应答\n"
            "被动监听:只收不发,监控另一台主节点产生的报文\n"
            "仿真演示:内置仿真从节点,无需硬件即可试用"
        )
        dev_form.addRow("工作模式", self.mode_combo)

        self.break_combo = QComboBox()
        for key, text in BREAK_MODES:
            self.break_combo.addItem(text, key)
        self.break_combo.setToolTip(
            "BREAK 域(≥11 个位时间的低电平)的生成方式。\n"
            "控制位:直接拉低 UART 的 BREAK 引脚(FTDI/CP210x/CH340 支持);\n"
            "波特率切换:以 2/3 波特率发一个 0x00,低电平正好 13.5 个位时间。"
        )
        dev_form.addRow("BREAK", self.break_combo)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(3, 5000)
        self.timeout_spin.setValue(100)
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setToolTip("从节点应答超时时间")
        dev_form.addRow("应答超时", self.timeout_spin)

        layout.addWidget(dev_box)

        # --- LDF ---
        ldf_box = QGroupBox("总线描述文件(LDF)")
        ldf_form = QFormLayout(ldf_box)

        self.ldf_path = QLineEdit()
        self.ldf_path.setReadOnly(True)
        self.ldf_path.setPlaceholderText("未加载")
        ldf_form.addRow("文件", self.ldf_path)

        self.ldf_info = QLabel("-")
        self.ldf_info.setWordWrap(True)
        self.ldf_info.setStyleSheet("color:#555;")
        ldf_form.addRow("信息", self.ldf_info)

        btn_row = QHBoxLayout()
        load_btn = QPushButton("加载 LDF…")
        load_btn.clicked.connect(self._browse_ldf)
        btn_row.addWidget(load_btn)
        clear_btn = QPushButton("清除")
        clear_btn.clicked.connect(self.clear_ldf)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        ldf_form.addRow("", btn_row)

        layout.addWidget(ldf_box)

        # --- 日志 ---
        log_box = QGroupBox("日志记录")
        log_form = QFormLayout(log_box)

        self.log_check = QCheckBox("记录报文到 CSV")
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

        self.log_dir_label = QLabel(f"日志目录: {default_log_dir()}")
        self.log_dir_label.setStyleSheet("color:#555;")
        self.log_dir_label.setWordWrap(True)
        log_form.addRow("", self.log_dir_label)

        layout.addWidget(log_box)

        # --- 连接按钮 ---
        self.connect_btn = QPushButton("打开通道")
        self.connect_btn.setFixedHeight(32)
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        layout.addWidget(self.connect_btn)

        layout.addStretch(1)

    # ------------------------------------------------------------ 串口

    def refresh_ports(self) -> None:
        """重新枚举串口。"""
        current = self.port_combo.currentData()
        self._ports = list_serial_ports()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        if not self._ports:
            self.port_combo.addItem("未发现串口", "")
        else:
            for port in self._ports:
                self.port_combo.addItem(port.display, port.device)
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        self.port_combo.blockSignals(False)
        self._update_port_info()

    def _update_port_info(self) -> None:
        device = self.port_combo.currentData()
        for port in self._ports:
            if port.device == device:
                self.port_info.setText(port.describe())
                return
        self.port_info.setText("未发现串口。请确认 USB-LIN 适配器已接好并安装驱动。")

    # ------------------------------------------------------------ LDF

    def _browse_ldf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 LDF 文件", "", "LDF 文件 (*.ldf *.LDF);;所有文件 (*)"
        )
        if not path:
            return
        self.load_ldf(path)

    def load_ldf(self, path: str) -> bool:
        """加载并解析 LDF,失败时弹出提示。"""
        try:
            db = load_ldf(path)
        except LdfError as exc:
            self.ldf_info.setText(f"解析失败: {exc}")
            return False
        except Exception as exc:  # pragma: no cover - 兜底
            self.ldf_info.setText(f"解析失败: {exc}")
            return False

        self._ldf = db
        self.ldf_path.setText(path)
        self.ldf_info.setText(db.summary())
        self.ldfLoaded.emit(db)
        return True

    def clear_ldf(self) -> None:
        self._ldf = None
        self.ldf_path.clear()
        self.ldf_info.setText("-")
        self.ldfLoaded.emit(None)

    @property
    def ldf(self) -> Optional[LdfDatabase]:
        return self._ldf

    # ------------------------------------------------------------ 日志

    def _browse_log_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "选择日志文件", self.log_path_edit.text(), "CSV 文件 (*.csv)"
        )
        if path:
            self.log_path_edit.setText(path)
            self._emit_log_settings()

    def _emit_log_settings(self) -> None:
        self.logSettingsChanged.emit(
            self.log_check.isChecked(), self.log_path_edit.text().strip()
        )

    def log_config(self) -> tuple:
        """返回 (是否记录, 文件路径)。"""
        path = self.log_path_edit.text().strip()
        if path and not path.lower().endswith(".csv"):
            path = path + ".csv"
        return self.log_check.isChecked(), path

    # ------------------------------------------------------------ 连接

    def _on_connect_clicked(self) -> None:
        if self.connect_btn.text() == "打开通道":
            self.connectRequested.emit(self.settings())
        else:
            self.disconnectRequested.emit()

    def settings(self) -> ConnectionSettings:
        """当前界面上的连接参数。"""
        return ConnectionSettings(
            mode=self.mode_combo.currentData() or MODE_MASTER,
            port=self.port_combo.currentData() or "",
            baudrate=int(self.baud_combo.currentData() or 19200),
            break_mode=self.break_combo.currentData() or "auto",
            response_timeout_ms=float(self.timeout_spin.value()),
            log_enabled=self.log_check.isChecked(),
            log_path=self.log_path_edit.text().strip(),
        )

    def set_connected(self, connected: bool, text: str = "") -> None:
        """切换按钮状态。"""
        self.connect_btn.setText("关闭通道" if connected else "打开通道")
        self.port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)
        self.mode_combo.setEnabled(not connected)
        self.break_combo.setEnabled(not connected)
        if text:
            self.port_info.setText(text)
