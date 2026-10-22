"""CAN 收发工作线程。

设计要点:
1. 收发循环跑在独立 QThread,界面永不因总线阻塞。
2. 不为每条报文发一个 Qt 信号(那会淹没事件循环),而是把报文攒成批,
   每 `BATCH_MAX` 条或 `BATCH_INTERVAL_MS` 毫秒发一次 `batchReady`。
3. 日志落盘和统计都在工作线程内完成,主线程只负责画界面。
4. 周期发送也在工作线程内按时间片调度,精度比界面 QTimer 高。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import can
from PyQt5.QtCore import QThread, pyqtSignal

from .logbook import LogBook
from .stats import IdStats

log = logging.getLogger(__name__)

# 批量参数:兼顾实时性(延迟上限)和吞吐(信号条数)
BATCH_MAX = 256
BATCH_INTERVAL_MS = 60.0
# 空闲时阻塞读的时长,决定唤醒延迟与 CPU 占用的折中
RX_IDLE_TIMEOUT = 0.005
# 统计快照广播周期
STATS_INTERVAL_MS = 500.0

CMD_TX = "tx"
CMD_ADD_CYCLIC = "add_cyclic"
CMD_STOP_CYCLIC = "stop_cyclic"
CMD_STOP_ALL_CYCLIC = "stop_all_cyclic"


@dataclass
class CyclicTask:
    """周期发送任务。"""

    task_id: int
    msg: can.Message
    interval_ms: float
    repeat: int  # <=0 表示不限次数
    sent: int = 0
    next_due: float = 0.0
    cancelled: bool = False

    @property
    def finished(self) -> bool:
        return self.cancelled or (0 < self.repeat <= self.sent)


@dataclass
class _PendingRequest:
    """一次进行中的请求/响应等待。"""

    result_q: "queue.Queue[can.Message]"
    expect_id: Optional[int] = None
    matcher: Optional[object] = None


class CanWorker(QThread):
    """CAN 通道后台线程。"""

    batchReady = pyqtSignal(list)
    opened = pyqtSignal(str)
    failed = pyqtSignal(str)
    closed = pyqtSignal()
    statsReady = pyqtSignal(object)
    notify = pyqtSignal(str, str)  # level, message

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bus: Optional[can.BusABC] = None
        self._logbook: Optional[LogBook] = None
        self._stats = IdStats()
        self._running = False

        # 通道参数由 open_bus() 覆盖,这里给出安全默认值
        self._interface = "pcan"
        self._channel = "PCAN_USBBUS1"
        self._bitrate = 500000
        self._fd = False
        self._data_bitrate = 500000
        
        self._cmd_q: "queue.Queue[tuple]" = queue.Queue()

        # 请求/响应:供 UDS 等需要"发一帧等回复"的协议使用
        self._req_lock = threading.Lock()
        self._pending_req: Optional[_PendingRequest] = None

        self._cyclic: Dict[int, CyclicTask] = {}
        self._next_task_id = 1

        self._rx_total = 0
        self._tx_total = 0
        self._tx_errors = 0

    # ------------------------------------------------------------ 打开/关闭

    def open_bus(
        self,
        channel: str,
        bitrate: int,
        logbook: Optional[LogBook] = None,
        fd: bool = False,
        data_bitrate: Optional[int] = None,
        interface: str = "pcan",
    ) -> None:
        """初始化 CAN 通道。只能在线程启动前调用。

        `interface` 默认为 pcan;改成 "virtual" 可在没有硬件时用虚拟通道验证收发链路。
        """
        self._interface = interface
        self._channel = channel
        self._bitrate = bitrate
        self._fd = fd
        self._data_bitrate = data_bitrate or bitrate
        self._logbook = logbook

    def run(self) -> None:  # noqa: C901 - 循环本身就是主流程,拆开反而更难读
        try:
            self._bus = self._open_with_retry()
        except Exception as exc:
            log.exception("打开 CAN 通道失败")
            self.failed.emit(str(exc))
            return

        if self._bus is None:
            self.failed.emit("CAN 通道初始化失败")
            return

        self._running = True
        mode = "CAN FD" if getattr(self, "_fd", False) else "CAN"
        self.opened.emit(f"{self._channel} @ {self._bitrate} bit/s ({mode})")
        self.notify.emit("info", f"已连接 {self._channel} @ {self._bitrate}")

        batch: List[can.Message] = []
        logged = 0  # batch[:logged] 已经落盘并计入统计
        last_emit = time.monotonic()
        last_stats = time.monotonic()
        batch_interval = BATCH_INTERVAL_MS / 1000.0
        stats_interval = STATS_INTERVAL_MS / 1000.0

        try:
            while self._running:
                now = time.monotonic()

                # --- 处理界面下发的命令 -------------------------------------
                self._drain_commands(now, batch)

                # --- 周期发送 ---------------------------------------------
                self._run_cyclic(now, batch)

                # --- 接收:先阻塞一次,再把队列抽干 -----------------------
                try:
                    msg = self._bus.recv(timeout=RX_IDLE_TIMEOUT)
                except Exception as exc:
                    log.exception("接收异常")
                    self.notify.emit("error", f"接收异常: {exc}")
                    self._running = False
                    break

                if msg is not None:
                    msg.is_rx = True
                    self._rx_total += 1
                    batch.append(msg)
                    self._fulfill_pending(msg)
                    while True:
                        msg = self._bus.recv(timeout=0)
                        if msg is None:
                            break
                        msg.is_rx = True
                        self._rx_total += 1
                        batch.append(msg)
                        self._fulfill_pending(msg)

                # --- 落盘 + 统计(只处理尚未记录的新增部分) ----------------
                # batch 会一直保留到推送给界面为止,必须用 logged 游标避免重复写入
                if logged < len(batch):
                    if self._logbook is not None:
                        self._logbook.write(batch[logged:])
                    for m in batch[logged:]:
                        self._stats.add(m)
                    logged = len(batch)

                # --- 批量推送给界面 ---------------------------------------
                mono = time.monotonic()
                if batch and (len(batch) >= BATCH_MAX or (mono - last_emit) >= batch_interval):
                    self.batchReady.emit(batch)
                    batch = []
                    logged = 0
                    last_emit = mono

                if (mono - last_stats) >= stats_interval:
                    self.statsReady.emit(self._make_stats_snapshot())
                    last_stats = mono

                # 命令队列非空时不要睡太久,保证界面操作响应及时
                if not batch and not self._cmd_q.empty():
                    continue

            # --- 退出前把剩余内容处理干净 ---------------------------------
            if logged < len(batch):
                if self._logbook is not None:
                    self._logbook.write(batch[logged:])
                for m in batch[logged:]:
                    self._stats.add(m)
            if batch:
                self.batchReady.emit(batch)
        finally:
            self._teardown()
            self.closed.emit()

    def _open_with_retry(self, attempts: int = 3, delay: float = 0.6):
        """打开通道,失败时重试。

        PCAN 驱动释放通道并非瞬时完成 —— 刚关闭本工具(或 PCAN-View)就立刻重开,
        会撞上 `A PCAN Channel has not been initialized yet`。这个错误重试几次
        通常就好了,所以这里自动兜一下,而不是直接把错误抛给用户。
        """
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                return self._make_bus()
            except Exception as exc:
                last_exc = exc
                if attempt < attempts:
                    self.notify.emit(
                        "warn", f"打开通道失败,正在重试 ({attempt}/{attempts - 1}): {exc}"
                    )
                    time.sleep(delay)
        raise last_exc

    def _make_bus(self):
        interface = getattr(self, "_interface", "pcan")
        kwargs = {"interface": interface, "channel": self._channel}
        if interface == "pcan":
            kwargs["bitrate"] = self._bitrate
            if getattr(self, "_fd", False):
                kwargs["fd"] = True
                kwargs["data_bitrate"] = getattr(self, "_data_bitrate", self._bitrate)
        return can.interface.Bus(**kwargs)

    def _teardown(self) -> None:
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                log.exception("关闭 CAN 通道失败")
            self._bus = None
        if self._logbook is not None:
            self._logbook.close()
        self._running = False
        # 唤醒所有等待中的 request()/receive(),否则刷写线程会一直阻塞到超时
        self.wake_pending_requests()

    def shutdown_bus(self) -> None:
        """请求线程退出(需先 wait,再调 shutdown)。"""
        self._running = False

    # ------------------------------------------------------------ 命令处理

    def _drain_commands(self, now: float, batch: List[can.Message]) -> None:
        while True:
            try:
                cmd = self._cmd_q.get_nowait()
            except queue.Empty:
                return
            kind = cmd[0]
            if kind == CMD_TX:
                self._send_one(cmd[1], batch)
            elif kind == CMD_ADD_CYCLIC:
                interval_ms, msg, repeat = cmd[1], cmd[2], cmd[3]
                task = CyclicTask(
                    task_id=self._next_task_id,
                    msg=msg,
                    interval_ms=max(float(interval_ms), 1.0),
                    repeat=int(repeat),
                    next_due=now,
                )
                self._next_task_id += 1
                self._cyclic[task.task_id] = task
                self.notify.emit(
                    "info", f"周期发送已启动: ID 0x{msg.arbitration_id:X} @ {task.interval_ms:g} ms"
                )
            elif kind == CMD_STOP_CYCLIC:
                task = self._cyclic.get(cmd[1])
                if task:
                    task.cancelled = True
            elif kind == CMD_STOP_ALL_CYCLIC:
                for task in self._cyclic.values():
                    task.cancelled = True

    # ---------------------------------------------------------- 请求/响应

    def _fulfill_pending(self, msg: can.Message) -> bool:
        """如果收到的报文命中当前等待的请求,把它交给等待方。"""
        with self._req_lock:
            pending = self._pending_req
            if pending is None:
                return False
            if pending.expect_id is not None and msg.arbitration_id != pending.expect_id:
                return False
            if pending.matcher is not None and not pending.matcher(msg):
                return False
            self._pending_req = None
        try:
            pending.result_q.put_nowait(msg)
        except queue.Full:
            pass
        return True

    def request(
        self,
        msg: can.Message,
        timeout: float = 5.0,
        expect_id: Optional[int] = None,
        matcher: Optional[object] = None,
    ) -> Optional[can.Message]:
        """发一帧并等待响应,超时返回 None。

        **阻塞调用,只能在非界面线程里用**(UDS 下载流程就是这么用的)。

        Args:
            msg: 要发出的报文
            timeout: 等待秒数
            expect_id: 只接受这个 CAN ID 的响应;None 表示接受任意 ID
            matcher: 可选的额外过滤函数 ``fn(can.Message) -> bool``

        注意:PCAN 同一通道不能被打开两次,所以 UDS 流程复用本线程的总线,
        而不是自己再开一个。
        """
        if not self._running:
            return None

        result_q: "queue.Queue[can.Message]" = queue.Queue(maxsize=8)
        pending = _PendingRequest(result_q=result_q, expect_id=expect_id, matcher=matcher)
        with self._req_lock:
            self._pending_req = pending

        msg.is_rx = False
        if msg.timestamp <= 0:
            msg.timestamp = time.time()
        self._cmd_q.put((CMD_TX, msg))

        try:
            return result_q.get(timeout=timeout)
        except queue.Empty:
            with self._req_lock:
                if self._pending_req is pending:
                    self._pending_req = None
            return None

    def receive(
        self,
        timeout: float = 1.0,
        expect_id: Optional[int] = None,
        matcher: Optional[object] = None,
    ) -> Optional[can.Message]:
        """只等待接收,不发送。**阻塞调用,只能在非界面线程里用。**

        ISO-TP 接收方向需要它:ECU 发完 BS 个连续帧后会主动送下一个流控帧,
        客户端此时不能发东西,只能等。
        """
        if not self._running:
            return None

        result_q: "queue.Queue[can.Message]" = queue.Queue(maxsize=8)
        pending = _PendingRequest(result_q=result_q, expect_id=expect_id, matcher=matcher)
        with self._req_lock:
            self._pending_req = pending

        try:
            return result_q.get(timeout=timeout)
        except queue.Empty:
            with self._req_lock:
                if self._pending_req is pending:
                    self._pending_req = None
            return None

    def cancel_pending_request(self) -> None:
        """丢弃当前等待中的请求(中止流程时调用)。"""
        with self._req_lock:
            self._pending_req = None

    def wake_pending_requests(self) -> None:
        """唤醒当前等待中的 request()/receive() 并让它返回 None。

        总线关闭时调用,否则调用方会一直阻塞到自己的超时。
        """
        with self._req_lock:
            pending = self._pending_req
            self._pending_req = None
        if pending is not None:
            try:
                pending.result_q.put_nowait(None)
            except queue.Full:
                pass

    def _send_one(self, msg: can.Message, batch: List[can.Message]) -> bool:
        """发送一条报文,成功则计入 batch 并返回 True。"""
        if self._bus is None:
            return False
        msg.is_rx = False
        if msg.timestamp <= 0:
            msg.timestamp = time.time()
        try:
            self._bus.send(msg)
        except can.CanError as exc:
            self._tx_errors += 1
            self.notify.emit(
                "error",
                f"发送失败 ID 0x{msg.arbitration_id:X}: {exc}",
            )
            return False
        except Exception as exc:  # 驱动异常不应拖垮线程
            log.exception("发送异常")
            self._tx_errors += 1
            self.notify.emit("error", f"发送异常: {exc}")
            return False

        self._tx_total += 1
        batch.append(msg)
        return True

    def _run_cyclic(self, now: float, batch: List[can.Message]) -> None:
        if not self._cyclic:
            return
        for task_id in list(self._cyclic.keys()):
            task = self._cyclic.get(task_id)
            if task is None:
                continue
            if task.cancelled:
                del self._cyclic[task_id]
                self.notify.emit("info", f"周期发送已停止 (ID 0x{task.msg.arbitration_id:X})")
                continue
            # 一口气补发错过的周期,保证平均频率
            guard = 0
            while now >= task.next_due and guard < 64:
                guard += 1
                task.sent += 1
                self._send_one(task.msg, batch)
                task.next_due += task.interval_ms / 1000.0
                if task.finished:
                    break
            if task.finished:
                del self._cyclic[task_id]
                self.notify.emit(
                    "info",
                    f"周期发送完成: ID 0x{task.msg.arbitration_id:X} 共 {task.sent} 帧",
                )
            elif task.next_due > now + 1.0:
                # 长时间没有调度机会(理论上不会发生),重置基准避免补发风暴
                task.next_due = now

    # ------------------------------------------------------------ 对外 API

    def send(self, msg: can.Message) -> None:
        """线程安全:排入一条待发送报文。"""
        self._cmd_q.put((CMD_TX, msg))

    def start_cyclic(self, interval_ms: float, msg: can.Message, repeat: int = 0) -> None:
        """线程安全:启动周期发送,repeat<=0 表示一直发到手动停止。"""
        self._cmd_q.put((CMD_ADD_CYCLIC, interval_ms, msg, repeat))

    def stop_cyclic(self, task_id: int) -> None:
        self._cmd_q.put((CMD_STOP_CYCLIC, task_id))

    def stop_all_cyclic(self) -> None:
        self._cmd_q.put((CMD_STOP_ALL_CYCLIC,))

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def rx_total(self) -> int:
        return self._rx_total

    @property
    def tx_total(self) -> int:
        return self._tx_total

    @property
    def tx_errors(self) -> int:
        return self._tx_errors

    def _make_stats_snapshot(self) -> dict:
        return {
            "rows": self._stats.snapshot(newest_first=False),
            "rx": self._stats.total_rx,
            "tx": self._stats.total_tx,
            "elapsed": self._stats.elapsed_s(),
            "tx_errors": self._tx_errors,
        }