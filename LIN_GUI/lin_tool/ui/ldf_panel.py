"""LDF 与轮询设置面板。

上半部分显示 LDF 里的调度表与帧;下半部分是"轮询帧列表",
决定了主节点模式下去哪些帧、每个帧的槽位时间(周期)。
没有 LDF 时也可以直接手工填这个表。
"""

from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ldf import LdfDatabase
from ..protocol import ChecksumType, FrameSpec

COL_CHECK, COL_ID, COL_NAME, COL_LEN, COL_PERIOD, COL_CKSUM = range(6)
FRAME_COL_CHECK, FRAME_COL_ID, FRAME_COL_NAME, FRAME_COL_PUB, FRAME_COL_LEN, FRAME_COL_SIG = range(6)


class LdfPanel(QWidget):
    """LDF 信息 + 轮询帧列表。"""

    specsChanged = pyqtSignal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ldf: Optional[LdfDatabase] = None
        self._build()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # --- 调度表 ---
        table_box = QGroupBox("调度表")
        table_form = QVBoxLayout(table_box)

        row = QHBoxLayout()
        self.schedule_combo = QComboBox()
        row.addWidget(self.schedule_combo, 1)
        fill_btn = QPushButton("填入轮询列表")
        fill_btn.clicked.connect(self.fill_from_schedule)
        row.addWidget(fill_btn)
        table_form.addLayout(row)

        self.summary = QLabel("未加载 LDF")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#555;")
        table_form.addWidget(self.summary)

        self.frame_table = QTableWidget(0, FRAME_COL_SIG + 1)
        self.frame_table.setHorizontalHeaderLabels(
            ["ID", "名称", "发布者", "长度", "信号数"]
        )
        self.frame_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.frame_table.verticalHeader().setVisible(False)
        self.frame_table.horizontalHeader().setSectionResizeMode(FRAME_COL_NAME, QHeaderView.Stretch)
        table_form.addWidget(self.frame_table)
        layout.addWidget(table_box, 1)

        # --- 轮询帧列表 ---
        poll_box = QGroupBox("轮询帧列表(主节点模式按此顺序轮询)")
        poll_form = QVBoxLayout(poll_box)

        self.poll_table = QTableWidget(0, COL_CKSUM + 1)
        self.poll_table.setHorizontalHeaderLabels(
            ["轮询", "ID", "名称", "长度", "周期(ms)", "校验和"]
        )
        self.poll_table.verticalHeader().setVisible(False)
        self.poll_table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self.poll_table.itemChanged.connect(self._on_item_changed)
        poll_form.addWidget(self.poll_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(lambda: self.add_spec(FrameSpec(0x00, 1)))
        btn_row.addWidget(add_btn)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self.remove_selected)
        btn_row.addWidget(del_btn)
        all_btn = QPushButton("全选")
        all_btn.clicked.connect(lambda: self.set_all_checked(True))
        btn_row.addWidget(all_btn)
        none_btn = QPushButton("全不选")
        none_btn.clicked.connect(lambda: self.set_all_checked(False))
        btn_row.addWidget(none_btn)
        btn_row.addStretch(1)
        poll_form.addLayout(btn_row)

        layout.addWidget(poll_box, 1)

    # ------------------------------------------------------------ LDF

    def set_ldf(self, ldb: Optional[LdfDatabase]) -> None:
        """载入/清除 LDF。"""
        self._ldf = ldb
        self.schedule_combo.clear()
        self.frame_table.setRowCount(0)
        if ldb is None:
            self.summary.setText("未加载 LDF")
            return

        self.summary.setText(ldb.summary())
        for name in ldb.schedules:
            self.schedule_combo.addItem(name, name)

        self.frame_table.setRowCount(len(ldb.frames))
        for row, frame in enumerate(sorted(ldb.frames.values(), key=lambda f: f.frame_id)):
            items = [
                QTableWidgetItem(f"0x{frame.frame_id:02X}"),
                QTableWidgetItem(frame.name),
                QTableWidgetItem(frame.publisher or "-"),
                QTableWidgetItem(str(frame.size)),
                QTableWidgetItem(str(len(frame.signals))),
            ]
            for col, item in enumerate(items):
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.frame_table.setItem(row, col, item)

    def fill_from_schedule(self) -> None:
        """把选中的调度表填进轮询列表。"""
        if self._ldf is None:
            return
        name = self.schedule_combo.currentData()
        if not name:
            return
        specs = self._ldf.schedule_specs(name)
        if not specs:
            return
        self.poll_table.setRowCount(0)
        for spec in specs:
            self.add_spec(spec)

    # ------------------------------------------------------------ 轮询列表

    def add_spec(self, spec: FrameSpec) -> None:
        """往轮询列表里加一行。"""
        row = self.poll_table.rowCount()
        self.poll_table.insertRow(row)

        check = QTableWidgetItem("")
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        check.setCheckState(Qt.Checked)
        self.poll_table.setItem(row, COL_CHECK, check)

        id_item = QTableWidgetItem(f"0x{spec.frame_id:02X}")
        self.poll_table.setItem(row, COL_ID, id_item)

        name_item = QTableWidgetItem(spec.name)
        self.poll_table.setItem(row, COL_NAME, name_item)

        len_item = QTableWidgetItem(str(spec.length))
        self.poll_table.setItem(row, COL_LEN, len_item)

        period_item = QTableWidgetItem(f"{spec.delay_ms:g}")
        self.poll_table.setItem(row, COL_PERIOD, period_item)

        cks_item = QTableWidgetItem(spec.checksum_type.label)
        self.poll_table.setItem(row, COL_CKSUM, cks_item)

        self._emit_specs()

    def remove_selected(self) -> None:
        """删除选中的行。"""
        rows = sorted(
            {index.row() for index in self.poll_table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            self.poll_table.removeRow(row)
        self._emit_specs()

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.poll_table.rowCount()):
            item = self.poll_table.item(row, COL_CHECK)
            if item is not None:
                item.setCheckState(state)
        self._emit_specs()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == COL_CHECK:
            self._emit_specs()

    def specs(self) -> List[FrameSpec]:
        """当前勾选的轮询帧。"""
        specs: List[FrameSpec] = []
        for row in range(self.poll_table.rowCount()):
            check = self.poll_table.item(row, COL_CHECK)
            if check is None or check.checkState() != Qt.Checked:
                continue

            id_text = (self.poll_table.item(row, COL_ID).text() if self.poll_table.item(row, COL_ID) else "")
            try:
                frame_id = int(id_text.strip(), 0)
            except ValueError:
                continue

            name_item = self.poll_table.item(row, COL_NAME)
            len_item = self.poll_table.item(row, COL_LEN)
            period_item = self.poll_table.item(row, COL_PERIOD)
            cks_item = self.poll_table.item(row, COL_CKSUM)

            try:
                length = int((len_item.text() if len_item else "0").strip() or 0)
            except ValueError:
                length = 0
            try:
                period = float((period_item.text() if period_item else "10").strip() or 10.0)
            except ValueError:
                period = 10.0

            checksum_type = (
                ChecksumType.CLASSIC
                if (cks_item and "经典" in cks_item.text())
                else ChecksumType.ENHANCED
            )

            specs.append(
                FrameSpec(
                    frame_id=frame_id,
                    length=length,
                    name=name_item.text() if name_item else "",
                    checksum_type=checksum_type,
                    delay_ms=period,
                )
            )
        return specs

    def _emit_specs(self) -> None:
        self.specsChanged.emit(self.specs())
