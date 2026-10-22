"""Qt 表格模型。

报文表格用 QAbstractTableModel + 批量插入:
LIN 总线帧率不高,但一次批量提交比逐行 repaint 依然快得多,
也能避免界面在高频轮询时卡顿。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt5.QtGui import QBrush, QColor

from .protocol import LinFrame, format_data

FRAME_COLUMNS = ["序号", "时间", "方向", "ID", "名称", "DLC", "数据", "校验", "备注"]
COL_SEQ, COL_TIME, COL_DIR, COL_ID, COL_NAME, COL_DLC, COL_DATA, COL_CKSUM, COL_NOTE = range(
    len(FRAME_COLUMNS)
)

DEFAULT_MAX_ROWS = 5000

_RX_BRUSH = QBrush(QColor("#2e7d32"))
_TX_BRUSH = QBrush(QColor("#1565c0"))
_ERR_BRUSH = QBrush(QColor("#d32f2f"))


class LinFrameTableModel(QAbstractTableModel):
    """按时间顺序保存最近 N 帧报文。"""

    def __init__(self, max_rows: int = DEFAULT_MAX_ROWS, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[LinFrame] = []
        self._max_rows = max(int(max_rows), 100)
        self._rx_total = 0
        self._tx_total = 0
        self._error_total = 0

    # ---------------------------------------------------------- Qt 接口

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(FRAME_COLUMNS)

    def headerData(self, section: int, orientation: int, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return FRAME_COLUMNS[section]
        if orientation == Qt.Vertical:
            return str(section + 1)
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        frame = self._rows[index.row()]

        if role == Qt.DisplayRole:
            return self._display(index.column(), frame)
        if role == Qt.TextAlignmentRole:
            if index.column() in (COL_SEQ, COL_DIR, COL_ID, COL_DLC, COL_CKSUM):
                return int(Qt.AlignCenter)
            if index.column() == COL_TIME:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if frame.is_error:
                return _ERR_BRUSH
            if index.column() == COL_DIR:
                return _RX_BRUSH if frame.direction == "RX" else _TX_BRUSH
        if role == Qt.UserRole:
            return frame
        return None

    # ---------------------------------------------------------- 内部工具

    @staticmethod
    def _display(col: int, frame: LinFrame):
        if col == COL_SEQ:
            return ""  # 竖直表头已经给了行号
        if col == COL_TIME:
            return datetime.fromtimestamp(frame.timestamp).strftime("%H:%M:%S.%f")[:-3]
        if col == COL_DIR:
            return frame.direction
        if col == COL_ID:
            return f"0x{frame.frame_id:02X}"
        if col == COL_NAME:
            return frame.display_name
        if col == COL_DLC:
            return str(frame.dlc)
        if col == COL_DATA:
            return format_data(frame.data)
        if col == COL_CKSUM:
            if frame.direction == "TX":
                return "--"
            return "OK" if frame.checksum_valid else f"0x{frame.checksum:02X}"
        if col == COL_NOTE:
            return frame.note
        return None

    # ---------------------------------------------------------- 数据写入

    def append_batch(self, frames: Sequence[LinFrame], count_totals: bool = True) -> None:
        """追加一批报文(整批只触发一次 layout 变化)。

        暂停显示时可以先用 `record_frames()` 更新计数，恢复后再追加缓存帧，
        此时传 `count_totals=False` 避免重复累计。
        """
        count = len(frames)
        if not count:
            return

        overflow = max(0, len(self._rows) + count - self._max_rows)
        if overflow:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self._rows[:overflow]
            self.endRemoveRows()

        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + count - 1)
        self._rows.extend(frames)
        self.endInsertRows()

        if count_totals:
            self.record_frames(frames)

    def record_frames(self, frames: Sequence[LinFrame]) -> None:
        """累计报文计数,但不把报文插入表格。"""
        for frame in frames:
            if frame.direction == "TX":
                self._tx_total += 1
            else:
                self._rx_total += 1
            if frame.is_error:
                self._error_total += 1

    def clear(self) -> None:
        if not self._rows:
            return
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()

    def reset_counters(self) -> None:
        """清空累计计数(不影响已显示的行)。"""
        self._rx_total = 0
        self._tx_total = 0
        self._error_total = 0

    def frame_at(self, row: int) -> Optional[LinFrame]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def all_frames(self) -> List[LinFrame]:
        return list(self._rows)

    @property
    def rx_total(self) -> int:
        return self._rx_total

    @property
    def tx_total(self) -> int:
        return self._tx_total

    @property
    def error_total(self) -> int:
        return self._error_total

    @property
    def max_rows(self) -> int:
        return self._max_rows

    def set_max_rows(self, value: int) -> None:
        self._max_rows = max(int(value), 100)
        if len(self._rows) > self._max_rows:
            excess = len(self._rows) - self._max_rows
            self.beginRemoveRows(QModelIndex(), 0, excess - 1)
            del self._rows[:excess]
            self.endRemoveRows()


class LinFrameFilterProxy(QSortFilterProxyModel):
    """按关键字 / 方向 / 仅错误过滤。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._keyword = ""
        self._direction = ""
        self._errors_only = False
        self.setDynamicSortFilter(True)

    def set_keyword(self, text: str) -> None:
        self._keyword = (text or "").strip().upper()
        self.invalidateFilter()

    def set_direction(self, direction: str) -> None:
        self._direction = direction or ""
        self.invalidateFilter()

    def set_errors_only(self, enabled: bool) -> None:
        self._errors_only = bool(enabled)
        self.invalidateFilter()

    @property
    def keyword(self) -> str:
        return self._keyword

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, LinFrameTableModel):
            return True
        frame = model.frame_at(source_row)
        if frame is None:
            return False

        if self._direction and frame.direction != self._direction:
            return False
        if self._errors_only and not frame.is_error:
            return False

        if self._keyword:
            kw = self._keyword
            id_hex = f"{frame.frame_id:02X}"
            data_hex = format_data(frame.data)
            if (
                kw not in id_hex
                and kw not in data_hex
                and kw not in (frame.name or "").upper()
                and kw not in (frame.note or "").upper()
            ):
                return False
        return True


