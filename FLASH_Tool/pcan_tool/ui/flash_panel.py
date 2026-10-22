"""UDS 下载(刷写)面板:读取 hex 文件并按 0x34/0x36/0x37 流程传输。"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..hexfile import HexFileError, HexImage, parse_intel_hex
from ..isotp import CONSECUTIVE_FRAME_DATA, FIRST_FRAME_DATA
from ..uds import MAX_BLOCK_DATA
from ..uds import (
    CanWorkerTransport,
    UdsConfig,
    UdsDownloader,
    UdsError,
    UdsProgress,
)

# 常用块大小预设(每次 0x36 携带的数据字节数)
BLOCK_PRESETS = [0x3F8, 0x1F4, 0xFA, 64, 32, 16, 8]

SESSION_PRESETS = [
    ("不发送", b""),
    ("编程会话 10 02", bytes([0x10, 0x02])),
    ("扩展会话 10 03", bytes([0x10, 0x03])),
    ("编程会话 + 扩展会话", bytes([0x10, 0x03, 0x10, 0x02])),
]


def _parse_hex_list(text: str) -> List[bytes]:
    """把 "10 02, 27 01 12 34" 解析成 [b'\\x10\\x02', b'\\x27\\x01\\x12\\x34']。"""
    out: List[bytes] = []
    for chunk in text.replace(";", ",").replace("\n", ",").split(","):
        chunk = "".join(chunk.split())
        if not chunk:
            continue
        try:
            out.append(bytes.fromhex(chunk))
        except ValueError as exc:
            raise ValueError(f"非法的十六进制: {chunk!r}({exc})") from exc
    return out


class _FlashThread(QThread):
    """在后台线程里跑 UDS 下载。

    进度与日志只经由信号回到界面线程(Qt 自动排队),不在后台线程碰任何控件。
    """

    progress = pyqtSignal(object)   # UdsProgress
    logline = pyqtSignal(str, str)  # level, text
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, panel, config, image, worker, parent=None) -> None:
        super().__init__(parent)
        transport = CanWorkerTransport(worker, config.tx_id, config.rx_id,
                                       config.extended_id)
        # on_progress / on_log 挂的是**本线程对象的方法**,回调里只 emit 信号。
        # 绝不能把操作 QWidget 的函数当回调传进来 —— 那等于在后台线程改界面,
        # Qt 不保证线程安全,高速刷写时表现为整个进程闪退(0xC0000409)。
        self._downloader = UdsDownloader(
            image=image,
            config=config,
            transport=transport,
            on_progress=self._emit_progress,
            on_log=self._emit_log,
        )

    def _emit_progress(self, p: UdsProgress) -> None:
        self.progress.emit(p)

    def _emit_log(self, level: str, text: str) -> None:
        self.logline.emit(level, text)

    def run(self) -> None:
        try:
            self._downloader.run()
        except UdsError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # 兜底,避免线程静默死掉
            import traceback

            self.logline.emit("error", traceback.format_exc())
            self.failed.emit(f"未预期的错误: {exc}")
        else:
            self.finished_ok.emit()

    def abort(self) -> None:
        self._downloader.abort()


class FlashPanel(QWidget):
    """UDS 诊断下载面板。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image: Optional[HexImage] = None
        self._thread: Optional[_FlashThread] = None
        self._worker = None
        self._connected = False
        self._actual_block = 0   # 实际生效的块大小(0 = 尚未确定)
        self._build()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        layout.addWidget(self._build_file_box())
        layout.addWidget(self._build_param_box())

        # ---- 进度 ----
        prog_box = QGroupBox("进度")
        prog_layout = QVBoxLayout(prog_box)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setFormat("%p%")
        prog_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        prog_layout.addWidget(self.status_label)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("color:#555;")
        self.detail_label.setWordWrap(True)
        prog_layout.addWidget(self.detail_label)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("开始下载")
        self.start_btn.setMinimumHeight(34)
        self.start_btn.setStyleSheet("QPushButton{font-weight:bold;}")
        self.start_btn.clicked.connect(self.start)
        btn_row.addWidget(self.start_btn)

        self.stop_btn = QPushButton("中止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        btn_row.addWidget(self.stop_btn)
        prog_layout.addLayout(btn_row)
        layout.addWidget(prog_box)

        # ---- 日志 ----
        log_box = QGroupBox("刷写日志")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(150)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_box)

        layout.addStretch(1)
        self._sync_buttons()

    def _build_file_box(self) -> QGroupBox:
        box = QGroupBox("固件文件 (Intel HEX)")
        form = QFormLayout(box)

        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("选择要烧写的 .hex 文件")
        row.addWidget(self.file_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addRow("文件", row)

        self.file_info = QLabel("未选择文件")
        self.file_info.setWordWrap(True)
        self.file_info.setStyleSheet("color:#555;")
        form.addRow("解析结果", self.file_info)

        self.address_override = QLineEdit("")
        self.address_override.setPlaceholderText("留空则使用 hex 内的地址")
        form.addRow("地址覆盖", self.address_override)

        return box

    def _build_param_box(self) -> QGroupBox:
        box = QGroupBox("UDS / ISO-TP 参数")
        form = QFormLayout(box)

        id_row = QHBoxLayout()
        # 默认诊断 ID:请求 0x737,响应 0x736(与 UdsConfig 保持一致)
        self.tx_id_edit = QLineEdit("0x737")
        self.rx_id_edit = QLineEdit("0x736")
        id_row.addWidget(QLabel("发送"))
        id_row.addWidget(self.tx_id_edit)
        id_row.addWidget(QLabel("接收"))
        id_row.addWidget(self.rx_id_edit)
        self.extended_check = QCheckBox("扩展 ID")
        id_row.addWidget(self.extended_check)
        form.addRow("CAN ID", id_row)

        block_row = QHBoxLayout()
        self.block_combo = QComboBox()
        for value in BLOCK_PRESETS:
            self.block_combo.addItem(f"{value} 字节 (0x{value:X})", value)
        self.block_combo.setCurrentIndex(0)
        self.block_combo.setEditable(True)
        self.block_combo.currentIndexChanged.connect(lambda _: self._on_block_changed())
        block_row.addWidget(self.block_combo, 1)

        self.block_custom = QSpinBox()
        # 上限放到 8192:让 4093 以上的值能填进来,由软件给出明确的越界提示,
        # 而不是被 SpinBox 静默夹成一个"看起来合法"的数
        self.block_custom.setRange(1, 8192)
        self.block_custom.setValue(0x3F8)
        self.block_custom.valueChanged.connect(self._on_custom_block_changed)
        block_row.addWidget(QLabel("自定义"))
        block_row.addWidget(self.block_custom)
        form.addRow("块大小", block_row)

        self.follow_ecu_check = QCheckBox("自动采用 ECU 在 0x34 响应中声明的块大小")
        self.follow_ecu_check.setChecked(True)
        self.follow_ecu_check.setToolTip(
            "选中后忽略下面的手动设置,直接用 ECU 返回的 maxNumberOfBlockLength"
        )
        self.follow_ecu_check.toggled.connect(self._on_follow_ecu_toggled)
        form.addRow("", self.follow_ecu_check)

        self.ecu_header_check = QCheckBox(
            "ECU 的块长度包含 0x36 服务头与块序号(共 2 字节)"
        )
        self.ecu_header_check.setChecked(True)
        self.ecu_header_check.setToolTip(
            "勾选:每块数据 = ECU 声明值 - 2\n"
            "不勾:每块数据 = ECU 声明值\n"
            "不确定就勾选,若 ECU 返回 0x13/0x31 可尝试取消"
        )
        form.addRow("", self.ecu_header_check)

        self.block_hint = QLabel("")
        self.block_hint.setStyleSheet("color:#555;")
        self.block_hint.setWordWrap(True)
        form.addRow("", self.block_hint)

        fmt_row = QHBoxLayout()
        self.addr_bytes = QComboBox()
        self.addr_bytes.addItems([str(n) for n in (2, 3, 4)])
        self.addr_bytes.setCurrentText("4")
        self.size_bytes = QComboBox()
        self.size_bytes.addItems([str(n) for n in (1, 2, 4)])
        self.size_bytes.setCurrentText("4")
        fmt_row.addWidget(QLabel("地址"))
        fmt_row.addWidget(self.addr_bytes)
        fmt_row.addWidget(QLabel("长度"))
        fmt_row.addWidget(self.size_bytes)
        fmt_row.addStretch(1)
        form.addRow("0x34 格式", fmt_row)

        self.session_combo = QComboBox()
        for label, _ in SESSION_PRESETS:
            self.session_combo.addItem(label)
        form.addRow("前置命令", self.session_combo)

        self.pre_edit = QLineEdit("")
        self.pre_edit.setPlaceholderText("可选,例如 27 01 12 34(安全解锁)")
        form.addRow("附加前置", self.pre_edit)

        t_row = QHBoxLayout()
        self.p2_spin = QDoubleSpinBox()
        self.p2_spin.setRange(0.05, 60.0)
        self.p2_spin.setValue(1.0)
        self.p2_spin.setSuffix(" s")
        self.s3_spin = QDoubleSpinBox()
        self.s3_spin.setRange(0.0, 5000.0)
        self.s3_spin.setValue(0.0)
        self.s3_spin.setDecimals(0)
        self.s3_spin.setSuffix(" ms")
        t_row.addWidget(QLabel("P2"))
        t_row.addWidget(self.p2_spin)
        t_row.addWidget(QLabel("S3"))
        t_row.addWidget(self.s3_spin)
        t_row.addStretch(1)
        form.addRow("超时/间隔", t_row)

        return box

    # ------------------------------------------------------------ 文件

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Intel HEX 文件", "", "Intel HEX 文件 (*.hex *.ihx);;所有文件 (*.*)"
        )
        if not path:
            return
        self.file_edit.setText(path)
        self.load_file(path)

    def load_file(self, path: str) -> bool:
        """解析 hex 文件并刷新界面。"""
        try:
            image = parse_intel_hex(path)
        except (HexFileError, OSError) as exc:
            self._image = None
            self.file_info.setText(f"解析失败: {exc}")
            self._sync_buttons()
            return False

        self._image = image
        self.file_info.setText(image.summary())
        if image.has_gaps:
            self.log("warn", f"文件含 {len(image.gaps)} 处地址空洞,已用 0xFF 填充")
        self.log("info", f"已加载 {path}")
        self._update_block_hint()
        self._sync_buttons()
        return True

    # ------------------------------------------------------------ 块大小

    def _on_block_changed(self) -> None:
        data = self.block_combo.currentData()
        if data is not None:
            self.block_custom.blockSignals(True)
            self.block_custom.setValue(int(data))
            self.block_custom.blockSignals(False)
        self._update_block_hint()

    def _on_custom_block_changed(self) -> None:
        self._update_block_hint()

    def _on_follow_ecu_toggled(self, _checked: bool) -> None:
        follow = self.follow_ecu_check.isChecked()
        # 跟随 ECU 时手动块大小就没意义了,置灰避免误解
        self.block_combo.setEnabled(not follow and self._thread is None)
        self.block_custom.setEnabled(not follow and self._thread is None)
        self._update_block_hint()

    def _current_block_size(self) -> int:
        if self.block_combo.isEditable() and self.block_combo.currentText():
            text = self.block_combo.currentText().strip()
            try:
                return max(1, int(text, 0))
            except ValueError:
                pass
        return max(1, int(self.block_custom.value()))

    def _update_block_hint(self) -> None:
        block = self._current_block_size()
        # 0x36 + 块序号 + 数据 > 4095 就超出 ISO-TP 单消息上限
        max_payload = 4095
        max_block = max_payload - 2
        if block > max_block:
            self.block_hint.setText(
                f"当前 {block} 字节:加上 0x36 与块序号后为 {block + 2} 字节,"
                f"超过 ISO-TP 单消息上限 {max_payload} 字节,无法发送"
            )
            self.block_hint.setStyleSheet("color:#c62828;")
        else:
            payload = block + 2  # 0x36 + 块序号
            if payload <= 7:
                frames = 1
            else:
                rest = payload - FIRST_FRAME_DATA
                frames = 1 + (rest + CONSECUTIVE_FRAME_DATA - 1) // CONSECUTIVE_FRAME_DATA
            self.block_hint.setText(
                f"每块 {block} 字节数据 -> 每块 {frames} 个 CAN 帧"
                f"(ISO-TP 载荷 {payload} 字节 = 首帧 6 + 连续帧 {frames - 1}x7)"
            )
            self.block_hint.setStyleSheet("color:#555;")
        if self.follow_ecu_check.isChecked():
            head = ("跟随 ECU:块大小将在 0x34 响应后按 ECU 声明值自动确定\n"
                    f"      (声明值若含服务头,实际数据 = 声明值 - 2;上限 {MAX_BLOCK_DATA} 字节)")
        else:
            head = ""
        text = self.block_hint.text()
        if self._image is not None:
            total = self._image.length
            text += f" / 共 {total // block} 块"
        self.block_hint.setText((head + text) if head else text)

    # ------------------------------------------------------------ 启动/中止

    def set_connected(self, connected: bool, worker=None) -> None:
        """通道开关时调用。worker 为打开的 CanWorker,没有则视为未连接。"""
        self._connected = bool(connected and worker is not None)
        self._worker = worker if self._connected else None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        running = self._thread is not None and self._thread.isRunning()
        self.start_btn.setEnabled(self._connected and self._image is not None and not running)
        self.stop_btn.setEnabled(running)
        self.file_edit.setEnabled(not running)
        self.block_combo.setEnabled(not running)

    def _build_config(self) -> tuple:
        tx = int(self.tx_id_edit.text().strip(), 0)
        rx = int(self.rx_id_edit.text().strip(), 0)

        pre: List[bytes] = []
        _, preset = SESSION_PRESETS[self.session_combo.currentIndex()]
        if preset:
            # 前置命令里的两条记录要拆成两条独立请求
            if preset == bytes([0x10, 0x03, 0x10, 0x02]):
                pre.extend([bytes([0x10, 0x03]), bytes([0x10, 0x02])])
            else:
                pre.append(preset)
        if self.pre_edit.text().strip():
            pre.extend(_parse_hex_list(self.pre_edit.text()))

        image = self._image
        if image is not None and self.address_override.text().strip():
            addr = int(self.address_override.text().strip(), 0)
            if addr != image.base_address:
                image = HexImage(
                    data=image.data,
                    base_address=addr,
                    record_count=image.record_count,
                    data_bytes=image.data_bytes,
                )

        return UdsConfig(
            tx_id=tx,
            rx_id=rx,
            extended_id=self.extended_check.isChecked(),
            address_bytes=int(self.addr_bytes.currentText()),
            size_bytes=int(self.size_bytes.currentText()),
            block_size=self._current_block_size(),
            follow_ecu_block_size=self.follow_ecu_check.isChecked(),
            ecu_block_len_includes_header=self.ecu_header_check.isChecked(),
            p2_timeout=self.p2_spin.value(),
            s3_timeout=self.s3_spin.value() / 1000.0,
            pre_commands=pre,
        ), image

    def start(self) -> None:
        """开始下载(需要先打开 CAN 通道)。"""
        worker = self._worker
        if self._thread is not None and self._thread.isRunning():
            return
        if self._image is None:
            self.log("error", "请先选择有效的 .hex 文件")
            return
        if worker is None:
            self.log("error", "请先在左侧打开 CAN 通道")
            return

        try:
            config, image = self._build_config()
        except ValueError as exc:
            self.log("error", f"参数有误: {exc}")
            return

        block = config.block_size
        if block + 2 > 4095:
            self.log("error", f"块大小 {block} 字节过大,加上 0x36 与块序号会超过 4095")
            return

        self.progress_bar.setValue(0)
        self.detail_label.setText("")
        self.status_label.setText("准备中…")
        self._actual_block = 0

        thread = _FlashThread(self, config, image, worker)
        thread.finished_ok.connect(self._on_done)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(self._on_thread_finished)
        thread.progress.connect(self._on_progress)
        thread.logline.connect(self.log)

        self._thread = thread
        self._sync_buttons()
        self.log(
            "info",
            f"开始下载 {image.length:,} 字节 @ 0x{image.base_address:08X},"
            f"块大小 {block} 字节,ID 0x{config.tx_id:X}->0x{config.rx_id:X}",
        )
        thread.start()

    def stop(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.log("warn", "正在中止…")
            self.status_label.setText("正在中止…")
            self._thread.abort()

    # ------------------------------------------------------------ 回调

    def _on_progress(self, p: UdsProgress) -> None:
        # downloader 在拿到 0x34 响应后才知道最终块大小
        if self._thread is not None:
            dl = self._thread._downloader
            if dl.effective_block_size:
                self._actual_block = dl.effective_block_size
        self.progress_bar.setValue(int(p.percent * 10))
        self.status_label.setText(
            f"{p.phase} — 块 {p.block_index}/{p.block_total}"
            if p.block_total
            else p.phase
        )
        self.detail_label.setText(
            f"{p.sent_bytes:,} / {p.total_bytes:,} 字节 ({p.percent:.1f}%)"
            + (f" @ {p.detail}" if p.detail else "")
            + (f" | 每块 {self._actual_block} 字节" if self._actual_block else "")
        )

    def _on_done(self) -> None:
        self.progress_bar.setValue(1000)
        self.status_label.setText("下载完成")
        self.log("info", "=== 流程结束:成功 ===")

    def _on_failed(self, reason: str) -> None:
        self.status_label.setText(f"失败: {reason}")
        self.log("error", f"=== 流程结束:失败 — {reason} ===")

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._sync_buttons()
        self._on_follow_ecu_toggled(self.follow_ecu_check.isChecked())

    def log(self, level: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        colors = {"info": "#1b5e20", "warn": "#e65100", "error": "#c62828"}
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log_view.appendHtml(
            f'<span style="color:{colors.get(level, "#000")};">[{stamp}] {safe}</span>'
        )
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )