"""LIN 传输层:把帧头/帧响应变成实际的总线收发。

提供两种传输实现:

* `SerialLinTransport` —— 通过 USB 转串口 + LIN 收发器访问真实总线。
  BREAK 域有两种生成方式,与上游 Linux LIN 驱动(sllin)一致:
    - "control":拉低 UART 的 BREAK 控制位保持低电平(FTDI/CP210x/CH340 支持);
    - "baud"   :临时把波特率降到 2/3,发送一个 0x00 字节,这样低电平
                 持续 9 × 1.5 = 13.5 个正常位时间,正好落在规范要求的
                 11~26 位时间之内,随后恢复波特率。
* `SimulatedLinTransport` —— 内置仿真从节点,没有硬件时也能验证整条链路,
  同时用于自动化测试。
"""

from __future__ import annotations

import abc
import logging
import time
from typing import Callable, Dict, List, Optional

from .protocol import (
    SYNC_BYTE,
    ChecksumType,
    checksum,
    protected_id,
)

log = logging.getLogger(__name__)

#: BREAK 域的目标位时间(规范 11~26,工程常用 13)
DEFAULT_BREAK_BITS = 13


class LinTransportError(Exception):
    """传输层错误。"""


class LinTimeout(LinTransportError):
    """读取超时。`partial` 里是已收到的字节。"""

    def __init__(self, message: str, partial: bytes = b"") -> None:
        super().__init__(message)
        self.partial = partial


class LinTransport(abc.ABC):
    """LIN 传输层接口。"""

    #: 供界面显示的描述
    description: str = "LIN"

    @abc.abstractmethod
    def open(self) -> None:
        """打开通道。失败时抛 `LinTransportError`。"""

    @abc.abstractmethod
    def close(self) -> None:
        """关闭通道(可重复调用)。"""

    @property
    @abc.abstractmethod
    def is_open(self) -> bool:
        """通道是否已打开。"""

    @abc.abstractmethod
    def send_header(self, frame_id: int) -> None:
        """发送帧头:BREAK + SYNC + 受保护 ID。"""

    def set_expected_length(self, frame_id: int, length: int) -> None:
        """告知传输层某个帧的应答数据长度。

        真实总线上这个长度由从节点自己决定(写在 LDF 里),
        只有仿真从节点需要主节点告诉它"这一帧应该有多长"。
        """

    @abc.abstractmethod
    def read_bytes(self, count: int, timeout: float) -> bytes:
        """读取指定数量的字节;超时抛 `LinTimeout`(带 partial)。"""

    def read_available(self, timeout: float) -> bytes:
        """读取当前可用的字节(最多等到 timeout),可能返回空串。"""
        raise NotImplementedError

    # ------------------------------------------------------------ 工具

    @staticmethod
    def build_response(
        data: bytes,
        protected: int,
        checksum_type: ChecksumType = ChecksumType.ENHANCED,
    ) -> bytes:
        """按协议拼出"数据 + 校验和"。"""
        return bytes(data) + bytes([checksum(data, protected, checksum_type)])


