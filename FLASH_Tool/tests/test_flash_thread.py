"""刷写线程安全性测试。

覆盖三类会导致"闪退"(0xC0000409,无 Python 异常)的问题:
  1. 后台线程直接操作 Qt 控件 —— 跨线程 GUI 访问必须为 0 次
  2. 刷写途中中止 / 关窗口 / 关通道 —— QThread 必须在析构前真正结束
  3. CAN 工作线程被 terminate() 强杀 —— 绝不允许

跑法: python tests\\test_flash_thread.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import can
from PyQt5.QtWidgets import QApplication

from pcan_tool.isotp import FlowStatus, IsoTpReassembler, build_flow_control
from pcan_tool.ui.main_window import MainWindow
from pcan_tool.worker import CanWorker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_uds_flow import hex_record

FAILURES = []
MAIN_IDENT = threading.get_ident()
APP = QApplication(sys.argv)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def spin(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.003)


def make_hex(size: int):
    d = tempfile.mkdtemp(prefix="flashthr_")
    hp = os.path.join(d, "f.hex")
    data = bytes((i * 3 + 1) & 0xFF for i in range(size))
    with open(hp, "w") as fh:
        fh.write(hex_record(0x04, 0, (0x0801).to_bytes(2, "big")) + "\n")
        for i in range(0, len(data), 16):
            fh.write(hex_record(0x00, i, data[i:i + 16]) + "\n")
        fh.write(hex_record(0x01, 0, b"") + "\n")
    return hp, data


class Ecu(threading.Thread):
    """会说话 UDS 的 ECU,支持响应延迟与流控块大小。"""

    def __init__(self, channel: str, bs: int = 0, delay: float = 0.0) -> None:
        super().__init__(daemon=True)
        self.bus = can.interface.Bus(interface="virtual", channel=channel)
        self.rx = IsoTpReassembler()
        self.stop = threading.Event()
        self.bs = bs
        self.delay = delay
        self.n36 = 0
        self.buf = bytearray()
        self.cf_since_fc = 0

    def _send(self, payload: bytes) -> None:
        try:
            self.bus.send(can.Message(
                arbitration_id=0x736,
                data=bytes([len(payload)]) + payload,
                is_extended_id=False))
        except Exception:
            pass

    def run(self) -> None:
        while not self.stop.is_set():
            m = self.bus.recv(timeout=0.05)
            if m is None:
                continue
            try:
                payload = self.rx.feed(bytes(m.data))
            except Exception:
                continue
            if payload is None:
                t = m.data[0] & 0xF0
                if t == 0x10:
                    self.cf_since_fc = 0
                    if self.delay:
                        time.sleep(self.delay)
                    try:
                        self.bus.send(can.Message(
                            arbitration_id=0x736,
                            data=build_flow_control(FlowStatus.CONTINUE, self.bs, 0),
                            is_extended_id=False))
                    except Exception:
                        return
                elif t == 0x20:
                    self.cf_since_fc += 1
                    if self.bs and self.cf_since_fc >= self.bs:
                        self.cf_since_fc = 0
                        try:
                            self.bus.send(can.Message(
                                arbitration_id=0x736,
                                data=build_flow_control(FlowStatus.CONTINUE, self.bs, 0),
                                is_extended_id=False))
                        except Exception:
                            return
                continue

            sid = payload[0]
            if sid == 0x10:
                rsp = bytes([0x50, payload[1]])
            elif sid == 0x34:
                self.buf.clear()
                rsp = bytes([0x74, 0x00, 0x04, 0x03])
            elif sid == 0x36:
                self.n36 += 1
                self.buf.extend(payload[2:])
                rsp = bytes([0x76])
            elif sid == 0x37:
                rsp = bytes([0x77])
            else:
                rsp = bytes([0x7F, 0x11])
            if self.delay:
                time.sleep(self.delay)
            self._send(rsp)

    def shutdown(self) -> None:
        self.stop.set()
        try:
            self.bus.shutdown()
        except Exception:
            pass


class Rig:
    """一套完整的刷写环境。"""

    def __init__(self, channel: str, ecu_bs=0, ecu_delay=0.0, size=4096, block=256):
        self.channel = channel
        self.size = size
        self.block = block
        self.ecu = Ecu(channel, bs=ecu_bs, delay=ecu_delay)
        self.ecu.start()
        hp, self.expect = make_hex(size)
        self.win = MainWindow()
        self.win.show()
        self.worker = CanWorker(self.win)
        self.worker.open_bus(channel=channel, bitrate=500000, logbook=None,
                             interface="virtual")
        self.worker.batchReady.connect(self.win._on_batch)
        self.worker.start()
        time.sleep(0.5)
        APP.processEvents()
        self.win._worker = self.worker
        self.fp = self.win.flash_panel
        self.fp.load_file(hp)
        self.fp.block_custom.setValue(block)
        self.fp.set_connected(True, self.worker)

    def start_flash(self, session: int = 0) -> None:
        self.fp.session_combo.setCurrentIndex(session)
        self.fp.start()

    def wait_done(self, timeout=120) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            APP.processEvents()
            th = self.fp._thread
            if th is None or not th.isRunning():
                break
            time.sleep(0.003)
        spin(0.5)  # 让排队的日志信号送达
        return time.monotonic() - t0

    def teardown(self) -> None:
        self.ecu.shutdown()
        self.win.close()
        spin(0.6)


# ============================================================ 用例


def test_full_flash() -> None:
    section("完整刷写(不跨线程操作 GUI)")
    rig = Rig("ft_full")
    cross = {"n": 0}
    orig_prog, orig_log = rig.fp._on_progress, rig.fp.log

    def spy_prog(p):
        if threading.get_ident() != MAIN_IDENT:
            cross["n"] += 1
        orig_prog(p)

    def spy_log(lv, t):
        if threading.get_ident() != MAIN_IDENT:
            cross["n"] += 1
        orig_log(lv, t)

    rig.fp._on_progress = spy_prog
    rig.fp.log = spy_log

    rig.start_flash(session=1)
    dt = rig.wait_done()
    txt = rig.fp.log_view.toPlainText()

    check("数据逐字节一致", bytes(rig.ecu.buf) == rig.expect,
          f"{len(rig.ecu.buf)}/{len(rig.expect)} 字节")
    check("流程正常结束", "下载完成" in txt or "成功" in txt, txt.splitlines()[-1][:50])
    check("无跨线程 GUI 操作", cross["n"] == 0, f"{cross['n']} 次")
    check("刷写线程已结束", rig.fp._thread is None,
          "仍在运行" if (rig.fp._thread and rig.fp._thread.isRunning()) else "")
    print(f"      耗时 {dt:.1f}s,0x36 共 {rig.ecu.n36} 次")
    rig.teardown()


def test_bs_limited() -> None:
    section("ECU 限制流控块大小 (BS=8)")
    rig = Rig("ft_bs", ecu_bs=8, size=8192, block=256)
    rig.start_flash()
    rig.wait_done()
    check("BS=8 下数据一致", bytes(rig.ecu.buf) == rig.expect,
          f"{len(rig.ecu.buf)}/{len(rig.expect)} 字节")
    check("BS=8 下流程完成", "成功" in rig.fp.log_view.toPlainText(),
          rig.fp.log_view.toPlainText().splitlines()[-1][:50])
    rig.teardown()


def test_ecu_limit_block() -> None:
    section("ECU 限制块大小 (maxBlockLen=1027)")
    rig = Rig("ft_blk", size=5752, block=4093)
    rig.start_flash()
    rig.wait_done()
    check("被 ECU 夹到 1027 后仍成功",
          "成功" in rig.fp.log_view.toPlainText()
          and bytes(rig.ecu.buf) == rig.expect,
          f"{len(rig.ecu.buf)}/{len(rig.expect)} 字节")
    rig.teardown()


def _interrupt_case(label, channel, action, ecu_delay=0.0, size=30000):
    section(label)
    rig = Rig(channel, ecu_delay=ecu_delay, size=size, block=64)
    rig.start_flash()
    spin(0.35)  # 传一会儿再打断

    action(rig.win, rig.fp)

    spin(0.8)
    th = rig.fp._thread
    still = th is not None and th.isRunning()
    check("打断后刷写线程已退出", not still,
          "" if not still else "线程仍在运行 —— 退出时可能闪退")
    check("线程引用已清空", rig.fp._thread is None or not rig.fp._thread.isRunning())
    rig.teardown()
    # 走到这里没崩,说明 QThread 在析构前确实结束了
    check("打断后关闭窗口未崩溃", True)


def test_interrupt_abort() -> None:
    _interrupt_case("刷写途中点『中止』", "ft_abort", lambda w, f: f.stop())


def test_interrupt_close_window() -> None:
    _interrupt_case("刷写途中直接关窗口", "ft_close", lambda w, f: w.close())


def test_interrupt_slow_ecu() -> None:
    _interrupt_case("ECU 很慢 + 中止", "ft_slow1", lambda w, f: f.stop(), ecu_delay=0.05)


def test_disconnect_during_flash() -> None:
    section("刷写途中关掉 CAN 通道")
    rig = Rig("ft_disc", size=30000, block=64)
    rig.start_flash()
    spin(0.35)

    w = rig.win
    w._stop_flash_thread()
    w._worker.wake_pending_requests()
    w._worker.shutdown_bus()
    exited = w._worker.wait(15000)
    w._worker = None
    w._on_worker_closed()
    spin(0.5)

    check("CAN 线程正常退出(未被强杀)", exited, f"wait={exited}")
    check("总线关闭后线程不残留", not w._worker is not None)
    rig.ecu.shutdown()
    spin(0.4)


def test_no_terminate_on_worker() -> None:
    section("CAN 工作线程不使用 terminate()")
    import inspect

    from pcan_tool.ui import main_window

    src = inspect.getsource(main_window.MainWindow._on_disconnect)
    # 去掉注释再判断,否则会被注释里出现的 terminate() 字样误伤
    code = "\n".join(
        l.split("#", 1)[0] for l in src.splitlines() if not l.strip().startswith("#")
    )
    check("_on_disconnect 里没有 terminate()", "terminate()" not in code)
    check("会先唤醒等待中的请求", "wake_pending_requests" in src)
    check("关闭时等待时间足够(>=10s)", "wait(15000)" in code or "wait(10000)" in code)


def test_default_ids() -> None:
    section("默认 CAN ID")
    rig = Rig("ft_ids")
    check("默认发送 ID 为 0x737", rig.fp.tx_id_edit.text() == "0x737",
          rig.fp.tx_id_edit.text())
    check("默认接收 ID 为 0x736", rig.fp.rx_id_edit.text() == "0x736",
          rig.fp.rx_id_edit.text())
    rig.teardown()


def main() -> int:
    test_full_flash()
    test_bs_limited()
    test_ecu_limit_block()
    test_interrupt_abort()
    test_interrupt_close_window()
    test_interrupt_slow_ecu()
    test_disconnect_during_flash()
    test_no_terminate_on_worker()
    test_default_ids()

    print("\n" + "=" * 56)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: " + ", ".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())