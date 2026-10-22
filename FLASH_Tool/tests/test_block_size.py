"""块大小跟随 ECU 的测试。

覆盖 0x34 正响应里 maxNumberOfBlockLength 的各种解释与限制:
  - 声明值含服务头(本项目实测 ECU:0x74 00 04 02 = 1026 -> 数据 1024)
  - 声明值仅数据字节
  - 跟随 ECU 开关的开/关
  - 声明值超过 ISO-TP 上限时的夹取
  - 声明值非法时退回设置值

跑法: python tests\\test_block_size.py
"""
from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pcan_tool.hexfile import parse_intel_hex
from pcan_tool.isotp import FlowStatus, IsoTpReassembler, build_flow_control
from pcan_tool.uds import (
    MAX_BLOCK_DATA,
    UdsConfig,
    UdsDownloader,
    UdsTransport,
)
from test_uds_flow import hex_record

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


class Ecu(threading.Thread):
    """返回指定 maxNumberOfBlockLength 的 ECU 模拟器。"""

    def __init__(self, max_block_len: int) -> None:
        super().__init__(daemon=True)
        self.in_q: "queue.Queue[bytes]" = queue.Queue()
        self.out_q: "queue.Queue[bytes]" = queue.Queue()
        self.max_block_len = max_block_len
        self.block_lengths: list = []
        self.buf = bytearray()
        self._running = True
        self._rx = IsoTpReassembler()

    def stop(self) -> None:
        self._running = False
        self.in_q.put(b"")

    def run(self) -> None:
        while self._running:
            try:
                frame = self.in_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if frame == b"":
                break
            try:
                payload = self._rx.feed(frame)
            except Exception:
                continue
            if payload is None:
                if (frame[0] & 0xF0) == 0x10:
                    self.out_q.put(build_flow_control(FlowStatus.CONTINUE, 0, 0))
                continue
            sid = payload[0]
            if sid == 0x34:
                self.buf.clear()
                v = self.max_block_len
                # 0x34 正响应: 74 <dataFormatIdentifier> <maxBlockLen_H> <maxBlockLen_L>
                rsp = bytes([0x74, 0x00, (v >> 8) & 0xFF, v & 0xFF])
                self.out_q.put(bytes([len(rsp)]) + rsp)
            elif sid == 0x36:
                self.block_lengths.append(len(payload) - 2)
                self.buf.extend(payload[2:])
                self.out_q.put(bytes([1, 0x76]))
            elif sid == 0x37:
                self.out_q.put(bytes([1, 0x77]))
            else:
                self.out_q.put(bytes([1, 0x7F, 0x11]))

    def shutdown(self) -> None:
        self.stop()
        self.join(timeout=2)


class Tp(UdsTransport):
    def __init__(self, ecu: Ecu) -> None:
        self.ecu = ecu

    def send(self, payload: bytes) -> None:
        self.ecu.in_q.put(payload)

    def send_and_wait(self, payload: bytes, timeout: float):
        self.ecu.in_q.put(payload)
        try:
            return self.ecu.out_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait(self, timeout: float):
        try:
            return self.ecu.out_q.get(timeout=timeout)
        except queue.Empty:
            return None


def make_image(size: int):
    d = tempfile.mkdtemp(prefix="blk_")
    p = os.path.join(d, "f.hex")
    data = bytes((i * 11 + 5) & 0xFF for i in range(size))
    with open(p, "w") as fh:
        fh.write(hex_record(0x04, 0, (0x0801).to_bytes(2, "big")) + "\n")
        for i in range(0, len(data), 16):
            fh.write(hex_record(0x00, i, data[i:i + 16]) + "\n")
        fh.write(hex_record(0x01, 0, b"") + "\n")
    return parse_intel_hex(p), data


def run(ecu_max: int, size: int, *, follow=True, includes_header=True, block_size=0x3F8):
    img, data = make_image(size)
    ecu = Ecu(ecu_max)
    ecu.start()
    cfg = UdsConfig(
        block_size=block_size,
        p2_timeout=2.0,
        p2_star_timeout=5.0,
        follow_ecu_block_size=follow,
        ecu_block_len_includes_header=includes_header,
    )
    dl = UdsDownloader(image=img, config=cfg, transport=Tp(ecu),
                       on_progress=lambda p: None, on_log=lambda l, t: None)
    err = None
    try:
        dl.run()
    except Exception as exc:
        err = exc
    ecu.shutdown()
    return dl, ecu, data, err


