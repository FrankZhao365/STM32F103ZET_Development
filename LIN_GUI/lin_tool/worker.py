"""LIN 收发工作线程。

设计要点(与仓库里的 CAN 工具保持一致):
1. 收发循环跑在独立 QThread,界面永不因总线阻塞;
2. 报文攒成批再发信号,避免每帧一个信号把事件循环淹掉;
3. 日志落盘与统计都在工作线程内完成,主线程只负责画界面。

三种工作模式:
    master —— 本机当主节点,按调度表(或手工指定的帧)轮询;
    sniff  —— 被动监听:只收不发,解码总线上现有的报文(需要另一台主节点);
    sim    —— 仿真演示:主节点轮询 + 内置仿真从节点,无需硬件。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from PyQt5.QtCore import QThread, pyqtSignal

from .logbook import LogBook
from .protocol import (
    SYNC_BYTE,
    ChecksumType,
    FrameSpec,
    LinFrame,
    protected_id,
    verify_checksum,
)
from .stats import FrameStats
from .transport import LinTimeout, LinTransport, LinTransportError

log = logging.getLogger(__name__)

MODE_MASTER = "master"
MODE_SNIFF = "sniff"
MODE_SIM = "sim"

MODE_LABELS = {
    MODE_MASTER: "主节点(轮询)",
    MODE_SNIFF: "被动监听",
    MODE_SIM: "仿真演示",
}

# 批量参数
BATCH_MAX = 128
BATCH_INTERVAL_MS = 60.0
# 统计快照广播周期
STATS_INTERVAL_MS = 500.0
# 单帧响应超时下限(ms)
MIN_RESPONSE_TIMEOUT_MS = 3.0


@dataclass
class WorkerConfig:
    """工作线程的运行参数。"""

    mode: str = MODE_MASTER
    transport: Optional[LinTransport] = None
    specs: List[FrameSpec] = field(default_factory=list)
    """要轮询/监听的帧。"""

    response_timeout_ms: float = 100.0
    """从节点应答超时。"""

    frame_gap_ms: float = 3.0
    """被动监听时,字节之间超过这个间隔就认为一帧结束。"""

    logbook: Optional[LogBook] = None

    name_lookup: Optional[Callable[[int], str]] = None
    """帧 ID -> 帧名(来自 LDF)。"""

    length_lookup: Optional[Callable[[int], Optional[int]]] = None
    """帧 ID -> 数据长度(来自 LDF);返回 None 表示长度未知。"""


class LinWorker(QThread):
    """LIN 通道后台线程。"""

    batchReady = pyqtSignal(list)
    opened = pyqtSignal(str)
    failed = pyqtSignal(str)
    closed = pyqtSignal()
    statsReady = pyqtSignal(object)
    notify = pyqtSignal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._config = WorkerConfig()
        self._stats = FrameStats()
        self._running = False
        self._stop = False
        self._last_emit = 0.0
        self._last_stats = 0.0

    # ------------------------------------------------------------ 配置

    def configure(self, config: WorkerConfig) -> None:
        """设置运行参数(必须在 start() 之前调用)。"""
        self._config = config

    def request_stop(self) -> None:
        """请求线程退出。"""
        self._stop = True

    # ------------------------------------------------------------ 主循环

    def run(self) -> None:  # noqa: C901 - 循环本身就是主流程
        config = self._config
        transport = config.transport
        if transport is None:
            self.failed.emit("没有配置传输通道")
            return

        try:
            transport.open()
        except LinTransportError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # pragma: no cover - 兜底
            log.exception("打开 LIN 通道失败")
            self.failed.emit(f"打开 LIN 通道失败: {exc}")
            return

        self._running = True
        self._stop = False
        self._last_emit = time.monotonic()
        self._last_stats = time.monotonic()
        self.opened.emit(transport.description)
        self.notify.emit("info", f"已连接 {transport.description}")

        batch: List[LinFrame] = []

        try:
            if config.mode == MODE_SNIFF:
                self._run_sniff(transport, batch)
            else:
                self._run_master(transport, batch)
        except Exception as exc:  # pragma: no cover - 兜底
            log.exception("LIN 工作线程异常")
            self.notify.emit("error", f"工作线程异常: {exc}")
        finally:
            if batch:
                self._flush(batch)
            try:
                transport.close()
            except Exception:  # pragma: no cover
                log.exception("关闭 LIN 通道失败")
            self._running = False
            self.closed.emit()

    # ------------------------------------------------------------ 主节点模式

    def _run_master(self, transport: LinTransport, batch: List[LinFrame]) -> None:
        config = self._config
        specs = config.specs or []
        if not specs:
            self.notify.emit("warn", "没有配置要轮询的帧")
            return

        while not self._stop:
            for spec in specs:
                if self._stop:
                    break
                started = time.monotonic()
                frame = self._poll_one(transport, spec)
                if frame is not None:
                    self._record(frame, batch)
                self._maybe_flush(batch)

                # 按调度表节拍等待
                slot = max(1.0, float(spec.delay_ms)) / 1000.0
                remain = slot - (time.monotonic() - started)
                if remain > 0:
                    self._sleep(remain)

            # 收一批后统一刷新界面
            self._tick(batch)

    def _poll_one(self, transport: LinTransport, spec: FrameSpec) -> Optional[LinFrame]:
        """轮询一帧:发帧头、收应答、校验。"""
        timestamp = time.time()
        pid = protected_id(spec.frame_id)
        try:
            # 告诉传输层这一帧期望的应答长度(仿真从节点需要)
            transport.set_expected_length(spec.frame_id, spec.length)
            transport.send_header(spec.frame_id)
        except Exception as exc:
            log.exception("发送帧头失败")
            return self._error_frame(spec, timestamp, f"发送失败: {exc}")

        timeout = self._timeout_for(spec)
        try:
            raw = transport.read_bytes(spec.length + 1, timeout)
        except LinTimeout as exc:
            return self._error_frame(spec, timestamp, "应答超时", partial=exc.partial)
        except LinTransportError as exc:
            return self._error_frame(spec, timestamp, str(exc))
        except Exception as exc:  # pragma: no cover
            log.exception("读取应答失败")
            return self._error_frame(spec, timestamp, f"读取失败: {exc}")

        data, checksum, valid = self._interpret(raw, spec, pid)

        # 主节点会收到自己发出的帧头(回显),这会让字节流整体错位。
        # 看到"像是回显"的前缀时,再补读几个字节重新判定。
        if not valid and self._looks_like_echo(raw, pid):
            for _ in range(3):
                try:
                    raw += transport.read_bytes(1, min(0.005, timeout))
                except LinTimeout:
                    break
                except LinTransportError:
                    break
                data, checksum, valid = self._interpret(raw, spec, pid)
                if valid:
                    break

        return LinFrame(
            timestamp=timestamp,
            frame_id=spec.frame_id,
            data=data,
            direction="RX",
            checksum=checksum,
            checksum_valid=valid,
            checksum_type=spec.checksum_type,
            name=spec.name or self._lookup_name(spec.frame_id),
            protected_id=pid,
            note="" if valid else "校验和错误",
        )

    def _timeout_for(self, spec: FrameSpec) -> float:
        """应答超时:不能超过该帧的槽位时间,否则会打乱调度表。"""
        configured = max(MIN_RESPONSE_TIMEOUT_MS, float(self._config.response_timeout_ms))
        slot = max(MIN_RESPONSE_TIMEOUT_MS, float(spec.delay_ms) - 2.0)
        return min(configured, slot) / 1000.0

    @staticmethod
    def _looks_like_echo(raw: bytes, pid: int) -> bool:
        """判断收到的字节开头是不是"自己发出的帧头"的回显。"""
        if not raw:
            return False
        if raw[0] == 0x00:  # BREAK 回显
            return True
        if len(raw) >= 2 and raw[0] == SYNC_BYTE and raw[1] == pid:  # SYNC+ID 回显
            return True
        return False

    @staticmethod
    def _interpret(raw: bytes, spec: FrameSpec, pid: int) -> Tuple[bytes, int, bool]:
        """从收到的字节里拆出"数据 + 校验和"。

        主节点自己发出的帧头也会被本地 UART 收到(回显),
        所以这里把几种可能的回显前缀都试一遍,用校验和来判定哪种解释正确。
        """
        expected = spec.length + 1
        prefixes = [
            b"",
            b"\x00",
            bytes([SYNC_BYTE, pid]),
            b"\x00" + bytes([SYNC_BYTE, pid]),
        ]

        fallback: Optional[Tuple[bytes, int, bool]] = None
        for prefix in prefixes:
            if prefix and not raw.startswith(prefix):
                continue
            body = raw[len(prefix) :]
            if len(body) < expected:
                continue
            candidate = body[:expected]
            data = candidate[:-1]
            checksum = candidate[-1]
            valid = verify_checksum(data, checksum, pid, spec.checksum_type)
            if valid:
                return data, checksum, True
            # 长度刚好吻合,说明回显剥离对了,优先用它兜底
            if fallback is None or len(body) == expected:
                fallback = (data, checksum, False)

        if fallback is not None:
            return fallback
        return b"", 0, False

    def _error_frame(
        self,
        spec: FrameSpec,
        timestamp: float,
        note: str,
        partial: bytes = b"",
    ) -> LinFrame:
        """构造一个错误帧(超时/校验失败),让界面上也能看到。"""
        return LinFrame(
            timestamp=timestamp,
            frame_id=spec.frame_id,
            data=b"",
            direction="RX",
            checksum=0,
            checksum_valid=False,
            checksum_type=spec.checksum_type,
            name=spec.name or self._lookup_name(spec.frame_id),
            protected_id=protected_id(spec.frame_id),
            note=note,
        )

    # ------------------------------------------------------------ 监听模式

    def _run_sniff(self, transport: LinTransport, batch: List[LinFrame]) -> None:
        """被动监听:只收不发,从字节流里还原帧。"""
        config = self._config
        state = "BREAK"
        pid = 0
        data = bytearray()
        last_byte_ts = 0.0

        self.notify.emit(
            "info", "已进入被动监听模式:只接收总线上已有的报文,不会发送任何数据"
        )

        while not self._stop:
            try:
                chunk = transport.read_available(0.05)
            except LinTransportError as exc:
                self.notify.emit("error", f"监听出错: {exc}")
                break
            except Exception as exc:  # pragma: no cover
                log.exception("监听读取失败")
                self.notify.emit("error", f"监听出错: {exc}")
                break

            now = time.monotonic()
            if chunk:
                for byte in chunk:
                    last_byte_ts = now
                    if state == "BREAK":
                        if byte == 0x00:
                            state = "SYNC"
                    elif state == "SYNC":
                        if byte == SYNC_BYTE:
                            state = "PID"
                        else:
                            state = "BREAK"
                    elif state == "PID":
                        pid = byte
                        data = bytearray()
                        state = "DATA"
                    elif state == "DATA":
                        data.append(byte)

            # 有帧长度信息时按长度收,否则按"字节间隔"判断一帧结束
            if state == "DATA" and data:
                frame_id = pid & 0x3F
                expected_len = self._expected_length(frame_id)
                if expected_len is not None:
                    if len(data) >= expected_len + 1:
                        frame = self._build_sniffed_frame(frame_id, pid, bytes(data), expected_len)
                        if frame is not None:
                            self._record(frame, batch)
                            self._maybe_flush(batch)
                        state = "BREAK"
                        data = bytearray()
                else:
                    gap = (now - last_byte_ts) * 1000.0
                    if gap >= config.frame_gap_ms:
                        frame = self._build_sniffed_frame(frame_id, pid, bytes(data), None)
                        if frame is not None:
                            self._record(frame, batch)
                            self._maybe_flush(batch)
                        state = "BREAK"
                        data = bytearray()

            self._maybe_flush(batch)
            self._tick(batch)

    def _expected_length(self, frame_id: int) -> Optional[int]:
        """从 LDF 查帧的数据长度;查不到返回 None。"""
        lookup = self._config.length_lookup
        if lookup is None:
            return None
        try:
            return lookup(frame_id)
        except Exception:  # pragma: no cover
            return None

    def _build_sniffed_frame(
        self, frame_id: int, pid: int, raw: bytes, expected_len: Optional[int]
    ) -> Optional[LinFrame]:
        """把收到的一串字节整理成一帧。"""
        if not raw:
            return None
        if expected_len is not None:
            if len(raw) < expected_len + 1:
                return None
            data = raw[:expected_len]
            checksum = raw[expected_len]
        else:
            # 长度未知:最后一个字节当校验和
            data = raw[:-1]
            checksum = raw[-1]

        checksum_type = self._checksum_type_for(frame_id)
        valid = verify_checksum(data, checksum, pid, checksum_type)
        return LinFrame(
            timestamp=time.time(),
            frame_id=frame_id,
            data=data,
            direction="RX",
            checksum=checksum,
            checksum_valid=valid,
            checksum_type=checksum_type,
            name=self._lookup_name(frame_id),
            protected_id=pid,
            note="" if valid else "校验和错误",
        )

    def _checksum_type_for(self, frame_id: int) -> ChecksumType:
        """校验和类型:诊断帧用经典,其余按 LIN 2.x 用增强。"""
        for spec in self._config.specs:
            if spec.frame_id == frame_id:
                return spec.checksum_type
        if frame_id in (0x3C, 0x3D):
            return ChecksumType.CLASSIC
        return ChecksumType.ENHANCED

    # ------------------------------------------------------------ 公共

    def _lookup_name(self, frame_id: int) -> str:
        lookup = self._config.name_lookup
        if lookup is None:
            return ""
        try:
            return lookup(frame_id) or ""
        except Exception:  # pragma: no cover
            return ""

    def _record(self, frame: LinFrame, batch: List[LinFrame]) -> None:
        """记录一帧:统计 + 日志 + 待发送批次。"""
        self._stats.add(frame)
        if self._config.logbook is not None:
            self._config.logbook.write([frame])
        batch.append(frame)

    def _maybe_flush(self, batch: List[LinFrame]) -> None:
        if len(batch) >= BATCH_MAX:
            self._flush(batch)

    def _flush(self, batch: List[LinFrame]) -> None:
        if not batch:
            return
        frames = list(batch)
        batch.clear()
        self.batchReady.emit(frames)

    def _tick(self, batch: List[LinFrame]) -> None:
        """按时间节流:批量刷界面 + 广播统计快照。"""
        now = time.monotonic()
        if now - self._last_emit >= BATCH_INTERVAL_MS / 1000.0:
            self._flush(batch)
            self._last_emit = now
        if now - self._last_stats >= STATS_INTERVAL_MS / 1000.0:
            self._emit_stats()
            self._last_stats = now

    def _emit_stats(self) -> None:
        snapshot = {
            "rows": self._stats.snapshot(),
            "rx": self._stats.total_rx,
            "tx": self._stats.total_tx,
            "errors": self._stats.total_errors,
            "elapsed": self._stats.elapsed_s(),
        }
        self.statsReady.emit(snapshot)

    def _sleep(self, seconds: float) -> None:
        """可被 stop 请求打断的等待。"""
        deadline = time.monotonic() + seconds
        while not self._stop:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return
            time.sleep(min(0.02, remain))

    def shutdown(self) -> None:
        """请求停止并等待线程退出。"""
        self.request_stop()
        self.wait(3000)
