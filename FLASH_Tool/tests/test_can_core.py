"""端到端自测:验证 CAN 工作线程的发送、接收、CSV 与 BLF 落盘。

用 python-can 的 virtual 通道做回环,不需要总线上有其它节点。
运行:  python _selftest.py
"""

from __future__ import annotations

import csv as _csv
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication

import can

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcan_tool.logbook import LogBook
from pcan_tool.worker import CanWorker

CHANNEL = "selftest_chan"
FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def pump(app: QCoreApplication, seconds: float) -> None:
    """跑事件循环 -- 跨线程的队列信号必须靠事件循环投递。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def main() -> int:
    app = QCoreApplication(sys.argv)
    tmpdir = tempfile.mkdtemp(prefix="pcan_selftest_")
    csv_path = os.path.join(tmpdir, "t.csv")
    blf_path = os.path.join(tmpdir, "t.blf")

    logbook = LogBook(csv_path=csv_path, blf_path=blf_path)
    worker = CanWorker()
    worker.open_bus(channel=CHANNEL, bitrate=500000, logbook=logbook, interface="virtual")

    received: list = []
    opened_info: list = []

    worker.opened.connect(opened_info.append)
    worker.batchReady.connect(received.append)

    worker.start()
    pump(app, 0.5)  # 等线程挂上虚拟通道,顺便让 opened 信号送达

    # 对端总线:向同一虚拟通道注入 3 帧,模拟 STM32 发来的报文
    peer = can.interface.Bus(interface="virtual", channel=CHANNEL)
    injected = [
        can.Message(arbitration_id=0x321, data=[1, 2, 3], is_extended_id=False),
        can.Message(arbitration_id=0x737, data=[0] * 8, is_extended_id=False),
        can.Message(arbitration_id=0x18FF50E5, data=b"\xde\xad\xbe\xef", is_extended_id=True),
    ]
    for m in injected:
        peer.send(m)
    pump(app, 0.6)

    # 发送 2 帧(标准 + 扩展)
    for msg in (
        can.Message(arbitration_id=0x100, data=[1, 2, 3, 4], is_extended_id=False, is_rx=False),
        can.Message(arbitration_id=0x1ABCDEF, data=[0xAA] * 8, is_extended_id=True, is_rx=False),
    ):
        worker.send(msg)
    pump(app, 0.4)

    # 周期发送 5 帧,10 ms 一次
    worker.start_cyclic(
        10.0, can.Message(arbitration_id=0x200, data=[9], is_extended_id=False, is_rx=False), 5
    )
    pump(app, 0.8)

    worker.stop_all_cyclic()
    pump(app, 0.3)

    rx_seen = [m for b in received for m in b if m.is_rx]
    tx_seen = [m for b in received for m in b if not m.is_rx]

    print("\n== 通道 ==")
    check("通道打开回调", bool(opened_info), opened_info[0] if opened_info else "无")

    print("\n== 接收 ==")
    check("收到注入的 3 帧 RX", len(rx_seen) == 3, f"实际 {len(rx_seen)} 帧")
    if len(rx_seen) == 3:
        got_ids = sorted(m.arbitration_id for m in rx_seen)
        check("RX 的 ID 正确", got_ids == [0x321, 0x737, 0x18FF50E5],
              f"{[hex(i) for i in got_ids]}")
        ext = [m for m in rx_seen if m.arbitration_id == 0x18FF50E5]
        check("扩展帧标志保留", bool(ext) and ext[0].is_extended_id)
        d = [m for m in rx_seen if m.arbitration_id == 0x321]
        check("数据内容正确", bool(d) and bytes(d[0].data) == b"\x01\x02\x03",
              bytes(d[0].data).hex(" ") if d else "")

    print("\n== 发送 ==")
    check("发送 2 帧 + 周期 5 帧 = 7", len(tx_seen) == 7, f"实际 {len(tx_seen)} 帧")
    cyc = [m for m in tx_seen if m.arbitration_id == 0x200]
    check("周期发送恰好 5 帧", len(cyc) == 5, f"实际 {len(cyc)} 帧")
    check("TX 报文 is_rx=False", all(not m.is_rx for m in tx_seen))

    print("\n== 统计 ==")
    snap = worker._make_stats_snapshot()
    by_id = {(r["arb_id"], r["dir"]): r for r in snap["rows"]}
    check("统计 RX = 3", snap["rx"] == 3, f"RX={snap['rx']} TX={snap['tx']}")
    check("统计 TX = 7", snap["tx"] == 7, f"RX={snap['rx']} TX={snap['tx']}")
    check("0x200 TX 统计项存在 (周期发送)", (0x200, "TX") in by_id)
    check("0x100 TX 统计项存在", (0x100, "TX") in by_id)
    check("0x321 RX 统计项存在", (0x321, "RX") in by_id)

    print("\n== 日志 ==")
    worker.shutdown_bus()
    worker.wait(5000)
    pump(app, 0.3)
    peer.shutdown()

    check("CSV 文件存在", os.path.exists(csv_path))
    check("BLF 文件存在", os.path.exists(blf_path))

    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(_csv.DictReader(fh))
        check("CSV 行数 = 10 (无重复写入)", len(rows) == 10, f"实际 {len(rows)}")
        check(
            "CSV 表头正确",
            list(rows[0].keys())[:8]
            == ["time", "time_s", "dir", "id_type", "id_hex", "dlc", "data_hex", "flags"],
        )
        check("CSV RX 行数 = 3", sum(r["dir"] == "RX" for r in rows) == 3,
              f"{sum(r['dir'] == 'RX' for r in rows)} 行")
        check("CSV TX 行数 = 7", sum(r["dir"] == "TX" for r in rows) == 7,
              f"{sum(r['dir'] == 'TX' for r in rows)} 行")
        check("CSV 扩展帧 = 2 行", sum(r["id_type"] == "EXT" for r in rows) == 2,
              f"{sum(r['id_type'] == 'EXT' for r in rows)} 行")
        if rows:
            print("      CSV 首行:",
                  {k: rows[0][k] for k in ("dir", "id_hex", "dlc", "data_hex")})

    if os.path.exists(blf_path):
        from can.io.blf import BLFReader

        with open(blf_path, "rb") as fh:
            blf_msgs = list(BLFReader(fh))
        check("BLF 消息数 = 10", len(blf_msgs) == 10, f"实际 {len(blf_msgs)}")
        if blf_msgs:
            rx_blf = [m for m in blf_msgs if m.is_rx]
            tx_blf = [m for m in blf_msgs if not m.is_rx]
            check("BLF 正确区分 RX/TX", len(rx_blf) == 3 and len(tx_blf) == 7,
                  f"RX={len(rx_blf)} TX={len(tx_blf)}")

    print("\n" + "=" * 54)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: " + ", ".join(FAILURES))
        print(f"日志保留在: {tmpdir}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())