def test_follow_header() -> None:
    section("跟随 ECU(声明值含服务头)—— 本项目实测 ECU")
    # 0x74 00 04 02 = 1026,含 2 字节服务头 -> 每块数据 1024
    dl, ecu, data, err = run(0x0402, 5752)
    check("下载无异常", err is None, str(err) if err else "")
    check("每块数据 = 1026 - 2 = 1024",
          dl.effective_block_size == 1024, str(dl.effective_block_size))
    # 5752 = 5 x 1024 + 632,最后一块不满是正常的
    expect_lengths = [1024] * 5 + [632]
    check("除末块外都是 1024,末块为余数",
          ecu.block_lengths == expect_lengths,
          f"{ecu.block_lengths[:2]}…{ecu.block_lengths[-1:]}")
    check("块数正确(5752 / 1024 向上取整)",
          len(ecu.block_lengths) == (5752 + 1023) // 1024, str(len(ecu.block_lengths)))
    check("总数据量正确", sum(ecu.block_lengths) == 5752, str(sum(ecu.block_lengths)))
    check("数据逐字节一致", bytes(ecu.buf) == data,
          f"{len(ecu.buf)}/{len(data)}")


def test_follow_no_header() -> None:
    section("跟随 ECU(声明值仅数据字节)")
    dl, ecu, data, err = run(0x0400, 4096, includes_header=False)
    check("每块数据 = 1024(不减 2)", dl.effective_block_size == 1024,
          str(dl.effective_block_size))
    check("数据一致", err is None and bytes(ecu.buf) == data)


def test_follow_off() -> None:
    section("不跟随 ECU,用手动设置")
    # 手动 256 < ECU 1024 -> 应保持 256
    dl, ecu, data, err = run(0x0402, 4096, follow=False, block_size=256)
    check("采用手动值 256", dl.effective_block_size == 256,
          str(dl.effective_block_size))
    check("数据一致", err is None and bytes(ecu.buf) == data)

    # 手动 2048 > ECU 1024 -> 被 ECU 夹到 1024
    dl2, ecu2, data2, err2 = run(0x0402, 4096, follow=False, block_size=2048)
    check("手动 2048 被 ECU 夹到 1024", dl2.effective_block_size == 1024,
          str(dl2.effective_block_size))
    check("夹取后数据一致", err2 is None and bytes(ecu2.buf) == data2)


def test_iso_tp_clamp() -> None:
    section("声明值超过 ISO-TP 上限")
    # 声明 8000 -> 含头后 7998 -> 必须夹到 4093
    dl, ecu, data, err = run(8000, 9000)
    check(f"夹到 ISO-TP 上限 {MAX_BLOCK_DATA}", dl.effective_block_size == MAX_BLOCK_DATA,
          str(dl.effective_block_size))
    check("夹取后仍能传完", err is None and bytes(ecu.buf) == data,
          f"{len(ecu.buf)}/{len(data)}")
    if ecu.block_lengths:
        check("实际每块未超上限", max(ecu.block_lengths) <= MAX_BLOCK_DATA,
              f"最大 {max(ecu.block_lengths)}")


def test_invalid_declared() -> None:
    section("声明值非法/过小")
    dl, ecu, data, err = run(1, 2048, follow=True, block_size=512)
    check("声明值 1 过小 -> 退回设置值 512", dl.effective_block_size == 512,
          str(dl.effective_block_size))
    check("退回后仍能传完", err is None and bytes(ecu.buf) == data)

    dl2, ecu2, data2, err2 = run(2, 2048, follow=True, block_size=512)
    check("声明值 2(含头后为 0) -> 退回设置值", dl2.effective_block_size == 512,
          str(dl2.effective_block_size))
    check("退回后数据一致", err2 is None and bytes(ecu2.buf) == data2)


def test_various_declared() -> None:
    section("多种声明值")
    for declared, expect in ((8, 6), (0x0064, 0x62), (0x0100, 0xFE),
                             (0x0402, 0x400), (0x0800, 0x7FE)):
        dl, ecu, data, err = run(declared, 4096)
        ok = dl.effective_block_size == expect and bytes(ecu.buf) == data
        check(f"声明 {declared} -> 每块 {expect} 字节", ok,
              f"实际 {dl.effective_block_size}, 数据 {len(ecu.buf)}/{len(data)}")


def main() -> int:
    test_follow_header()
    test_follow_no_header()
    test_follow_off()
    test_iso_tp_clamp()
    test_invalid_declared()
    test_various_declared()

    print("\n" + "=" * 56)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: " + ", ".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())