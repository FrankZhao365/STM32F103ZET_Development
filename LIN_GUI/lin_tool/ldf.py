"""LDF(LIN Description File)解析。

支持两种语法:

* **分节语法**(LIN 1.3 / 2.0 / 2.1 / ISO 17987 常见写法)::

      [LIN_Description]
      LIN_File_Version=2.1
      Master_Node=Master
      S Baudrate=19200

      [Signal]
      MotorSpeed: 8, 0, Master, Slave1;

      [Frame]
      MotorControl: 0x11, Master, 8;

      [Schedule_Table]
      MainTable: MotorControl delay 10 ms;

* **结构化语法**(LIN 2.2 规范里的写法)::

      Nodes { Master: CEM, 5 ms, 0.1 ms; Slaves: LSM, RSM; }
      Frames { CEM_Frm1: 0x01, CEM, 1 { InternalLightsRequest, 0; } }
      Schedule_tables { Normal_Schedule { CEM_Frm1 delay 15 ms; } }

解析器刻意做得"宽容":遇到不认识的关键字会跳过而不是报错,
这样各家厂商导出的 LDF 基本都能打开。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .protocol import ChecksumType, FrameSpec

log = logging.getLogger(__name__)


class LdfError(Exception):
    """LDF 解析错误。"""


# ---------------------------------------------------------------- 数据模型


@dataclass
class LdfSignal:
    """一个信号。"""

    name: str
    size: int = 0
    """位长度。"""

    initial: int = 0
    """初始值。"""

    publisher: str = ""
    """发布节点。"""

    subscribers: List[str] = field(default_factory=list)
    """订阅节点。"""

    bit_offset: int = 0
    """在帧内的起始位偏移(bit0 = 第 0 字节的最低位)。"""

    factor: float = 1.0
    """物理值换算系数。"""

    scale: float = 0.0
    """物理值换算偏移。"""

    unit: str = ""
    """物理单位。"""

    min_value: Optional[float] = None
    """物理值下限。"""

    max_value: Optional[float] = None
    """物理值上限。"""

    encoding_name: str = ""
    """关联的编码表名。"""

    logical_values: Dict[int, str] = field(default_factory=dict)
    """逻辑值 -> 文字说明。"""

    @property
    def byte_length(self) -> int:
        """占用的字节数(向上取整)。"""
        return (self.size + 7) // 8

    def physical(self, raw: int) -> float:
        """原始值换算成物理值。"""
        return raw * self.factor + self.scale

    def text(self, raw: int) -> str:
        """原始值对应的文字说明(没有则返回空串)。"""
        return self.logical_values.get(int(raw), "")


@dataclass
class LdfFrame:
    """一个帧。"""

    name: str
    frame_id: int
    size: int = 0
    """数据字节数。"""

    publisher: str = ""
    subscribers: List[str] = field(default_factory=list)
    signals: List[LdfSignal] = field(default_factory=list)
    checksum_type: ChecksumType = ChecksumType.ENHANCED
    event_triggered: bool = False
    associated_frames: List[str] = field(default_factory=list)
    """事件触发帧关联的无条件帧。"""


@dataclass
class LdfScheduleEntry:
    """调度表里的一个槽位。"""

    frame_name: str
    delay_ms: float = 10.0


@dataclass
class LdfScheduleTable:
    """一张调度表。"""

    name: str
    entries: List[LdfScheduleEntry] = field(default_factory=list)


@dataclass
class LdfDatabase:
    """整个 LDF 的解析结果。"""

    version: str = ""
    protocol_version: str = ""
    baudrate: int = 19200
    master: str = ""
    slaves: List[str] = field(default_factory=list)
    nodes: Dict[str, Dict[str, str]] = field(default_factory=dict)
    signals: Dict[str, LdfSignal] = field(default_factory=dict)
    frames: Dict[str, LdfFrame] = field(default_factory=dict)
    schedules: Dict[str, LdfScheduleTable] = field(default_factory=dict)
    source_path: str = ""

    # ------------------------------------------------------------ 查询

    def frame_by_id(self, frame_id: int) -> Optional[LdfFrame]:
        """按 ID 查帧。"""
        fid = int(frame_id) & 0x3F
        for frame in self.frames.values():
            if frame.frame_id == fid:
                return frame
        return None

    def frame_by_name(self, name: str) -> Optional[LdfFrame]:
        """按名字查帧。"""
        return self.frames.get(name)

    def schedule_specs(self, table_name: str) -> List[FrameSpec]:
        """把一张调度表展开成轮询用的帧列表。"""
        table = self.schedules.get(table_name)
        if table is None:
            return []
        specs: List[FrameSpec] = []
        for entry in table.entries:
            frame = self.frames.get(entry.frame_name)
            if frame is None:
                continue
            specs.append(
                FrameSpec(
                    frame_id=frame.frame_id,
                    length=frame.size,
                    name=frame.name,
                    checksum_type=frame.checksum_type,
                    delay_ms=entry.delay_ms,
                )
            )
        return specs

    def all_specs(self) -> List[FrameSpec]:
        """所有无条件帧。"""
        specs: List[FrameSpec] = []
        for frame in self.frames.values():
            if frame.event_triggered:
                continue
            specs.append(
                FrameSpec(
                    frame_id=frame.frame_id,
                    length=frame.size,
                    name=frame.name,
                    checksum_type=frame.checksum_type,
                )
            )
        specs.sort(key=lambda s: s.frame_id)
        return specs

    def summary(self) -> str:
        """一句话摘要,给界面状态栏用。"""
        return (
            f"版本 {self.version or '未知'} | 主节点 {self.master or '未知'} | "
            f"从节点 {len(self.slaves)} | 帧 {len(self.frames)} | "
            f"调度表 {len(self.schedules)} | {self.baudrate} bit/s"
        )


# ---------------------------------------------------------------- 工具函数


def _strip_comments(text: str) -> str:
    """去掉 // 行注释与 /* */ 块注释。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _to_int(value: str, default: int = 0) -> int:
    """解析整数,支持 0x 前缀。"""
    value = (value or "").strip().rstrip(";").strip()
    if not value:
        return default
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return default