class DecodedSignalModel(QAbstractTableModel):
    """一帧里各信号的解码结果。"""

    COLUMNS = ["信号", "原始值", "物理值", "说明", "位偏移", "位长"]
    COL_NAME, COL_RAW, COL_PHYS, COL_TEXT, COL_OFFSET, COL_SIZE = range(len(COLUMNS))

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List = []
        self._frame_name = ""

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: int, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return str(section + 1)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        sig = self._rows[index.row()]

        if role == Qt.DisplayRole:
            col = index.column()
            if col == self.COL_NAME:
                return sig.name
            if col == self.COL_RAW:
                return f"0x{sig.raw:X} ({sig.raw})"
            if col == self.COL_PHYS:
                return sig.value_text
            if col == self.COL_TEXT:
                return sig.text
            if col == self.COL_OFFSET:
                return str(sig.bit_offset)
            if col == self.COL_SIZE:
                return str(sig.size)
        if role == Qt.TextAlignmentRole:
            if index.column() in (self.COL_OFFSET, self.COL_SIZE):
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.UserRole:
            return sig
        return None

    def set_signals(self, frame_name: str, signals: Sequence) -> None:
        """整体替换信号列表。"""
        self.beginResetModel()
        self._frame_name = frame_name
        self._rows = list(signals)
        self.endResetModel()

    def clear(self) -> None:
        self.set_signals("", [])

    @property
    def frame_name(self) -> str:
        return self._frame_name


class StatsModel(QAbstractTableModel):
    """按帧 ID 统计的表格。"""

    COLUMNS = ["ID", "名称", "方向", "次数", "周期(ms)", "错误数", "最近数据"]
    COL_ID, COL_NAME, COL_DIR, COL_COUNT, COL_PERIOD, COL_ERROR, COL_DATA = range(len(COLUMNS))

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[dict] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: int, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return str(section + 1)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.DisplayRole:
            col = index.column()
            if col == self.COL_ID:
                return f"0x{row['frame_id']:02X}"
            if col == self.COL_NAME:
                return row.get("name", "")
            if col == self.COL_DIR:
                return row.get("direction", "")
            if col == self.COL_COUNT:
                return str(row.get("count", 0))
            if col == self.COL_PERIOD:
                period = row.get("period_ms", 0.0)
                return f"{period:.1f}" if period else "--"
            if col == self.COL_ERROR:
                return str(row.get("errors", 0))
            if col == self.COL_DATA:
                return row.get("data", "")
        if role == Qt.TextAlignmentRole:
            if index.column() in (self.COL_ID, self.COL_DIR, self.COL_COUNT, self.COL_PERIOD, self.COL_ERROR):
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.UserRole:
            return row
        return None

    def set_rows(self, rows: Sequence[dict]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()
