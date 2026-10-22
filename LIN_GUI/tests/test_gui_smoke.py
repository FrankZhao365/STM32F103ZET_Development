"""GUI 冒烟测试:离屏启动主窗口,校验面板装配与 LDF 联动。"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from lin_tool.ldf import load_ldf
from lin_tool.protocol import LinFrame
from lin_tool.ui.main_window import MainWindow

LDF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ldf")


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication(sys.argv)
    yield instance


@pytest.fixture(scope="module")
def window(app):
    win = MainWindow()
    yield win
    win.close()


def test_main_window_assembly(window):
    assert window.windowTitle().startswith("LIN")
    assert window.monitor_panel.table.model() is not None
    assert window.frame_panel.table.model() is not None
    assert window.stats_panel.table.model() is not None
    assert window.ldf_panel.poll_table.columnCount() == 6


def test_demo_specs_loaded(window):
    """启动时应预置几个演示帧,方便直接用仿真模式试。"""
    specs = window.ldf_panel.specs()
    assert specs
    assert specs[0].frame_id == 0x11


def test_ldf_load_populates_panels(window):
    """加载 LDF 后:帧列表、轮询列表、信号解码都要跟上。"""
    db = load_ldf(os.path.join(LDF_DIR, "example_21.ldf"))
    window._on_ldf_loaded(db)

    assert window.ldf_panel.frame_table.rowCount() == len(db.frames)
    specs = window.ldf_panel.specs()
    assert [s.frame_id for s in specs] == [0x11, 0x12]

    frame = LinFrame(
        timestamp=0.0,
        frame_id=0x11,
        data=bytes([100, 1]),
        direction="RX",
        checksum=0,
        checksum_valid=True,
    )
    window._on_frame_activated(frame)
    assert window.frame_panel._model.rowCount() == 2


def test_device_panel_settings(window):
    settings = window.device_panel.settings()
    assert settings.mode in ("master", "sniff", "sim")
    assert settings.baudrate > 0
    assert settings.response_timeout_ms >= 3


def test_worker_starts_in_sim_mode(window, app):
    """仿真模式下应该能真正跑起来并收到帧。"""
    from lin_tool.worker import MODE_SIM, LinWorker, WorkerConfig
    from lin_tool.transport import SimulatedLinTransport

    transport = SimulatedLinTransport()
    transport.set_counter_payload(8)
    worker = LinWorker()
    worker.configure(
        WorkerConfig(
            mode=MODE_SIM,
            transport=transport,
            specs=window.ldf_panel.specs(),
        )
    )
    received = []
    worker.batchReady.connect(lambda batch: received.extend(batch))
    worker.start()

    end = __import__("time").monotonic() + 0.4
    while __import__("time").monotonic() < end:
        app.processEvents()
        __import__("time").sleep(0.005)

    worker.request_stop()
    worker.wait(3000)
    app.processEvents()

    assert received
    assert all(frame.checksum_valid for frame in received)


def test_monitor_follows_new_frames_until_user_scrolls_up(app):
    """默认跟随新报文；向上滚动暂停；回到底部恢复跟随。"""
    from lin_tool.protocol import LinFrame

    window = MainWindow()
    monitor = window.monitor_panel
    monitor.resize(900, 400)
    monitor.show()
    app.processEvents()

    def frame(index):
        return LinFrame(
            timestamp=float(index + 1),
            frame_id=index & 0x3F,
            data=b"\x01",
        )

    monitor.append_frames([frame(i) for i in range(120)])
    app.processEvents()
    bar = monitor.table.verticalScrollBar()
    assert monitor.follow_check.isChecked()
    assert bar.value() == bar.maximum()

    # 手动回看历史后，新数据到来不应把视图拖回最后一行。
    bar.setValue(0)
    app.processEvents()
    assert not monitor.follow_check.isChecked()
    monitor.append_frames([frame(120)])
    app.processEvents()
    assert bar.value() == 0

    # 手动回到底部后继续跟随新增报文。
    bar.setValue(bar.maximum())
    app.processEvents()
    assert monitor.follow_check.isChecked()
    monitor.append_frames([frame(121)])
    app.processEvents()
    assert bar.value() == bar.maximum()
    monitor.close()
    window.close()


def test_monitor_pause_freezes_display_but_buffers_latest_frames(app):
    """暂停只冻结显示；新帧计数继续，恢复后补上缓存帧且不重复计数。"""
    window = MainWindow()
    monitor = window.monitor_panel
    monitor.resize(900, 400)
    monitor.show()
    app.processEvents()

    def frame(index):
        return LinFrame(
            timestamp=float(index + 1),
            frame_id=index & 0x3F,
            data=bytes([index & 0xFF]),
        )

    monitor.append_frames([frame(i) for i in range(10)])
    app.processEvents()
    original_rows = monitor._model.rowCount()
    original_total = monitor._model.rx_total + monitor._model.tx_total

    monitor.pause_button.click()
    monitor.append_frames([frame(i) for i in range(10, 15)])
    app.processEvents()

    assert monitor._model.rowCount() == original_rows
    assert monitor._model.rx_total + monitor._model.tx_total == original_total + 5
    assert len(monitor._pending_frames) == 5
    assert monitor.pause_button.text() == "继续 log"
    assert "缓存 5 帧" in monitor.summary.text()

    monitor.pause_button.click()
    app.processEvents()
    assert monitor._model.rowCount() == original_rows + 5
    assert monitor._model.rx_total + monitor._model.tx_total == original_total + 5
    assert not monitor._pending_frames
    assert monitor.pause_button.text() == "暂停 log"
    window.close()


def test_monitor_pause_buffer_is_bounded_by_max_rows(app):
    """长时间暂停时只缓存表格上限内的最新报文。"""
    window = MainWindow()
    monitor = window.monitor_panel
    monitor.max_rows_spin.setValue(100)
    monitor.pause_button.click()
    frames = [
        LinFrame(timestamp=float(i + 1), frame_id=i & 0x3F, data=b"")
        for i in range(150)
    ]
    monitor.append_frames(frames)

    assert len(monitor._pending_frames) == 100
    assert monitor._paused_dropped == 50
    assert "已丢弃较早的 50 帧" in monitor.summary.text()

    monitor.pause_button.click()
    assert monitor._model.rowCount() == 100
    assert monitor._model.rx_total == 150
    assert monitor._model.frame_at(0).timestamp == 51.0
    window.close()