def _to_float(value: str, default: float = 0.0) -> float:
    value = (value or "").strip().rstrip(";").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_list(text: str) -> List[str]:
    """按逗号切分,去掉空项。"""
    return [item.strip() for item in text.split(",") if item.strip()]


def _extract_blocks(text: str, keyword: str) -> List[Tuple[str, str]]:
    """取出 `关键字 { ... }` 形式的块,返回 [(块名, 块内容)]。"""
    blocks: List[Tuple[str, str]] = []
    pattern = re.compile(re.escape(keyword) + r"\s*\{")
    for match in pattern.finditer(text):
        start = match.end()
        depth = 1
        idx = start
        while idx < len(text) and depth > 0:
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            idx += 1
        body = text[start : idx - 1]
        blocks.append((keyword, body))
    return blocks


# ---------------------------------------------------------------- 解析入口


def parse_ldf(text: str, source_path: str = "") -> LdfDatabase:
    """解析 LDF 文本。"""
    if not text or not text.strip():
        raise LdfError("LDF 文件为空")

    cleaned = _strip_comments(text)
    if "[LIN_Description]" in cleaned or "[Node_Attribute]" in cleaned:
        db = _parse_section_style(cleaned)
    elif re.search(r"\bNodes\s*\{", cleaned) or re.search(r"\bFrames\s*\{", cleaned):
        db = _parse_structured_style(cleaned)
    else:
        # 兜底:两种都试一遍
        try:
            db = _parse_section_style(cleaned)
        except LdfError:
            db = _parse_structured_style(cleaned)

    db.source_path = source_path
    _finalize(db)
    return db


def load_ldf(path: str) -> LdfDatabase:
    """从文件读取并解析 LDF。"""
    if not os.path.isfile(path):
        raise LdfError(f"找不到 LDF 文件: {path}")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise LdfError(f"无法读取 LDF 文件 {path}: {exc}") from exc
    return parse_ldf(text, source_path=os.path.abspath(path))


