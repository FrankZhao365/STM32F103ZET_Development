"""UDS 诊断下载流程 (ISO 14229-1)。

    0x34 RequestDownload        申请下载,ECU 回复最大块长度
    0x36 TransferData           按块发送,块序号 1,2,3...
    0x37 RequestTransferExit    结束传输

每块数据都远超单帧容量,因此 0x36 走 ISO-TP 多帧:

    客户端 -> 首帧(FF)         携带总长度 + 前 4 字节
    ECU    -> 流控(FC)         BS = 每多少帧要再要一次流控, STmin = 帧间隔
    客户端 -> 连续帧(CF) ...   只发不等;每发满 BS 个要停下来等下一个 FC
    ECU    -> UDS 肯定响应     全部数据发完后才回

方向容易搞反:FC 是**接收方**发出的,客户端只能等,不能自己发 FC。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .hexfile import HexImage
from .isotp import (
    CONSECUTIVE_FRAME_DATA,
    FIRST_CONSECUTIVE_SEQ,
    FIRST_FRAME_DATA,
    FlowStatus,
    IsoTpError,
    IsoTpReassembler,
    IsoTpTimeout,
    build_single_frame,
    parse_flow_control,
)

log = logging.getLogger(__name__)

SID_REQUEST_DOWNLOAD = 0x34
SID_TRANSFER_DATA = 0x36
SID_REQUEST_TRANSFER_EXIT = 0x37
SID_POSITIVE_RESPONSE = 0x40
SID_NEGATIVE_RESPONSE = 0x7F
SID_RESPONSE_PENDING = 0x78

# 一次 0x36 请求的头部:服务 ID(1)+ 块序号(1)
TRANSFER_DATA_HEADER_LEN = 2
# ISO-TP 单条消息最长 4095 字节,减掉上面的头部
MAX_BLOCK_DATA = 4095 - TRANSFER_DATA_HEADER_LEN

# 否定响应码 -> 中文说明
NRC_TEXT = {
    0x10: "通用拒绝 generalReject",
    0x11: "服务不支持 serviceNotSupported",
    0x12: "子功能不支持 subFunctionNotSupported",
    0x13: "长度或格式错误 incorrectMessageLengthOrInvalidFormat",
    0x22: "条件不满足 conditionsNotCorrect",
    0x24: "请求顺序错误 requestSequenceError",
    0x31: "请求超出范围 requestOutOfRange",
    0x33: "安全访问拒绝 securityAccessDenied",
    0x35: "无效密钥 invalidKey",
    0x36: "超出尝试次数 exceedNumberOfAttempts",
    0x37: "延时计时器已过期 requiredTimeDelayNotExpired",
    0x70: "上传下载条件不满足 uploadDownloadConditionsNotCorrect",
    0x71: "不支持的传输类型 transferDataTypeNotSupported",
    0x72: "通用编程失败 generalProgrammingFailure",
    0x73: "传输数据挂起 transferDataSuspended",
    0x78: "响应待定 requestCorrectButResponsePending",
    0x7E: "当前会话不支持该子功能 subFunctionNotSupportedInActiveSession",
    0x7F: "当前会话不支持该服务 serviceNotSupportedInActiveSession",
}


class UdsError(Exception):
    """UDS 流程错误(消息已带可读说明)。"""


class UdsTransport:
    """UDS 需要的传输能力,由 CanWorker 适配实现。"""

    def send(self, payload: bytes) -> None:
        """只发不收。"""

    def send_and_wait(self, payload: bytes, timeout: float) -> Optional[bytes]:
        """发一帧并等一帧响应;超时返回 None。"""

    def wait(self, timeout: float) -> Optional[bytes]:
        """只等接收,不发送。"""


class CanWorkerTransport(UdsTransport):
    """把 CanWorker 适配成 UdsTransport。

    PCAN 同一通道不能被打开两次,所以下载流程复用监控用的那条总线,
    而不是自己再开一个 —— 这样刷写过程中的所有帧依然会出现在监控表和日志里。
    """

    def __init__(self, worker, tx_id: int, rx_id: int, extended_id: bool = False) -> None:
        self._worker = worker
        self._tx_id = tx_id
        self._rx_id = rx_id
        self._extended = extended_id

    def _message(self, payload: bytes):
        import can

        return can.Message(
            timestamp=time.time(),
            arbitration_id=self._tx_id,
            is_extended_id=self._extended,
            is_rx=False,
            data=payload,
        )

    def send(self, payload: bytes) -> None:
        self._worker.send(self._message(payload))

    def send_and_wait(self, payload: bytes, timeout: float) -> Optional[bytes]:
        rsp = self._worker.request(
            self._message(payload), timeout=timeout, expect_id=self._rx_id
        )
        return bytes(rsp.data) if rsp is not None else None

    def wait(self, timeout: float) -> Optional[bytes]:
        rsp = self._worker.receive(timeout=timeout, expect_id=self._rx_id)
        return bytes(rsp.data) if rsp is not None else None


@dataclass
class UdsConfig:
    """UDS 下载参数。"""

    tx_id: int = 0x737
    rx_id: int = 0x736
    extended_id: bool = False

    # RequestDownload 地址/长度格式:高 4 位 = 地址字节数,低 4 位 = 长度字节数
    address_bytes: int = 4
    size_bytes: int = 4

    # 每次 TransferData 携带的数据字节数(不含 0x36 与块序号这 2 字节)
    block_size: int = 0x3F8

    # True: 收到 0x34 正响应后,直接采用 ECU 声明的 maxNumberOfBlockLength;
    # False: 用下面的 block_size,但仍会被 ECU 的上限夹住
    follow_ecu_block_size: bool = True

    # ECU 返回的 maxNumberOfBlockLength 是否**包含** 0x36 服务头与块序号这 2 字节。
    # 本项目实测的 ECU(如 0x74 00 04 02 = 1026)是包含的,所以默认 True。
    # 若 ECU 是"仅数据字节",把它改成 False。
    ecu_block_len_includes_header: bool = True

    p2_timeout: float = 1.0         # 等 ECU 响应的超时
    p2_star_timeout: float = 10.0   # 出现 0x78 之后的等待上限
    s3_timeout: float = 0.0         # 两条请求之间的额外间隔(ECU 常要求 >=75ms)

    # 下载前先发的原始帧,例如进入编程会话 10 02 / 安全解锁 27 01 1234
    pre_commands: List[bytes] = field(default_factory=list)
    inter_frame_delay_ms: float = 0.0  # 连续帧之间的额外延时(忙等式协议用)


@dataclass
class UdsProgress:
    """进度快照。"""

    phase: str = ""
    sent_bytes: int = 0
    total_bytes: int = 0
    block_index: int = 0
    block_total: int = 0
    detail: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, self.sent_bytes * 100.0 / self.total_bytes)


class UdsDownloader:
    """执行 0x34 / 0x36 / 0x37 下载流程。

    可以脱离 CAN 单测 —— 只要提供一个 UdsTransport 的假实现。
    """

    def __init__(
        self,
        image: HexImage,
        config: UdsConfig,
        transport: UdsTransport,
        on_progress: Optional[Callable[[UdsProgress], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.image = image
        self.cfg = config
        self.tp = transport
        self._on_progress = on_progress
        self._on_log = on_log

        self._abort = threading.Event()
        # 0x34 正响应里声明的 maxNumberOfBlockLength;0 = 还没拿到
        self._ecu_block_len = 0
        # 实际用于传输的每块数据字节数(界面会显示)
        self.effective_block_size = config.block_size

    # ------------------------------------------------------------ 辅助

    def abort(self) -> None:
        """请求中止(线程安全)。"""
        self._abort.set()

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    def _log(self, level: str, text: str) -> None:
        log.log(logging.INFO if level == "info" else logging.WARNING, text)
        if self._on_log:
            self._on_log(level, text)

    def _progress(self, **kw) -> None:
        if self._on_progress:
            self._on_progress(UdsProgress(**kw))

    def _check_abort(self) -> None:
        if self._abort.is_set():
            raise UdsError("已被用户中止")

    @staticmethod
    def _describe_nrc(nrc: int) -> str:
        return f"{nrc:02X} ({NRC_TEXT.get(nrc, '未知响应码')})"

    # ------------------------------------------------------------ UDS 层

    def _request(self, payload: bytes) -> bytes:
        """发一条 UDS 请求,返回去掉 PCI 的响应数据(已校验肯定/否定)。"""
        deadline = time.monotonic() + self.cfg.p2_star_timeout
        while True:
            self._check_abort()
            body = self._exchange(payload, deadline)

            if not body:
                raise UdsError("收到空响应")
            sid = body[0]

            if sid == SID_RESPONSE_PENDING:
                # 0x78:ECU 说"还没好",继续等最终的肯定响应
                self._log("info", "  ECU 返回 0x78(响应待定),继续等待…")
                continue

            if sid == SID_NEGATIVE_RESPONSE:
                nrc = body[1] if len(body) > 1 else 0x00
                raise UdsError(f"ECU 否定响应 NRC={self._describe_nrc(nrc)}")

            # UDS 肯定响应 ID = 请求 ID + 0x40
            # (0x34->0x74, 0x36->0x76, 0x37->0x77)
            expected = (payload[0] + SID_POSITIVE_RESPONSE) & 0xFF
            if sid == expected:
                return body
            raise UdsError(
                f"响应 ID 0x{sid:02X} 与请求 0x{payload[0]:02X} 不匹配"
                f"(期望 0x{expected:02X})"
            )

    def _exchange(self, payload: bytes, deadline: float) -> bytes:
        """发一条 UDS 请求(含 ISO-TP 分帧),收齐响应并剥掉 PCI 头。"""
        if len(payload) <= 7:
            # 单帧也必须加 ISO-TP 头(0x0N + 数据)。
            # 裸发会被对端当成 PCI 误解析 —— 例如 0x37 首字节高半字节是 0x3,
            # 会被当成流控帧直接丢掉。
            raw = self.tp.send_and_wait(build_single_frame(payload), self.cfg.p2_timeout)
            if raw is None:
                raise IsoTpTimeout(f"等待 ECU 响应超时({self.cfg.p2_timeout:.1f}s)")
            return self._strip_single_frame(raw)

        self._send_multiframe(payload)

        # 全部数据发完后 ECU 才回 UDS 响应
        rx = IsoTpReassembler()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsoTpTimeout("接收多帧响应超时")
            self._check_abort()
            raw = self.tp.wait(min(remaining, 0.2))
            if raw is None:
                continue
            if (raw[0] & 0xF0) == 0x30:  # 流控帧,忽略
                continue
            result = rx.feed(raw)
            if result is not None:
                return result

    @staticmethod
    def _strip_single_frame(raw: bytes) -> bytes:
        """从单帧响应里取出 UDS 数据。"""
        if len(raw) >= 2 and (raw[0] & 0xF0) == 0x00:
            length = raw[0] & 0x0F
            if length and len(raw) >= 1 + length:
                return raw[1 : 1 + length]
            raise UdsError(f"单帧响应长度非法: {raw.hex(' ')}")
        if (raw[0] & 0xF0) == 0x10:
            raise UdsError("收到首帧,但本流程只请求单帧响应")
        raise UdsError(f"无法识别的响应帧: {raw.hex(' ')}")

    # ------------------------------------------------------------ ISO-TP 发送

    def _send_multiframe(self, payload: bytes) -> None:
        """ISO-TP 多帧发送:FF -> 等 FC -> CF ...。UDS 响应由调用方接收。"""
        if len(payload) > 0xFFF:
            raise IsoTpError(f"载荷 {len(payload)} 字节超过 ISO-TP 单消息上限 4095")

        # 1. 首帧(PCI 2 字节,带 6 字节数据),并等流控
        ff = bytes(
            [0x10 | ((len(payload) >> 8) & 0x0F), len(payload) & 0xFF]
        ) + payload[:FIRST_FRAME_DATA]
        fc = self._wait_flow_control(self.tp.send_and_wait(ff, self.cfg.p2_timeout))

        # 2. 连续帧:只发不等;每发满 BS 个停下来等下一个 FC
        rest = payload[FIRST_FRAME_DATA:]
        idx = 0
        seq = FIRST_CONSECUTIVE_SEQ
        since_fc = 0
        while idx < len(rest):
            self._check_abort()
            chunk = rest[idx : idx + CONSECUTIVE_FRAME_DATA]
            self.tp.send(bytes([0x20 | seq]) + chunk)
            idx += len(chunk)
            seq = (seq + 1) & 0x0F
            since_fc += 1

            if fc.st_min_ms > 0:
                time.sleep(fc.st_min_ms / 1000.0)
            if self.cfg.inter_frame_delay_ms > 0:
                time.sleep(self.cfg.inter_frame_delay_ms / 1000.0)

            if fc.block_size and since_fc >= fc.block_size and idx < len(rest):
                # 收方要求先停一下 —— 必须等 ECU 的下一个 FC,客户端不能自己发 FC
                fc = self._wait_flow_control(self._wait_raw(self.cfg.p2_timeout))
                since_fc = 0

    def _wait_raw(self, timeout: float) -> bytes:
        raw = self.tp.wait(timeout)
        if raw is None:
            raise IsoTpTimeout(f"等待流控帧超时({timeout:.1f}s)")
        return raw

    def _wait_flow_control(self, raw: Optional[bytes]) -> object:
        """解析流控帧;必要时按 Wait 指示再等一次。"""
        if raw is None:
            raise IsoTpTimeout(f"等待 ECU 流控帧超时({self.cfg.p2_timeout:.1f}s)")
        fc = parse_flow_control(raw)
        if fc.status == FlowStatus.OVERFLOW:
            raise IsoTpError("ECU 要求中止传输(FC = Overflow)")
        if fc.status == FlowStatus.WAIT:
            # ECU 让我们等一会儿,按它给的时间再要一次流控
            time.sleep(max(fc.st_min_ms, 1.0) / 1000.0)
            return self._wait_flow_control(self._wait_raw(self.cfg.p2_timeout))
        return fc

    # ------------------------------------------------------------ 主流程

    def run(self) -> None:
        """执行下载。协议层异常统一转成 UdsError,调用方只需捕获一种类型。"""
        try:
            self._run()
        except UdsError:
            raise
        except IsoTpError as exc:
            raise UdsError(str(exc)) from exc

    def _run(self) -> None:  # noqa: C901 - 下载流程本身就是顺序步骤
        cfg = self.cfg
        data = self.image.data
        total = len(data)

        block = max(1, min(cfg.block_size, self._ecu_block_len))
        block_total = (total + block - 1) // block

        self._progress(phase="准备", total_bytes=total, block_total=block_total)

        # ---- 前置命令(进编程会话 / 安全解锁等) ----
        for i, raw in enumerate(cfg.pre_commands, 1):
            self._check_abort()
            self._log("info", f"前置命令 {i}/{len(cfg.pre_commands)}: {raw.hex(' ')}")
            rsp = self._request(raw)
            self._log("info", f"    -> {rsp.hex(' ')}")
            time.sleep(cfg.s3_timeout)

        # ---- 0x34 RequestDownload ----
        self._progress(phase="0x34 申请下载", total_bytes=total, block_total=block_total)
        req = bytearray([SID_REQUEST_DOWNLOAD, 0x00])
        req.append(((cfg.address_bytes & 0x0F) << 4) | (cfg.size_bytes & 0x0F))
        req.extend(self.image.base_address.to_bytes(cfg.address_bytes, "big"))
        req.extend(total.to_bytes(cfg.size_bytes, "big"))

        self._log("info", f"0x34 RequestDownload: {req.hex(' ')}")
        self._log("info", f"    地址 0x{self.image.base_address:08X},长度 {total:,} 字节")
        rsp = self._request(bytes(req))
        self._log("info", f"    -> {rsp.hex(' ')}")

        if len(rsp) >= 4:
            # 0x34 正响应: [0x74, dataFormatIdentifier, maxBlockLen_H, maxBlockLen_L]
            # 从第 2 个参数字节开始读,别把 dataFormatIdentifier 算进去
            declared = int.from_bytes(rsp[2:4], "big")
            self._log("info", f"    ECU maxNumberOfBlockLength = {declared} 字节")

            # 该值是否含 0x36 + 块序号这 2 字节,各家 ECU 实现不统一
            header = TRANSFER_DATA_HEADER_LEN if cfg.ecu_block_len_includes_header else 0
            ecu_data = declared - header

            if ecu_data < 1:
                # ECU 没给出可用上限(或声明值过小),退回用户设置
                self._log(
                    "warn",
                    f"    ECU 声明的块长度不可用({declared}),沿用设置值 {cfg.block_size} 字节",
                )
                ecu_data = 0
            else:
                if header:
                    self._log(
                        "info",
                        f"    该值含 0x36 与块序号 -> 每块实际数据 {ecu_data} 字节",
                    )
                # ISO-TP 单条消息上限 4095,减掉 2 字节服务头
                if ecu_data > MAX_BLOCK_DATA:
                    self._log(
                        "warn",
                        f"    {ecu_data} 字节超过 ISO-TP 上限,夹到 {MAX_BLOCK_DATA} 字节",
                    )
                    ecu_data = MAX_BLOCK_DATA
                self._ecu_block_len = ecu_data

            if ecu_data > 0:
                if cfg.follow_ecu_block_size:
                    block = ecu_data
                    self._log("info", f"    采用 ECU 块大小: {block} 字节")
                else:
                    block = max(1, min(cfg.block_size, ecu_data))
                    if block != cfg.block_size:
                        self._log(
                            "warn",
                            f"    块大小被 ECU 夹住: {cfg.block_size} -> {block} 字节",
                        )
                    else:
                        self._log("info", f"    采用设置值: {block} 字节")
            else:
                block = max(1, min(cfg.block_size, MAX_BLOCK_DATA))

            self.effective_block_size = block
            block_total = (total + block - 1) // block
            self._log(
                "info",
                f"    共 {total:,} 字节 / 每块 {block} 字节 = {block_total} 块",
            )
        else:
            # 有些 ECU 的 0x34 响应只带 dataFormatIdentifier,不声明块长度
            self._log(
                "warn",
                f"    0x34 响应只有 {len(rsp)} 字节,未声明块长度,"
                f"沿用设置值 {cfg.block_size} 字节",
            )

        # ---- 0x36 TransferData 循环 ----
        sent = 0
        block_index = 0
        last_ts = 0.0
        while sent < total:
            self._check_abort()
            block_index += 1
            chunk = data[sent : sent + block]
            payload = bytes([SID_TRANSFER_DATA, block_index & 0xFF]) + chunk

            if last_ts and cfg.s3_timeout > 0:
                gap = cfg.s3_timeout - (time.monotonic() - last_ts)
                if gap > 0:
                    time.sleep(gap)

            self._progress(
                phase="0x36 传输数据",
                sent_bytes=sent,
                total_bytes=total,
                block_index=block_index,
                block_total=block_total,
                detail=f"0x{sent:08X}",
            )

            try:
                self._exchange(payload, time.monotonic() + cfg.p2_star_timeout)
            except IsoTpError as exc:
                self._log("error", f"块 {block_index}/{block_total} 失败: {exc}")
                raise UdsError(f"块 {block_index}/{block_total} 传输失败: {exc}") from exc

            sent += len(chunk)
            last_ts = time.monotonic()

            if block_index % 16 == 0 or block_index == block_total:
                self._log(
                    "info",
                    f"0x36 进度 {block_index}/{block_total} 块, "
                    f"{sent:,}/{total:,} 字节 ({sent * 100.0 / total:.1f}%)",
                )

        # ---- 0x37 RequestTransferExit ----
        self._progress(
            phase="0x37 结束传输",
            sent_bytes=sent,
            total_bytes=total,
            block_index=block_index,
            block_total=block_total,
        )
        self._log("info", "0x37 RequestTransferExit")
        rsp = self._request(bytes([SID_REQUEST_TRANSFER_EXIT]))
        self._log("info", f"    -> {rsp.hex(' ')}")

        self._progress(
            phase="完成",
            sent_bytes=sent,
            total_bytes=total,
            block_index=block_index,
            block_total=block_total,
        )
        self._log("info", f"下载完成: {sent:,} 字节 / {block_total} 块")