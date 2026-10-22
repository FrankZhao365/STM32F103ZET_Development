"""主窗口:装配各面板并管理 LIN 工作线程的生命周期。"""

from __future__ import annotations

import logging
from typing import List, Optional

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
from ..ldf import LdfDatabase
from ..logbook import LogBook
from ..protocol import FrameSpec, LinFrame
from ..transport import SerialLinTransport, SimulatedLinTransport
from ..worker import (
    MODE_LABELS,
    MODE_MASTER,
    MODE_SIM,
    MODE_SNIFF,
    LinWorker,
    WorkerConfig,
)
from .device_panel import ConnectionSettings, DevicePanel
from .frame_panel import FramePanel
from .ldf_panel import LdfPanel
from .message_panel import MessagePanel
from .monitor_panel import MonitorPanel
from .stats_panel import StatsPanel

log = logging.getLogger(__name__)

#: 仿真模式下的演示帧(没有 LDF 也能直接试)
DEMO_SPECS = [
    FrameSpec(0x11, 8, "Motor_1", delay_ms=10),
    FrameSpec(0x12, 4, "Motor_2", delay_ms=10),
    FrameSpec(0x13, 2, "Status", delay_ms=10),
]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  v{__version__}")
        self.resize(1400, 840)

        self._worker: Optional[LinWorker] = None
        self._logbook: Optional[LogBook] = None
        self._ldf: Optional[LdfDatabase] = None

        self._build_ui()
        self._wire()

        self._load_demo_specs()
        self.message_panel.log("info", "程序就绪。可直接点“打开通道”用仿真模式试用,或选择串口接真实 LIN 总线。")

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self) -> None:
        # --- 中央:报文监控 ---
        self.monitor_panel = MonitorPanel()
        self.setCentralWidget(self.monitor_panel)

        # --- 左侧:设备连接 ---
        self.device_panel = DevicePanel()
        self.left_dock = QDockWidget("连接设置", self)
        self.left_dock.setObjectName("left_dock")
        self.left_dock.setWidget(self.device_panel)
        self.left_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_dock)

        # --- 右侧:解析 / 统计 / LDF ---
        self.frame_panel = FramePanel()
        self.stats_panel = StatsPanel()
        self.ldf_panel = LdfPanel()
        self.side_tabs = QTabWidget()
        self.side_tabs.setObjectName("side_tabs")
        self.side_tabs.addTab(self.frame_panel, "报文解析")
        self.side_tabs.addTab(self.stats_panel, "报文统计")
        self.side_tabs.addTab(self.ldf_panel, "LDF / 轮询")

        self.side_dock = QDockWidget("分析与配置", self)
        self.side_dock.setObjectName("side_dock")
        self.side_dock.setWidget(self.side_tabs)
        self.addDockWidget(Qt.RightDockWidgetArea, self.side_dock)
        self.resizeDocks([self.side_dock], [640], Qt.Horizontal)

        # --- 底部:系统消息 ---
        self.message_panel = MessagePanel()
        self.message_dock = QDockWidget("系统消息", self)
        self.message_dock.setObjectName("message_dock")
        self.message_dock.setWidget(self.message_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.message_dock)

        self.left_dock.setMinimumWidth(330)
        self.message_dock.setMinimumHeight(120)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("未连接")

    def _wire(self) -> None:
        self.device_panel.connectRequested.connect(self._on_connect)
        self.device_panel.disconnectRequested.connect(self._on_disconnect)
        self.device_panel.ldfLoaded.connect(self._on_ldf_loaded)
        self.monitor_panel.frameActivated.connect(self._on_frame_activated)

    # ------------------------------------------------------------ 演示数据

    def _load_demo_specs(self) -> None:
        """启动时先放几个演示帧,方便直接用仿真模式试。"""
        for spec in DEMO_SPECS:
            self.ldf_panel.add_spec(spec)

    def _on_ldf_loaded(self, ldb: Optional[LdfDatabase]) -> None:
        """LDF 载入/清除。"""
        self._ldf = ldb
        self.ldf_panel.set_ldf(ldb)
        self.frame_panel.set_ldf(ldb)
        if ldb is None:
            self.message_panel.log("info", "已清除 LDF")
            return
        self.message_panel.log("info", f"已加载 LDF: {ldb.summary()}")
        # 自动把第一个调度表填进轮询列表
        if ldb.schedules:
            self.ldf_panel.fill_from_schedule()

    def _on_frame_activated(self, frame: LinFrame) -> None:
        self.frame_panel.show_frame(frame)
        self.side_tabs.setCurrentWidget(self.frame_panel)

    # ------------------------------------------------------------ 连接管理

    def _on_connect(self, settings: ConnectionSettings) -> None:
        if self._worker is not None:
            self._on_disconnect()

        specs = self.ldf_panel.specs()
        if settings.mode in (MODE_MASTER, MODE_SIM) and not specs:
            QMessageBox.warning(
                self,
                "没有轮询帧",
                "主节点模式需要先在右侧“LDF / 轮询”页里添加要轮询的帧。",
            )
            return

        transport = self._make_transport(settings)
        if transport is None:
            return

        logbook: Optional[LogBook] = None
        try:
            enabled, csv_path = self.device_panel.log_config()
            if enabled and csv_path:
                logbook = LogBook(csv_path=csv_path)
                self.message_panel.log("info", f"日志文件: {csv_path}")
        except Exception as exc:
            QMessageBox.critical(self, "日志创建失败", f"无法创建日志文件:\n{exc}")
            return

        worker = LinWorker(self)
        worker.configure(
            WorkerConfig(
                mode=settings.mode,
                transport=transport,
                specs=specs,
                response_timeout_ms=settings.response_timeout_ms,
                logbook=logbook,
                name_lookup=self._name_lookup,
                length_lookup=self._length_lookup,
            )
        )
        worker.opened.connect(self._on_opened)
        worker.failed.connect(self._on_failed)
        worker.closed.connect(self._on_worker_closed)
        worker.batchReady.connect(self._on_batch)
        worker.statsReady.connect(self.stats_panel.apply_snapshot)
        worker.notify.connect(self.message_panel.log)

        self._worker = worker
        self._logbook = logbook
        self.device_panel.set_connected(True, "正在打开…")
        self.statusBar().showMessage(
            f"正在打开 {MODE_LABELS.get(settings.mode, settings.mode)}…"
        )
        worker.start()

    def _make_transport(self, settings: ConnectionSettings):
        """按工作模式建立传输通道。"""
        if settings.mode == MODE_SIM:
            transport = SimulatedLinTransport()
            transport.set_counter_payload(8)
            return transport

        if not settings.port:
            QMessageBox.warning(
                self,
                "没有选择串口",
                "请先在左侧选择 LIN 适配器对应的串口,或改用“仿真演示”模式。",
            )
            return None

        transport = SerialLinTransport(
            port=settings.port,
            baudrate=settings.baudrate,
            break_mode=settings.break_mode,
        )
        return transport

    def _on_disconnect(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        self.statusBar().showMessage("正在关闭…")
        worker.request_stop()
        worker.wait(3000)
        if self._logbook is not None:
            self._logbook.close()
            self._logbook = None
        self.device_panel.set_connected(False)
        self.statusBar().showMessage("未连接")
        self.message_panel.log("info", "通道已关闭")

    def _on_opened(self, text: str) -> None:
        self.device_panel.set_connected(True, text)
        self.statusBar().showMessage(text)
        self.message_panel.log("info", f"通道已打开: {text}")

    def _on_failed(self, text: str) -> None:
        self._worker = None
        self.device_panel.set_connected(False)
        self.statusBar().showMessage("打开失败")
        self.message_panel.log("error", f"打开失败: {text}")
        QMessageBox.critical(self, "打开通道失败", text)

    def _on_worker_closed(self) -> None:
        if self._logbook is not None:
            self._logbook.close()
            self._logbook = None
        self.device_panel.set_connected(False)
        if self._worker is not None:
            self._worker = None
        self.statusBar().showMessage("未连接")

    def _on_batch(self, frames: List[LinFrame]) -> None:
        self.monitor_panel.append_frames(frames)

    # ------------------------------------------------------------ LDF 查询

    def _name_lookup(self, frame_id: int) -> str:
        if self._ldf is None:
            return ""
        frame = self._ldf.frame_by_id(frame_id)
        return frame.name if frame else ""

    def _length_lookup(self, frame_id: int) -> Optional[int]:
        if self._ldf is None:
            return None
        frame = self._ldf.frame_by_id(frame_id)
        return frame.size if frame else None

    # ------------------------------------------------------------ 退出

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if self._worker is not None:
            self._worker.request_stop()
            self._worker.wait(3000)
            self._worker = None
        if self._logbook is not None:
            self._logbook.close()
            self._logbook = None
        super().closeEvent(event)