def _finalize(db: LdfDatabase) -> None:
    """补齐派生信息:信号挂到帧上、编码表关联、校验和类型。"""
    # 校验和类型:LIN 1.3 用经典,2.0+ 用增强;诊断帧(0x3C/0x3D)用经典。
    for frame in db.frames.values():
        if frame.frame_id in (0x3C, 0x3D):
            frame.checksum_type = ChecksumType.CLASSIC
        elif db.protocol_version.startswith("1."):
            frame.checksum_type = ChecksumType.CLASSIC
        else:
            frame.checksum_type = ChecksumType.ENHANCED

    # 把帧里的信号定义合并到全局信号表
    for frame in db.frames.values():
        for sig in frame.signals:
            if sig.name in db.signals:
                existing = db.signals[sig.name]
                # 保留 LDF 里更完整的信息(size/编码表)
                if not existing.size:
                    existing.size = sig.size
                if not existing.publisher:
                    existing.publisher = sig.publisher
                if not existing.subscribers:
                    existing.subscribers = list(sig.subscribers)
                if sig.encoding_name and not existing.encoding_name:
                    existing.encoding_name = sig.encoding_name
                if sig.logical_values and not existing.logical_values:
                    existing.logical_values = dict(sig.logical_values)
            else:
                db.signals[sig.name] = sig

    # 主节点名兜底
    if not db.master:
        for name, attrs in db.nodes.items():
            if attrs.get("is_master", "").lower() in ("1", "true", "yes"):
                db.master = name
                break
        if not db.master and db.nodes:
            db.master = next(iter(db.nodes))

    # 从节点列表兜底:分节语法里往往只写 [Node_Attribute],没有单独的 Slaves 列表
    if not db.slaves:
        db.slaves = [
            name
            for name in db.nodes
            if name != db.master and not name.startswith("__")
        ]


# ---------------------------------------------------------------- 分节语法


