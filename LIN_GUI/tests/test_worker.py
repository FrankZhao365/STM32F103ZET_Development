"""工作线程端到端测试:主节点轮询、回显处理、被动监听、CSV 落盘。"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from lin_tool.logbook import LogBook
from lin_tool.protocol import (
    SYNC_BYTE,
    ChecksumType,
    FrameSpec,
    protected_id,
)
from lin_tool.transport import SimulatedLinTransport
from lin_tool.worker import MODE_MASTER, MODE_SNIFF, LinWorker, WorkerConfig

# 统一用 QApplication(离屏),避免与 GUI 冒烟测试里的 QApplication 冲突
APP = QApplication.instance() or QApplication(sys.argv)


def pump(seconds: float) -> None:
    """跑事件循环,让跨线程信号能投递。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.005)


class EchoTransport(SimulatedLinTransport):
    """模拟"主节点能收到自己发出的帧头"的真实串口行为。"""

    def send_header(self, frame_id: int) -> None:
        super().send_header(frame_id)
        pid = protected_id(frame_id)
        self._rx[0:0] = b"\x00" + bytes([SYNC_BYTE, pid])


class FeedTransport(SimulatedLinTransport):
    """把预先准备好的字节流喂给被动监听。"""

    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self._payload = payload
        self._pos = 0

    def read_available(self, timeout: float) -> bytes:
        if self._pos >= len(self._payload):
            time.sleep(min(timeout, 0.01))
            return b""
        chunk = self._payload[self._pos : self._pos + 16]
        self._pos += len(chunk)
        return chunk


# ---------------------------------------------------------------- 主节点轮询


@pytest.mark.parametrize("transport_cls", [SimulatedLinTransport, EchoTransport])
def test_master_polling(transport_cls):
    """主节点轮询:仿真从节点应答应被正确解码,校验和全部通过。"""
    transport = transport_cls()
    transport.responses = {0x11: b"\x01\x02", 0x12: b"\xAA\xBB\xCC"}

    worker = LinWorker()
    worker.configure(
        WorkerConfig(
            mode=MODE_MASTER,
            transport=transport,
            specs=[
                FrameSpec(0x11, 2, "Motor_1", delay_ms=5),
                FrameSpec(0x12, 3, "Motor_2", delay_ms=5),
            ],
        )
    )

    received = []
    worker.batchReady.connect(lambda batch: received.extend(batch))
    worker.start()
    pump(0.4)
    worker.request_stop()
    worker.wait(3000)
    pump(0.1)

    assert received, "没有收到任何报文"
    assert all(frame.checksum_valid for frame in received)
    assert {frame.frame_id for frame in received} == {0x11, 0x12}

    by_id = {}
    for frame in received:
        by_id.setdefault(frame.frame_id, frame)
    assert by_id[0x11].data == b"\x01\x02"
    assert by_id[0x12].data == b"\xAA\xBB\xCC"


def test_master_polling_writes_csv():
    """CSV 日志要把收到的帧写进去。"""
    tmpdir = tempfile.mkdtemp(prefix="lin_test_")
    csv_path = os.path.join(tmpdir, "frames.csv")

    transport = SimulatedLinTransport()
    transport.responses = {0x11: b"\x01\x02"}
    logbook = LogBook(csv_path=csv_path)

    worker = LinWorker()
    worker.configure(
        WorkerConfig(
            mode=MODE_MASTER,
            transport=transport,
            specs=[FrameSpec(0x11, 2, delay_ms=5)],
            logbook=logbook,
        )
    )
    worker.start()
    pump(0.3)
    worker.request_stop()
    worker.wait(3000)
    logbook.close()

    assert os.path.exists(csv_path)
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert rows[0]["id_hex"] == "0x11"
    assert rows[0]["data_hex"] == "01 02"
    assert rows[0]["checksum_ok"] == "1"
    assert rows[0]["dir"] == "RX"


def test_master_timeout_reports_error():
    """仿真从节点不响应时,应该产生一条"应答超时"的错误帧。"""
    transport = SimulatedLinTransport()
    transport.responses = {}  # 任何帧都不应答

    worker = LinWorker()
    worker.configure(
        WorkerConfig(
            mode=MODE_MASTER,
            transport=transport,
            specs=[FrameSpec(0x11, 2, delay_ms=10)],
            response_timeout_ms=5,
        )
    )
    received = []
    worker.batchReady.connect(lambda batch: received.extend(batch))
    worker.start()
    pump(0.3)
    worker.request_stop()
    worker.wait(3000)
    pump(0.1)

    assert received
    assert all(frame.is_error for frame in received)
    assert "超时" in received[0].note


def test_stats_snapshot():
    """统计快照里的计数与实际收到的帧数一致。"""
    transport = SimulatedLinTransport()
    transport.responses = {0x11: b"\x01\x02"}

    worker = LinWorker()
    worker.configure(
        WorkerConfig(
            mode=MODE_MASTER,
            transport=transport,
            specs=[FrameSpec(0x11, 2, delay_ms=5)],
        )
    )
    snapshots = []
    worker.statsReady.connect(snapshots.append)
    worker.start()
    pump(1.2)
    worker.request_stop()
    worker.wait(3000)
    pump(0.1)

    assert snapshots
    last = snapshots[-1]
    assert last["rx"] > 0
    assert any(row["frame_id"] == 0x11 for row in last["rows"])


# ---------------------------------------------------------------- 被动监听


def test_sniff_mode_decodes_frames():
    """被动监听:把一段原始字节流喂进去,应该还原出帧。"""
    data = b"\x11\x22"
    pid = protected_id(0x11)
    from lin_tool.protocol import checksum

    cks = checksum(data, pid, ChecksumType.ENHANCED)
    payload = b"\x00" + bytes([SYNC_BYTE, pid]) + data + bytes([cks])

    transport = FeedTransport(payload)
    worker = LinWorker()
    worker.configure(
        WorkerConfig(
            mode=MODE_SNIFF,
            transport=transport,
            specs=[FrameSpec(0x11, 2, delay_ms=10)],
            length_lookup=lambda fid: 2 if fid == 0x11 else None,
            name_lookup=lambda fid: "Motor_1" if fid == 0x11 else "",
        )
    )
    received = []
    worker.batchReady.connect(lambda batch: received.extend(batch))
    worker.start()
    pump(0.4)
    worker.request_stop()
    worker.wait(3000)
    pump(0.1)

    assert received, "被动监听没有解出帧"
    frame = received[0]
    assert frame.frame_id == 0x11
    assert frame.data == b"\x11\x22"
    assert frame.checksum_valid
    assert frame.name == "Motor_1"
