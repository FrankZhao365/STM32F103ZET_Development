"""报文表格数据模型。

用 QAbstractTableModel + 批量插入,而不是每条报文一个单元格变更:
高速总线上每帧都触发一次 repaint 会直接卡死界面。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

import can

COLUMNS = ["序号", "时间", "方向", "ID", "类型", "DLC", "数据"]
COL_SEQ, COL_TIME, COL_DIR, COL_ID, COL_TYPE, COL_DLC, COL_DATA = range(len(COLUMNS))

DEFAULT_MAX_ROWS = 5000


class MessageTableModel(QAbstractTableModel):
    """按时间顺序保存最近 N 条报文。"""

    def __init__(self, max_rows: int = DEFAULT_MAX_ROWS, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[can.Message] = []
        self._max_rows = max(max_rows, 100)
        self._seq = 0
        self._rx_total = 0
        self._tx_total = 0

    # ---------------------------------------------------------- Qt 接口

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: int, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            return str(section + 1)
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        msg = self._rows[index.row()]

        if role == Qt.DisplayRole:
            return self._display(index.column(), msg)
        if role == Qt.TextAlignmentRole:
            if index.column() in (COL_SEQ, COL_DIR, COL_TYPE, COL_DLC):
                return int(Qt.AlignCenter)
            if index.column() == COL_TIME:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if index.column() == COL_DIR:
                return self._dir_brush(msg)
            if index.column() == COL_DATA:
                # 错误帧标红,方便一眼看到总线异常
                if msg.is_error_frame:
                    from PyQt5.QtGui import QColor

                    return QColor("#d32f2f")
        if role == Qt.UserRole:
            # 给过滤代理和"复制"用,返回原始 Message
            return msg
        return None

    # ---------------------------------------------------------- 内部工具

    @staticmethod
    def _dir_brush(msg: can.Message):
        from PyQt5.QtGui import QBrush, QColor

        if msg.is_error_frame:
            return QBrush(QColor("#d32f2f"))
        return QBrush(QColor("#2e7d32") if msg.is_rx else QColor("#1565c0"))

    @staticmethod
    def _display(col: int, msg: can.Message):
        if col == COL_SEQ:
            return ""  # 由 Qt 的 vertical header 提供,避免每行多存一个整数
        if col == COL_TIME:
            return datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S.%f")[:-3]
        if col == COL_DIR:
            return "RX" if msg.is_rx else "TX"
        if col == COL_ID:
            return f"0x{msg.arbitration_id:X}"
        if col == COL_TYPE:
            if msg.is_error_frame:
                return "ERR"
            if msg.is_extended_id:
                return "EXT"
            return "STD"
        if col == COL_DLC:
            return str(len(msg.data))
        if col == COL_DATA:
            return " ".join(f"{b:02X}" for b in msg.data)
        return None

    # ---------------------------------------------------------- 数据写入

    def append_batch(self, messages: Sequence[can.Message]) -> None:
        """追加一批报文(整批只触发一次 layoutChanged)。"""
        n = len(messages)
        if not n:
            return

        overflow = max(0, len(self._rows) + n - self._max_rows)
        if overflow:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self._rows[:overflow]
            self.endRemoveRows()

        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + n - 1)
        self._rows.extend(messages)
        self.endInsertRows()

        for msg in messages:
            if msg.is_rx:
                self._rx_total += 1
            else:
                self._tx_total += 1

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

    def message_at(self, row: int) -> Optional[can.Message]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def all_messages(self) -> List[can.Message]:
        return list(self._rows)

    @property
    def rx_total(self) -> int:
        return self._rx_total

    @property
    def tx_total(self) -> int:
        return self._tx_total

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


class MessageFilterProxy(QSortFilterProxyModel):
    """按 ID / 数据关键字 / 方向过滤。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._keyword = ""
        self._direction = ""  # "" 全部 / "RX" / "TX"
        self.setDynamicSortFilter(True)

    def set_keyword(self, text: str) -> None:
        self._keyword = (text or "").strip().upper()
        self.invalidateFilter()

    def set_direction(self, direction: str) -> None:
        self._direction = direction or ""
        self.invalidateFilter()

    @property
    def keyword(self) -> str:
        return self._keyword

    @property
    def direction(self) -> str:
        return self._direction

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, MessageTableModel):
            return True
        msg = model.message_at(source_row)
        if msg is None:
            return False

        if self._direction:
            want_rx = self._direction == "RX"
            if msg.is_rx != want_rx:
                return False

        if self._keyword:
            kw = self._keyword
            id_hex = f"{msg.arbitration_id:X}"
            if kw not in id_hex and kw not in (" ".join(f"{b:02X}" for b in msg.data)):
                return False
        return True