def _parse_section_style(text: str) -> LdfDatabase:
    db = LdfDatabase()

    sections: Dict[str, List[str]] = {}
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().upper()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)

    # --- [LIN_Description] ---
    for line in sections.get("LIN_DESCRIPTION", []):
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key == "LIN_FILE_VERSION":
            db.version = value
        elif key == "LIN_PROTOCOL_VERSION":
            db.protocol_version = value
        elif key == "MASTER_NODE":
            db.master = value
        elif key.endswith("BAUDRATE"):
            db.baudrate = _to_int(value, db.baudrate)
        elif key == "LIN_NODE_CONFIGURATION_FILE":
            pass

    # --- [Node_Attribute] ---
    for stmt in _split_statements(" ".join(sections.get("NODE_ATTRIBUTE", []))):
        match = re.match(r"^\s*([A-Za-z_][\w]*)\s*:\s*(.*)$", stmt)
        if not match:
            continue
        node, body = match.group(1), match.group(2)
        attrs: Dict[str, str] = {}
        for item in _split_list(body):
            if "=" in item:
                key, _, value = item.partition("=")
                attrs[key.strip().lower()] = value.strip()
            else:
                attrs[item.strip().lower()] = ""
        db.nodes[node] = attrs

    # --- [Signal] ---
    for stmt in _split_statements(" ".join(sections.get("SIGNAL", []))):
        match = re.match(r"^\s*([A-Za-z_][\w]*)\s*:\s*(.*)$", stmt)
        if not match:
            continue
        name, body = match.group(1), match.group(2)
        parts = _split_list(body)
        if len(parts) < 2:
            continue
        sig = LdfSignal(
            name=name,
            size=_to_int(parts[0]),
            initial=_to_int(parts[1]),
            publisher=parts[2] if len(parts) > 2 else "",
            subscribers=parts[3:] if len(parts) > 3 else [],
        )
        db.signals[sig.name] = sig

    # --- [Frame] ---
    for stmt in _split_statements(" ".join(sections.get("FRAME", []))):
        match = re.match(r"^\s*([A-Za-z_][\w]*)\s*:\s*(.*)$", stmt)
        if not match:
            continue
        name, body = match.group(1), match.group(2)
        parts = _split_list(body)
        if not parts:
            continue
        frame = LdfFrame(
            name=name,
            frame_id=_to_int(parts[0]) & 0x3F,
            publisher=parts[1] if len(parts) > 1 else "",
            size=_to_int(parts[2]) if len(parts) > 2 else 0,
        )
        db.frames[name] = frame

    # --- [Frame_Definition] ---
    body = " ".join(sections.get("FRAME_DEFINITION", []))
    for match in re.finditer(
        r"([A-Za-z_][\w]*)\s*:\s*([^;{}]*)\{([^}]*)\}", body, flags=re.S
    ):
        name = match.group(1)
        header = _split_list(match.group(2))
        inner = match.group(3)
        frame = db.frames.get(name)
        if frame is None:
            frame = LdfFrame(name=name, frame_id=0x3E)
            db.frames[name] = frame
        # header: 数据长度, 发布者, 订阅者..., 信号个数
        if header:
            frame.size = _to_int(header[0], frame.size)
        if len(header) > 1:
            frame.publisher = header[1]
        subs = header[2:]
        if subs and subs[-1].isdigit():
            subs = subs[:-1]
        if subs:
            frame.subscribers = subs
        for sig_stmt in _split_statements(inner):
            sig_match = re.match(r"^\s*([A-Za-z_][\w]*)\s*,\s*(\d+)\s*$", sig_stmt)
            if not sig_match:
                continue
            sig_name, offset = sig_match.group(1), int(sig_match.group(2))
            sig = db.signals.get(sig_name) or LdfSignal(name=sig_name)
            sig.bit_offset = offset
            sig.size = sig.size or 0
            frame.signals.append(sig)

    # --- [Schedule_Table] ---
    # 表名与第一个槽位写在同一条语句里("MainTable: Frame1 delay 10 ms;"),
    # 所以先按 "名字:" 切块,再在块里找 delay 条目。
    body = " ".join(sections.get("SCHEDULE_TABLE", []))
    heads = [
        (match.group(1), match.end())
        for match in re.finditer(r"([A-Za-z_][\w]*)\s*:", body)
    ]
    for index, (name, start) in enumerate(heads):
        end = heads[index + 1][1] if index + 1 < len(heads) else len(body)
        chunk = body[start:end]
        table = LdfScheduleTable(name=name)
        for entry in re.finditer(
            r"([A-Za-z_][\w]*)\s+delay\s+([\d.]+)\s*ms", chunk
        ):
            table.entries.append(
                LdfScheduleEntry(entry.group(1), float(entry.group(2)))
            )
        db.schedules[name] = table

    # --- [Signal_Encoding] / [Signal_Representation] ---
    encodings = _parse_encoding_blocks(" ".join(sections.get("SIGNAL_ENCODING", [])))
    for stmt in _split_statements(" ".join(sections.get("SIGNAL_REPRESENTATION", []))):
        match = re.match(r"^\s*([A-Za-z_][\w]*)\s*:\s*(.*)$", stmt)
        if not match:
            continue
        enc_name, sig_list = match.group(1), match.group(2)
        for sig_name in _split_list(sig_list):
            sig = db.signals.setdefault(sig_name, LdfSignal(name=sig_name))
            sig.encoding_name = enc_name
            _apply_encoding(sig, encodings.get(enc_name, {}))

    # --- 事件触发帧 ---
    body = " ".join(sections.get("EVENT_TRIGGERED_FRAMES", []))
    for stmt in _split_statements(body):
        match = re.match(
            r"^\s*([A-Za-z_][\w]*)\s*:\s*([A-Za-z_][\w]*)\s*,\s*(0x[0-9A-Fa-f]+|\d+)\s*,(.*)$",
            stmt,
        )
        if not match:
            continue
        name = match.group(1)
        frame = db.frames.get(name) or LdfFrame(name=name, frame_id=_to_int(match.group(3)))
        frame.frame_id = _to_int(match.group(3)) & 0x3F
        frame.event_triggered = True
        frame.associated_frames = _split_list(match.group(4))
        db.frames[name] = frame

    return db