class SerialLinTransport(LinTransport):
    """USB 转串口 + LIN 收发器。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 19200,
        break_mode: str = "auto",
        break_bits: int = DEFAULT_BREAK_BITS,
        read_poll: float = 0.002,
    ) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self.break_mode = break_mode
        self.break_bits = int(break_bits)
        self.read_poll = float(read_poll)
        self._serial = None
        self._effective_break_mode = break_mode

    # ------------------------------------------------------------ 打开/关闭

    def open(self) -> None:
        try:
            import serial  # 延迟导入:没装 pyserial 时界面仍可打开
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise LinTransportError(
                "未安装 pyserial,无法使用串口模式。请执行:\n"
                "    python -m pip install -r requirements.txt"
            ) from exc

        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.read_poll,
                write_timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except Exception as exc:
            raise LinTransportError(f"打开串口 {self.port} 失败: {exc}") from exc

        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception:  # pragma: no cover - 个别驱动不支持
            pass

        # "auto" 优先用 BREAK 控制位,不支持再退回波特率切换
        if self.break_mode == "auto":
            self._effective_break_mode = "control" if self._break_control_supported() else "baud"
        else:
            self._effective_break_mode = self.break_mode

        self.description = (
            f"{self.port} @ {self.baudrate} bit/s "
            f"(BREAK:{'控制位' if self._effective_break_mode == 'control' else '波特率切换'})"
        )

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # pragma: no cover
                log.exception("关闭串口失败")
            finally:
                self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and bool(getattr(self._serial, "is_open", False))

    # ------------------------------------------------------------ BREAK

    def _break_control_supported(self) -> bool:
        if self._serial is None:
            return False
        try:
            self._serial.break_condition = True
            self._serial.break_condition = False
            return True
        except Exception:
            log.info("串口 %s 不支持 BREAK 控制位,改用波特率切换", self.port)
            return False

    def _break_time(self) -> float:
        """BREAK 域持续时间(秒)。"""
        return self.break_bits / float(self.baudrate)

    def _send_break(self) -> None:
        ser = self._serial
        if ser is None:
            raise LinTransportError("串口未打开")

        if self._effective_break_mode == "control":
            # 保持低电平一段时间;随后紧跟的 0x00/0x55 会继续拉低,
            # 总的低电平时长满足规范的 ≥11 位时间。
            ser.break_condition = True
            time.sleep(self._break_time())
            ser.break_condition = False
        else:
            # 以 2/3 波特率发送 0x00:低电平 = 9 × 1.5 = 13.5 个正常位时间
            low_baud = max(1200, (self.baudrate * 2) // 3)
            try:
                ser.baudrate = low_baud
                ser.write(b"\x00")
                ser.flush()
            finally:
                ser.baudrate = self.baudrate

    # ------------------------------------------------------------ 收发

    def send_header(self, frame_id: int) -> None:
        ser = self._serial
        if ser is None:
            raise LinTransportError("串口未打开")
        self._send_break()
        pid = protected_id(frame_id)
        ser.write(bytes([SYNC_BYTE, pid]))
        ser.flush()

    def read_bytes(self, count: int, timeout: float) -> bytes:
        ser = self._serial
        if ser is None:
            raise LinTransportError("串口未打开")
        if count <= 0:
            return b""

        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < count:
            chunk = ser.read(count - len(buf))
            if chunk:
                buf.extend(chunk)
                continue
            if time.monotonic() >= deadline:
                raise LinTimeout(
                    f"读取超时(期望 {count} 字节,收到 {len(buf)} 字节)", bytes(buf)
                )
        return bytes(buf)

    def read_available(self, timeout: float) -> bytes:
        ser = self._serial
        if ser is None:
            raise LinTransportError("串口未打开")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while True:
            waiting = ser.in_waiting
            if waiting:
                buf.extend(ser.read(waiting))
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        return bytes(buf)


class SimulatedLinTransport(LinTransport):
    """仿真从节点:收到帧头就按预置数据应答。

    没有 LIN 硬件时,用它在"仿真演示"模式下验证整个软件链路。
    """

    def __init__(
        self,
        responses: Optional[Dict[int, bytes]] = None,
        generator: Optional[Callable[[int], bytes]] = None,
        checksum_type: ChecksumType = ChecksumType.ENHANCED,
        latency_ms: float = 0.0,
    ) -> None:
        self.responses: Dict[int, bytes] = dict(responses or {})
        self.generator = generator
        self.checksum_type = checksum_type
        self.latency_ms = float(latency_ms)
        self.description = "仿真 LIN 从节点"
        self._rx = bytearray()
        self._open = False
        self.requested_ids: List[int] = []
        self._counter = 0
        self._lengths: Dict[int, int] = {}

    def open(self) -> None:
        self._open = True
        self._rx.clear()
        self.requested_ids.clear()

    def close(self) -> None:
        self._open = False
        self._rx.clear()

    @property
    def is_open(self) -> bool:
        return self._open

    def set_expected_length(self, frame_id: int, length: int) -> None:
        """记住主节点期望的应答长度,仿真应答会按这个长度生成。"""
        self._lengths[int(frame_id) & 0x3F] = max(0, min(int(length), 8))

    def send_header(self, frame_id: int) -> None:
        if not self._open:
            raise LinTransportError("仿真通道未打开")
        fid = int(frame_id) & 0x3F
        self.requested_ids.append(fid)

        if self.generator is not None:
            data = bytes(self.generator(fid))
        else:
            data = bytes(self.responses.get(fid, b""))

        pid = protected_id(fid)
        self._rx.extend(data)
        self._rx.append(checksum(data, pid, self.checksum_type))

    def read_bytes(self, count: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(timeout, 0.0) + self.latency_ms / 1000.0
        while len(self._rx) < count:
            if time.monotonic() >= deadline:
                raise LinTimeout(
                    f"读取超时(期望 {count} 字节,收到 {len(self._rx)} 字节)", bytes(self._rx)
                )
            time.sleep(0.001)
        out = bytes(self._rx[:count])
        del self._rx[:count]
        return out

    def read_available(self, timeout: float) -> bytes:
        deadline = time.monotonic() + max(timeout, 0.0)
        while not self._rx and time.monotonic() < deadline:
            time.sleep(0.001)
        out = bytes(self._rx)
        self._rx.clear()
        return out

    # ------------------------------------------------------------ 便利方法

    def set_counter_payload(self, length: int = 8) -> None:
        """让每个被轮询的帧返回一个递增计数器(演示用)。

        实际长度取主节点告知的期望长度,没有告知时用 `length`。
        """

        def _gen(fid: int) -> bytes:
            self._counter = (self._counter + 1) & 0xFFFFFFFF
            size = self._lengths.get(fid, length)
            payload = self._counter.to_bytes(4, "little") + bytes([fid & 0xFF])
            if size <= len(payload):
                return payload[:size]
            return payload + bytes(size - len(payload))

        self.generator = _gen
