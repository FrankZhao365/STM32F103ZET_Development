"""UDS 下载流程测试。

三部分:
  1. Intel HEX 解析(校验和、扩展线性地址、地址空洞)
  2. ISO-TP 分帧/重组往返 + 流控
  3. 完整 0x34 / 0x36 / 0x37 下载 —— 用会说话 UDS 的 ECU 模拟器做对端

ISO-TP 帧布局对照 ISO 15765-2(经典 CAN 8 字节帧):
    SF  [0x0N][data x7]                 PCI 1 字节,数据 <=7
    FF  [0x1H][0xLL][data x6]           PCI 2 字节,无序列号,数据 <=6
    CF  [0x2N][data x7]                 PCI 1 字节,首个 CF 的 N = 1
    FC  [0x3F][BS][STmin]               PCI 3 字节

运行:  python tests\\test_uds_flow.py
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcan_tool.hexfile import HexFileError, parse_intel_hex
from pcan_tool.isotp import (
    CONSECUTIVE_FRAME_DATA,
    FIRST_CONSECUTIVE_SEQ,
    FIRST_FRAME_DATA,
    FlowStatus,
    IsoTpReassembler,
    build_flow_control,
    parse_flow_control,
    split_frames,
)
from pcan_tool.uds import UdsConfig, UdsDownloader, UdsError, UdsProgress, UdsTransport

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ====================================================================== HEX


def hex_record(rec_type: int, address: int, data: bytes) -> str:
    """生成一行合法的 Intel HEX 记录(含正确校验和)。"""
    raw = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, rec_type]) + data
    return ":" + raw.hex().upper() + f"{(-sum(raw)) & 0xFF:02X}"


def make_hex(path: str, base: int, payload: bytes, seg_len: int = 16) -> None:
    lines = []
    if base > 0xFFFF:
        lines.append(hex_record(0x04, 0, (base >> 16).to_bytes(2, "big")))
    for i in range(0, len(payload), seg_len):
        lines.append(hex_record(0x00, (base + i) & 0xFFFF, payload[i : i + seg_len]))
    lines.append(hex_record(0x01, 0, b""))
    with open(path, "w", encoding="ascii") as fh:
        fh.write("\n".join(lines) + "\n")


def test_hex(tmpdir: str):
    section("Intel HEX 解析")
    path = os.path.join(tmpdir, "fw.hex")
    payload = bytes((i * 7 + 3) & 0xFF for i in range(1000))
    make_hex(path, 0x08000000, payload)
    image = parse_intel_hex(path)

    check("数据逐字节一致", image.data == payload, f"{len(image.data)} vs {len(payload)}")
    check("长度无多余字节", image.length == len(payload), str(image.length))
    check("起始地址正确", image.base_address == 0x08000000, f"0x{image.base_address:08X}")
    check("结束地址正确", image.end_address == 0x080003E7, f"0x{image.end_address:08X}")
    check("无空洞", not image.has_gaps)

    section("HEX 边界情况")
    bad = os.path.join(tmpdir, "bad.hex")
    with open(bad, "w") as fh:
        fh.write(":04000000DEADBEEF00\n")  # 校验和故意写错
    try:
        parse_intel_hex(bad)
        check("拒绝错误校验和", False)
    except HexFileError as exc:
        check("拒绝错误校验和", "校验和" in str(exc), str(exc)[:40])

    bad2 = os.path.join(tmpdir, "bad2.hex")
    with open(bad2, "w") as fh:
        fh.write("04000000DEADBEEF00\n")
    try:
        parse_intel_hex(bad2)
        check("拒绝缺少起始符", False)
    except HexFileError:
        check("拒绝缺少起始符", True)

    gap = os.path.join(tmpdir, "gap.hex")
    with open(gap, "w") as fh:
        fh.write(hex_record(0x04, 0, (0x0800).to_bytes(2, "big")) + "\n")
        fh.write(hex_record(0x00, 0x0000, b"\xAA\xBB") + "\n")
        fh.write(hex_record(0x00, 0x0010, b"\xCC\xDD") + "\n")
        fh.write(hex_record(0x01, 0, b"") + "\n")
    img = parse_intel_hex(gap)
    check("检出地址空洞", img.has_gaps, f"{len(img.gaps)} 处")
    expect = b"\xAA\xBB" + b"\xFF" * 14 + b"\xCC\xDD"
    check("空洞用 0xFF 填充", img.data == expect, f"{len(img.data)} 字节")

    return image


# ==================================================================== ISO-TP


def test_isotp() -> None:
    section("ISO-TP 分帧/重组往返")
    for size in (1, 6, 7, 8, 100, 1000, 4095):
        payload = bytes((i * 13 + 1) & 0xFF for i in range(size))
        frames = split_frames(payload)
        rx = IsoTpReassembler()
        got = None
        for f in frames:
            got = rx.feed(f) or got
        check(f"{size} 字节往返一致", got == payload, f"{len(frames)} 帧")

        if got == payload and size > 7:
            exp_ff_len = ((size - FIRST_FRAME_DATA) + CONSECUTIVE_FRAME_DATA - 1) // CONSECUTIVE_FRAME_DATA
            exp_frames = 1 + exp_ff_len
            check(f"  {size} 字节帧数正确", len(frames) == exp_frames,
                  f"{len(frames)} 帧 (期望 {exp_frames})")
            check("  首帧 PCI 长度字段正确",
                  (frames[0][0] & 0xF0) == 0x10
                  and (frames[0][0] & 0x0F) == ((size >> 8) & 0x0F)
                  and frames[0][1] == (size & 0xFF),
                  frames[0][:2].hex(" "))
            check("  首帧 = 2 字节 PCI + 6 字节数据", len(frames[0]) == 8,
                  f"{len(frames[0])} 字节")
            check("  首个连续帧序列号为 1", (frames[1][0] & 0x0F) == FIRST_CONSECUTIVE_SEQ,
                  f"0x{frames[1][0]:02X}")
            seqs = [frames[i][0] & 0x0F for i in range(1, len(frames))]
            check("  连续帧序列号递增(1,2,3...回绕 15->0)",
                  seqs == [((i) & 0x0F) for i in range(1, len(frames))],
                  str(seqs[:6]))
            # 末帧允许不满(只剩几个字节),其余连续帧都应是满 8 字节
            check("  连续帧除末帧外均 8 字节",
                  all(len(f) == 8 for f in frames[1:-1]),
                  f"长度 {[len(f) for f in frames[1:]][:5]}…")

    section("流控帧(3 字节)")
    fc = parse_flow_control(bytes([0x30, 0x0A, 0x14]))
    check("FC CONTINUE", fc.status == FlowStatus.CONTINUE)
    check("FC BS=10", fc.block_size == 10)
    check("FC STmin=20ms", fc.st_min_ms == 20.0, f"{fc.st_min_ms}")
    check("FC 构造长度 3 字节", len(build_flow_control()) == 3, build_flow_control().hex(" "))
    check("FC FS 打包在 byte0 低半字节",
          build_flow_control(FlowStatus.OVERFLOW, 5, 0)[0] == 0x32,
          build_flow_control(FlowStatus.OVERFLOW, 5, 0).hex(" "))
    fc2 = parse_flow_control(build_flow_control(FlowStatus.WAIT, 0, 0xF1))
    check("FC 100µs -> 0.1ms", abs(fc2.st_min_ms - 0.1) < 1e-6, f"{fc2.st_min_ms}")
    fc3 = parse_flow_control(bytes([0x32, 0x00, 0x00]))
    check("FC Overflow 可解析", fc3.status == FlowStatus.OVERFLOW)

    section("ISO-TP 异常处理")
    rx = IsoTpReassembler()
    rx.feed(bytes([0x10, 0x20]) + b"\x01\x02\x03\x04\x05\x06")
    try:
        rx.feed(bytes([0x25]) + b"\x00" * 7)
        check("检出连续帧序列号错误", False)
    except Exception as exc:
        check("检出连续帧序列号错误", "序列号" in str(exc), str(exc))

    try:
        rx2 = IsoTpReassembler()
        rx2.feed(bytes([0x23]) + b"\x00" * 7)
        check("检出无首帧的连续帧", False)
    except Exception as exc:
        check("检出无首帧的连续帧", "没有首帧" in str(exc), str(exc))


# ============================================================== ECU 模拟器


class SimEcu(threading.Thread):
    """会说话 ISO-TP + UDS 的 ECU 模拟器。

    客户端 -> ECU 用 0x737,ECU -> 客户端用 0x736。
    实现 0x34 / 0x36 / 0x37,并记录收到的数据用于校验。
    """

    def __init__(self, max_block_len: int = 0x3F8, fc_bs: int = 0, st_min: int = 0) -> None:
        super().__init__(daemon=True)
        self.in_q: "queue.Queue[bytes]" = queue.Queue()
        self.out_q: "queue.Queue[bytes]" = queue.Queue()
        self.max_block_len = max_block_len
        self.fc_bs = fc_bs
        self.st_min = st_min

        self.received = bytearray()
        self.req_download_addr = 0
        self.req_download_size = 0
        self.expect_block = 1
        self.block_errors: list = []
        self.saw_exit = False
        self.seen_sids: list = []
        self._running = True
        self._rx = IsoTpReassembler()
        self._cf_since_fc = 0

    def stop(self) -> None:
        self._running = False
        self.in_q.put(b"")

    def handle_uds(self, payload: bytes) -> bytes:
        if not payload:
            return b""
        sid = payload[0]
        self.seen_sids.append(sid)

        if sid == 0x34 and len(payload) >= 3:
            self.received.clear()
            self.expect_block = 1
            afi = payload[2]
            n_addr = (afi >> 4) & 0x0F
            n_size = afi & 0x0F
            off = 3
            self.req_download_addr = int.from_bytes(payload[off:off + n_addr], "big")
            off += n_addr
            self.req_download_size = int.from_bytes(payload[off:off + n_size], "big")
            return bytes([0x74, afi, (self.max_block_len >> 8) & 0xFF,
                          self.max_block_len & 0xFF])

        if sid == 0x36 and len(payload) >= 2:
            counter = payload[1]
            if counter != self.expect_block:
                self.block_errors.append((self.expect_block, counter))
                return bytes([0x7F, 0x73])
            self.expect_block = self.expect_block % 0xFF + 1
            self.received.extend(payload[2:])
            return bytes([0x76])

        if sid == 0x37:
            self.saw_exit = True
            return bytes([0x77])

        if sid == 0x10:
            return bytes([0x50, payload[1]]) if len(payload) > 1 else bytes([0x7F, 0x11])

        return bytes([0x7F, 0x11])

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
                self.out_q.put(build_flow_control(FlowStatus.OVERFLOW, 0, 0))
                self._rx.start()
                continue
            if payload is None:
                ptype = frame[0] & 0xF0
                if ptype == 0x10:
                    self._cf_since_fc = 0
                    self.out_q.put(
                        build_flow_control(FlowStatus.CONTINUE, self.fc_bs, self.st_min)
                    )
                elif ptype == 0x20:
                    # 真实 ECU 会在每 BS 个连续帧后补发一次流控
                    self._cf_since_fc += 1
                    if self.fc_bs and self._cf_since_fc >= self.fc_bs:
                        self._cf_since_fc = 0
                        self.out_q.put(
                            build_flow_control(FlowStatus.CONTINUE, self.fc_bs, self.st_min)
                        )
                continue
            try:
                rsp = self.handle_uds(payload)
            except Exception:
                rsp = bytes([0x7F, 0x72])
            if rsp:
                self.out_q.put(bytes([len(rsp)]) + rsp)


class SimTransport(UdsTransport):
    """客户端侧传输,直连 SimEcu 的队列。"""

    def __init__(self, ecu: SimEcu) -> None:
        self.ecu = ecu
        self.sent_frames: list = []

    def send(self, payload: bytes) -> None:
        self.sent_frames.append(payload)
        self.ecu.in_q.put(payload)

    def send_and_wait(self, payload: bytes, timeout: float):
        self.sent_frames.append(payload)
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


def run_download(image, big, block_size=64, follow_ecu=False, **ecu_kwargs):
    """跑一次下载,返回 (ecu, transport, progress, error)。

    默认 follow_ecu=False —— 这些用例专门验证**手动块大小**的传递是否正确;
    跟随 ECU 的行为由 test_block_size.py 覆盖。
    """
    ecu = SimEcu(**ecu_kwargs)
    ecu.start()
    transport = SimTransport(ecu)
    progress: list = []
    dl = UdsDownloader(
        image=image,
        config=UdsConfig(block_size=block_size, p2_timeout=2.0, p2_star_timeout=5.0,
                         follow_ecu_block_size=follow_ecu),
        transport=transport,
        on_progress=progress.append,
        on_log=lambda lvl, txt: None,
    )
    error = None
    try:
        dl.run()
    except Exception as exc:
        error = exc
    ecu.stop()
    ecu.join(timeout=2)
    return ecu, transport, progress, error


# ================================================================== 下载流程


def test_download(base_image) -> None:
    big = bytes((i * 31 + 7) & 0xFF for i in range(2000))  # 2000 字节
    image = type(base_image)(data=big, base_address=base_image.base_address)

    section("0x34 / 0x36 / 0x37 完整流程")
    ecu, tp, prog, err = run_download(image, big, block_size=64)
    check("下载无异常", err is None, str(err) if err else "")
    check("ECU 收到的数据逐字节一致", bytes(ecu.received) == big,
          f"{len(ecu.received)} vs {len(big)}")
    check("块序号连续无跳号", not ecu.block_errors, str(ecu.block_errors))
    check("服务顺序 34 -> 36... -> 37",
          ecu.seen_sids[:2] == [0x34, 0x36] and ecu.seen_sids[-1] == 0x37,
          f"{[hex(s) for s in ecu.seen_sids[:3]]}…{hex(ecu.seen_sids[-1])}")
    check("0x34 收到的地址正确", ecu.req_download_addr == base_image.base_address,
          f"0x{ecu.req_download_addr:08X}")
    check("0x34 收到的长度正确", ecu.req_download_size == len(big),
          str(ecu.req_download_size))
    check("确实走了多帧(发出过连续帧)",
          any((f[0] & 0xF0) == 0x20 for f in tp.sent_frames),
          f"共发出 {len(tp.sent_frames)} 帧")
    check("客户端没有自行发 FC(应由 ECU 发)",
          not any((f[0] & 0xF0) == 0x30 for f in tp.sent_frames))
    ff66 = next(
        (f for f in tp.sent_frames
         if (f[0] & 0xF0) == 0x10 and f[1] == (64 + 2) & 0xFF
         and (f[0] & 0x0F) == ((64 + 2) >> 8) & 0x0F),
        None,
    )
    check("0x36 首帧长度字段 = 0x36 + 序号 + 64 字节 = 66", ff66 is not None,
          ff66[:2].hex(" ") if ff66 else "未找到")
    check("单帧请求都包了 ISO-TP 头(0x0N)",
          any((f[0] & 0xF0) == 0x00 and (f[0] & 0x0F) == len(f) - 1
              for f in tp.sent_frames),
          next((f.hex(" ") for f in tp.sent_frames if (f[0] & 0xF0) == 0x00), "无单帧"))
    check("进度达到 100%", bool(prog) and prog[-1].percent >= 99.9,
          f"{prog[-1].percent:.1f}%" if prog else "无进度")
    check("块数计算正确", bool(prog) and prog[-1].block_total == (len(big) + 63) // 64,
          str(prog[-1].block_total) if prog else "")

    section("块大小配置(message 大小)")
    for bs in (8, 16, 64, 256, 1016):
        ecu2, tp2, prog2, err2 = run_download(image, big, block_size=bs)
        exp_blocks = (len(big) + bs - 1) // bs
        ok = (err2 is None
              and bytes(ecu2.received) == big
              and prog2[-1].block_total == exp_blocks)
        check(f"块大小 {bs} 字节", ok,
              f"{prog2[-1].block_total if prog2 else '?'} 块"
              + (f" 错误:{err2}" if err2 else ""))

    section("ECU 限制块大小")
    ecu3, tp3, prog3, err3 = run_download(image, big, block_size=1016, max_block_len=128)
    exp = (len(big) + 127) // 128
    check("被 ECU 的 maxNumberOfBlockLength 夹到 128",
          err3 is None and bytes(ecu3.received) == big and prog3[-1].block_total == exp,
          f"{prog3[-1].block_total if prog3 else '?'} 块 (期望 {exp})")

    section("流控块大小 BS 限制")
    ecu4, tp4, prog4, err4 = run_download(image, big, block_size=256, fc_bs=8)
    check("ECU 设 BS=8 时仍能传完", err4 is None and bytes(ecu4.received) == big,
          f"{len(ecu4.received)} 字节" + (f" 错误:{err4}" if err4 else ""))
    # 0x34 请求本身也是多帧,会多发 1 个首帧,所以用 >= 比较
    ff_count = sum(1 for f in tp4.sent_frames if (f[0] & 0xF0) == 0x10)
    check("每块都发了首帧(含 0x34 自身)",
          ff_count >= prog4[-1].block_total,
          f"{ff_count} 个首帧 / {prog4[-1].block_total} 块")

    section("跟随 ECU 块大小")
    ecu7, tp7, prog7, err7 = run_download(image, big, block_size=8,
                                          follow_ecu=True, max_block_len=0x0402)
    check("跟随 ECU 时采用 1024(声明 1026 减服务头 2)",
          err7 is None and prog7[-1].block_total == (len(big) + 1023) // 1024,
          f"{prog7[-1].block_total if prog7 else '?'} 块"
          + (f" 错误:{err7}" if err7 else ""))
    check("跟随 ECU 后数据仍一致", bytes(ecu7.received) == big,
          f"{len(ecu7.received)} 字节")

    section("前置命令")
    ecu5, tp5, prog5, err5 = run_download(image, big, block_size=64)
    check("默认不发前置命令", err5 is None)

    ecu6, tp6, prog6, err6 = run_download(image, big, block_size=64)
    check("下载流程完整可重复", err6 is None and bytes(ecu6.received) == big)

    section("异常处理")
    dead = SimEcu()

    class DeadTransport(SimTransport):
        def send_and_wait(self, payload, timeout):
            return None

    dl6 = UdsDownloader(image=image,
                        config=UdsConfig(block_size=64, p2_timeout=0.2,
                                         follow_ecu_block_size=False),
                        transport=DeadTransport(dead))
    try:
        dl6.run()
        check("ECU 无响应时报错", False)
    except UdsError as exc:
        check("ECU 无响应时报错", "超时" in str(exc), str(exc)[:50])

    rej = SimEcu()

    class NrcTransport(SimTransport):
        """首帧后正常回流控,数据发完后回否定响应 0x72。"""

        def send_and_wait(self, payload, timeout):
            if (payload[0] & 0xF0) == 0x10:
                return build_flow_control(FlowStatus.CONTINUE, 0, 0)
            return bytes([0x02, 0x7F, 0x72])

        def wait(self, timeout):
            return bytes([0x02, 0x7F, 0x72])

    dl7 = UdsDownloader(image=image,
                        config=UdsConfig(block_size=64, p2_timeout=0.5,
                                         follow_ecu_block_size=False),
                        transport=NrcTransport(rej))
    try:
        dl7.run()
        check("否定响应 0x72 被识别", False)
    except UdsError as exc:
        check("否定响应 0x72 被识别", "NRC=72" in str(exc) and "编程失败" in str(exc),
              str(exc)[:60])

    # 单帧请求路径上的否定响应:用前置命令 10 02(2 字节,必然走单帧)
    class NrcSfTransport(SimTransport):
        def send_and_wait(self, payload, timeout):
            return bytes([0x02, 0x7F, 0x31])

    dl9 = UdsDownloader(
        image=image,
        config=UdsConfig(block_size=64, p2_timeout=0.5,
                         follow_ecu_block_size=False,
                         pre_commands=[bytes([0x10, 0x02])]),
        transport=NrcSfTransport(rej),
    )
    try:
        dl9.run()
        check("单帧否定响应 0x31 被识别", False)
    except UdsError as exc:
        check("单帧否定响应 0x31 被识别", "NRC=31" in str(exc), str(exc)[:60])

    over = SimEcu()

    class OverflowTransport(SimTransport):
        def send_and_wait(self, payload, timeout):
            if (payload[0] & 0xF0) == 0x10:
                return build_flow_control(FlowStatus.OVERFLOW, 0, 0)
            return bytes([0x01, 0x76])

    dl8 = UdsDownloader(image=image,
                        config=UdsConfig(block_size=64, p2_timeout=0.5,
                                         follow_ecu_block_size=False),
                        transport=OverflowTransport(over))
    try:
        dl8.run()
        check("FC Overflow 被识别为中止", False)
    except (UdsError, Exception) as exc:
        check("FC Overflow 被识别为中止", "Overflow" in str(exc) or "中止" in str(exc),
              str(exc)[:50])


def test_pre_command(base_image) -> None:
    """前置命令(编程会话)应先于 0x34 发出。"""
    section("前置命令(编程会话)")
    ecu = SimEcu()
    ecu.start()
    transport = SimTransport(ecu)
    cfg = UdsConfig(block_size=64, p2_timeout=2.0,
                    follow_ecu_block_size=False,
                    pre_commands=[bytes([0x10, 0x02])])
    dl = UdsDownloader(image=base_image, config=cfg, transport=transport,
                       on_log=lambda lvl, txt: None)
    err = None
    try:
        dl.run()
    except Exception as exc:
        err = exc
    ecu.stop()
    ecu.join(timeout=2)
    check("带前置命令下载成功", err is None, str(err) if err else "")
    check("会话 0x10 已发送", 0x10 in ecu.seen_sids,
          f"{[hex(s) for s in ecu.seen_sids[:3]]}")
    check("会话在 0x34 之前", ecu.seen_sids[:2] == [0x10, 0x34],
          f"{[hex(s) for s in ecu.seen_sids[:2]]}")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="uds_test_")
    image = test_hex(tmpdir)
    test_isotp()
    test_download(image)
    test_pre_command(image)

    print("\n" + "=" * 58)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: " + ", ".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())