def _split_statements(text: str) -> List[str]:
    """按 `;` 拆分语句,忽略花括号内的分号。"""
    statements: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == ";" and depth <= 0:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _parse_encoding_blocks(text: str) -> Dict[str, Dict[str, object]]:
    """解析 `名称 { logical_value, 0, "Off"; ... }` 形式的编码表。"""
    encodings: Dict[str, Dict[str, object]] = {}
    for match in re.finditer(r"([A-Za-z_][\w]*)\s*\{([^}]*)\}", text, flags=re.S):
        name = match.group(1)
        info: Dict[str, object] = {"logical": {}, "physical": None}
        for stmt in _split_statements(match.group(2)):
            parts = _split_list(stmt)
            if not parts:
                continue
            kind = parts[0].lower()
            if kind == "logical_value" and len(parts) >= 3:
                info["logical"][_to_int(parts[1])] = _unquote(parts[2])
            elif kind == "physical_value" and len(parts) >= 6:
                info["physical"] = {
                    "min": _to_float(parts[1]),
                    "max": _to_float(parts[2]),
                    "factor": _to_float(parts[3], 1.0),
                    "offset": _to_float(parts[4]),
                    "unit": _unquote(parts[5]),
                }
        encodings[name] = info
    return encodings


def _apply_encoding(sig: LdfSignal, info: Dict[str, object]) -> None:
    """把编码表里的逻辑值/物理换算写进信号。"""
    logical = info.get("logical") or {}
    if isinstance(logical, dict):
        sig.logical_values = {int(k): str(v) for k, v in logical.items()}
    physical = info.get("physical")
    if isinstance(physical, dict):
        sig.factor = float(physical.get("factor", 1.0))
        sig.scale = float(physical.get("offset", 0.0))
        sig.unit = str(physical.get("unit", ""))
        sig.min_value = float(physical.get("min", 0.0))
        sig.max_value = float(physical.get("max", 0.0))


# ---------------------------------------------------------------- 结构化语法


