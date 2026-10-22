"""主窗口:装配各面板并管理 CAN 工作线程的生命周期。"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDockWidget,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
)

from .. import APP_NAME, __version__
from ..logbook import LogBook
from ..worker import CanWorker
from .device_panel import DevicePanel
from .flash_panel import FlashPanel
from .message_panel import MessagePanel
from .monitor_panel import MonitorPanel
from .send_panel import SendPanel
from .stats_panel import StatsPanel

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  v{__version__}")
        self.resize(1360, 820)

        self._worker: Optional[CanWorker] = None

        self._build_ui()
        self._wire()

        self.message_panel.log("info", "程序就绪,请选择 PCAN 通道并打开")

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self) -> None:
        # --- 中央:报文监控 ---
        self.monitor_panel = MonitorPanel()
        self.setCentralWidget(self.monitor_panel)

        # --- 左侧:设备 + 发送 ---
        self.device_panel = DevicePanel()
        self.send_panel = SendPanel()
        left = QSplitter(Qt.Vertical)
        left.addWidget(self.device_panel)
        left.addWidget(self.send_panel)
        left.setStretchFactor(0, 0)
        left.setStretchFactor(1, 1)

        self.left_dock = QDockWidget("连接与发送", self)
        self.left_dock.setObjectName("left_dock")
        self.left_dock.setWidget(left)
        self.left_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_dock)

        # --- 右侧:统计 / UDS 刷写(同一区域用标签页切换) ---
        self.stats_panel = StatsPanel()
        self.flash_panel = FlashPanel()
        self.side_tabs = QTabWidget()
        self.side_tabs.setObjectName("side_tabs")
        self.side_tabs.addTab(self.stats_panel, "报文统计")
        self.side_tabs.addTab(self.flash_panel, "UDS 刷写")

        self.side_dock = QDockWidget("分析与刷写", self)
        self.side_dock.setObjectName("side_dock")
        self.side_dock.setWidget(self.side_tabs)
        self.addDockWidget(Qt.RightDockWidgetArea, self.side_dock)
        self.resizeDocks([self.side_dock], [620], Qt.Horizontal)

        # --- 底部:系统消息 ---
        self.message_panel = MessagePanel()
        self.message_dock = QDockWidget("系统消息", self)
        self.message_dock.setObjectName("message_dock")
        self.message_dock.setWidget(self.message_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.message_dock)

        self.left_dock.setMinimumWidth(360)
        self.message_dock.setMinimumHeight(120)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("未连接")

    def _wire(self) -> None:
        self.device_panel.connectRequested.connect(self._on_connect)
        self.device_panel.disconnectRequested.connect(self._on_disconnect)

        self.send_panel.sendRequested.connect(self._on_send)
        self.send_panel.cyclicRequested.connect(self._on_cyclic)
        self.send_panel.stopCyclicRequested.connect(self._on_stop_cyclic)

        self.monitor_panel.messageActivated.connect(self._load_into_send)

    # ------------------------------------------------------------ 连接管理

    def _on_connect(self, device, bitrate: int, fd: bool, data_bitrate: int) -> None:
        if self._worker is not None:
            self._on_disconnect()

        logbook: Optional[LogBook] = None
        try:
            enabled, csv_path, blf_path = self.device_panel.log_config()
            if enabled and (csv_path or blf_path):
                logbook = LogBook(csv_path=csv_path or None, blf_path=blf_path or None)
                self.message_panel.log(
                    "info", f"日志文件: {csv_path or '(无 CSV)'} , {blf_path or '(无 BLF)'}"
                )
        except Exception as exc:
            QMessageBox.critical(self, "日志创建失败", f"无法创建日志文件:\n{exc}")
            return

        worker = CanWorker(self)
        worker.open_bus(
            channel=device.channel,
            bitrate=bitrate,
            logbook=logbook,
            fd=fd,
            data_bitrate=data_bitrate,
        )
        worker.opened.connect(self._on_opened)
        worker.failed.connect(self._on_open_failed)
        worker.closed.connect(self._on_worker_closed)
        worker.batchReady.connect(self._on_batch)
        worker.statsReady.connect(self.stats_panel.apply_snapshot)
        worker.notify.connect(self.message_panel.log)

        self._worker = worker
        self.device_panel.set_connected(True, "正在打开…")
        self.statusBar().showMessage(f"正在打开 {device.channel}…")
        worker.start()

    def _on_disconnect(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self.statusBar().showMessage("正在关闭通道…")
        # 先唤醒所有等待中的请求,否则刷写线程会一直阻塞到超时
        if hasattr(worker, "wake_pending_requests"):
            worker.wake_pending_requests()
        worker.shutdown_bus()

        # worker 的循环最长阻塞只有一次 recv(5ms),正常几十毫秒就该退出。
        # 这里绝不用 terminate() 强杀 —— 线程可能正卡在 PCANBasic.dll 内部,
        # 强杀会直接踩坏驱动状态,表现为整个进程访问冲突闪退。
        if not worker.wait(15000):
            self.message_panel.log(
                "error", "CAN 工作线程 15 秒未退出。请检查是否有别的程序占用设备,然后重开程序。"
            )
        self._worker = None
        self._on_worker_closed()

    def _on_opened(self, info: str) -> None:
        self.device_panel.set_connected(True, f"已连接\n{info}")
        self.send_panel.set_connected(True)
        self.flash_panel.set_connected(True, self._worker)
        self.statusBar().showMessage(f"已连接 {info}")
        self.message_panel.log("info", f"通道已打开: {info}")

    def _on_open_failed(self, reason: str) -> None:
        self._worker = None
        self.device_panel.set_connected(False, "打开失败")
        self.send_panel.set_connected(False)
        self.statusBar().showMessage("打开失败")
        self.message_panel.log("error", f"通道打开失败: {reason}")
        QMessageBox.critical(
            self,
            "打开失败",
            f"无法打开 CAN 通道。\n\n原因: {reason}\n\n"
            "请检查:\n"
            "  1. PCAN 设备是否已插好,驱动是否安装(设备管理器中应无黄色感叹号)\n"
            "  2. 通道是否已被其它程序占用\n"
            "  3. 波特率是否与总线上其它节点一致",
        )

    def _stop_flash_thread(self, timeout_ms: int = 8000) -> bool:
        """停掉刷写线程。返回是否干净退出。

        必须确保线程真的结束了再让它析构 —— QThread 在运行中被销毁会让
        Qt 直接 abort() 整个进程(表现为闪退,没有任何 Python 异常)。
        """
        thread = self.flash_panel._thread
        if thread is None:
            return True
        if not thread.isRunning():
            self.flash_panel._thread = None
            return True

        self.flash_panel.stop()
        if thread.wait(timeout_ms):
            self.flash_panel._thread = None
            return True

        # 走到这里说明线程卡在等待上。terminate() 是最后手段:
        # 刷写线程只在 Python 层等待(队列取数),强制终止相对安全,
        # 但仍要确认它真的结束了 —— 运行中的 QThread 被析构会让 Qt 直接 abort。
        self.message_panel.log("warn", "刷写线程未在超时内退出,强制终止")
        thread.terminate()
        thread.wait(5000)
        if thread.isRunning():
            self.message_panel.log("error", "刷写线程仍未退出,关闭时可能异常")
            return False
        self.flash_panel._thread = None
        return True

    def _on_worker_closed(self) -> None:
        self.device_panel.set_connected(False, "已关闭")
        self.send_panel.set_connected(False)
        self.send_panel.on_cyclic_stopped()
        # 先停掉刷写线程,再断开总线
        self._stop_flash_thread()
        self.flash_panel.set_connected(False)
        self.statusBar().showMessage("未连接")

    # ------------------------------------------------------------ 收发转发

    def _on_send(self, msg) -> None:
        if self._worker is not None:
            self._worker.send(msg)

    def _on_cyclic(self, interval_ms: float, msg, repeat: int) -> None:
        if self._worker is not None:
            self._worker.start_cyclic(interval_ms, msg, repeat)

    def _on_stop_cyclic(self) -> None:
        if self._worker is not None:
            self._worker.stop_all_cyclic()
        self.send_panel.on_cyclic_stopped()

    def _on_batch(self, messages: list) -> None:
        """工作线程推送的一批报文(收发混合)。"""
        self.monitor_panel.append_messages(messages)
        for msg in messages:
            if not msg.is_rx:
                self.send_panel.add_history(msg)

    def _load_into_send(self, msg) -> None:
        """把监控表里选中/双击的报文填回发送区,方便改参数重发。"""
        panel = self.send_panel
        panel.id_edit.setText(f"0x{msg.arbitration_id:X}")
        panel.ext_check.setChecked(msg.is_extended_id)
        panel.rtr_check.setChecked(msg.is_remote_frame)
        if msg.data:
            panel.data_edit.setText(" ".join(f"{b:02X}" for b in msg.data))
        panel.cyclic_state.setText(
            f"已载入 0x{msg.arbitration_id:X} ({'接收' if msg.is_rx else '发送'})"
        )
        self.message_panel.log("info", f"已载入报文 0x{msg.arbitration_id:X} 到发送区")

    # ------------------------------------------------------------ 退出

    def closeEvent(self, event) -> None:
        # 先停刷写,再断总线;顺序反了会让刷写线程还在用总线就被抽走
        self._stop_flash_thread()
        self._on_disconnect()
        super().closeEvent(event)