def _parse_structured_style(text: str) -> LdfDatabase:
    db = LdfDatabase()

    # 顶层属性
    for key in ("LIN_protocol_version", "LIN_file_version"):
        match = re.search(key + r'\s*=\s*"([^"]*)"', text, flags=re.I)
        if match:
            if key.lower() == "lin_protocol_version":
                db.protocol_version = match.group(1)
            else:
                db.version = match.group(1)
    match = re.search(r"LIN_speed\s*=\s*([\d.]+)\s*kbps", text, flags=re.I)
    if match:
        db.baudrate = int(float(match.group(1)) * 1000)
    match = re.search(r"Channel_name\s*=\s*\"([^\"]*)\"", text)
    if match:
        db.nodes.setdefault("__channel__", {})["channel_name"] = match.group(1)

    # --- Nodes ---
    for _, body in _extract_blocks(text, "Nodes"):
        for stmt in _split_statements(body):
            master = re.match(r"^\s*Master\s*:\s*(.*)$", stmt, flags=re.I)
            if master:
                items = _split_list(master.group(1))
                if items:
                    db.master = items[0]
                    attrs = db.nodes.setdefault(items[0], {})
                    attrs["is_master"] = "1"
                    continue
            slaves = re.match(r"^\s*Slaves\s*:\s*(.*)$", stmt, flags=re.I)
            if slaves:
                for name in _split_list(slaves.group(1)):
                    db.slaves.append(name)
                    db.nodes.setdefault(name, {})

    # --- Signals ---
    for _, body in _extract_blocks(text, "Signals"):
        for stmt in _split_statements(body):
            match = re.match(
                r"^\s*([A-Za-z_][\w]*)\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*([\w, ]+?)\s*$",
                stmt,
            )
            if not match:
                continue
            parts = _split_list(match.group(4))
            sig = LdfSignal(
                name=match.group(1),
                size=int(match.group(2)),
                initial=int(match.group(3)),
                publisher=parts[0] if parts else "",
                subscribers=parts[1:],
            )
            db.signals[sig.name] = sig

    # --- Frames ---
    for _, body in _extract_blocks(text, "Frames"):
        for match in re.finditer(
            r"([A-Za-z_][\w]*)\s*:\s*(0x[0-9A-Fa-f]+|\d+)\s*,\s*([A-Za-z_][\w]*)\s*,\s*(\d+)\s*\{([^}]*)\}",
            body,
            flags=re.S,
        ):
            name = match.group(1)
            frame = LdfFrame(
                name=name,
                frame_id=_to_int(match.group(2)) & 0x3F,
                publisher=match.group(3),
                size=int(match.group(4)),
            )
            for sig_stmt in _split_statements(match.group(5)):
                sig_match = re.match(
                    r"^\s*([A-Za-z_][\w]*)\s*,\s*(\d+)\s*$", sig_stmt
                )
                if not sig_match:
                    continue
                sig_name, offset = sig_match.group(1), int(sig_match.group(2))
                sig = db.signals.get(sig_name) or LdfSignal(name=sig_name)
                sig.bit_offset = offset
                frame.signals.append(sig)
            db.frames[name] = frame

    # --- Event triggered frames ---
    for _, body in _extract_blocks(text, "Event_triggered_frames"):
        for stmt in _split_statements(body):
            match = re.match(
                r"^\s*([A-Za-z_][\w]*)\s*:\s*([A-Za-z_][\w]*)\s*,\s*(0x[0-9A-Fa-f]+|\d+)\s*,\s*(.*)$",
                stmt,
            )
            if not match:
                continue
            name = match.group(1)
            frame = LdfFrame(name=name, frame_id=_to_int(match.group(3)) & 0x3F)
            frame.event_triggered = True
            frame.associated_frames = _split_list(match.group(4))
            db.frames[name] = frame

    # --- Node attributes ---
    for _, body in _extract_blocks(text, "Node_attributes"):
        for match in re.finditer(
            r"([A-Za-z_][\w]*)\s*\{([^}]*)\}", body, flags=re.S
        ):
            attrs: Dict[str, str] = {}
            for stmt in _split_statements(match.group(2)):
                if "=" in stmt:
                    key, _, value = stmt.partition("=")
                    attrs[key.strip().lower()] = value.strip().rstrip(";").strip()
            db.nodes[match.group(1)] = attrs

    # --- Schedule tables ---
    for _, body in _extract_blocks(text, "Schedule_tables"):
        for match in re.finditer(
            r"([A-Za-z_][\w]*)\s*\{([^}]*)\}", body, flags=re.S
        ):
            table = LdfScheduleTable(name=match.group(1))
            for stmt in _split_statements(match.group(2)):
                entry = re.match(
                    r"^\s*([A-Za-z_][\w]*)\s+delay\s+([\d.]+)\s*ms\s*$", stmt
                )
                if entry:
                    table.entries.append(
                        LdfScheduleEntry(entry.group(1), float(entry.group(2)))
                    )
            db.schedules[table.name] = table

    # --- Signal encoding types ---
    encodings: Dict[str, Dict[str, object]] = {}
    for _, body in _extract_blocks(text, "Signal_encoding_types"):
        for match in re.finditer(
            r"([A-Za-z_][\w]*)\s*\{([^}]*)\}", body, flags=re.S
        ):
            name = match.group(1)
            info: Dict[str, object] = {"logical": {}, "physical": None}
            for stmt in _split_statements(match.group(2)):
                parts = _split_list(stmt)
                if not parts:
                    continue
                kind = parts[0].lower()
                if kind == "logical_value" and len(parts) >= 3:
                    info["logical"][_to_int(parts[1])] = _unquote(parts[2])
                elif kind == "physical_value" and len(parts) >= 6:
                    info["physical"] = {
                        "min": _to_float(parts[1]),
                        "max": _to_float(parts[2]),
                        "factor": _to_float(parts[3], 1.0),
                        "offset": _to_float(parts[4]),
                        "unit": _unquote(parts[5]),
                    }
            encodings[name] = info

    # --- Signal representation ---
    for _, body in _extract_blocks(text, "Signal_representation"):
        for stmt in _split_statements(body):
            match = re.match(r"^\s*([A-Za-z_][\w]*)\s*:\s*(.*)$", stmt)
            if not match:
                continue
            enc_name = match.group(1)
            for sig_name in _split_list(match.group(2)):
                sig = db.signals.setdefault(sig_name, LdfSignal(name=sig_name))
                sig.encoding_name = enc_name
                _apply_encoding(sig, encodings.get(enc_name, {}))

    return db
