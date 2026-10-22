"""报文解析面板:显示选中帧的信息与按 LDF 解码出来的信号。"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..ldf import LdfDatabase
from ..models import DecodedSignalModel
from ..protocol import LinFrame, format_data
from ..signals import decode_frame


class FramePanel(QWidget):
    """单帧详情。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ldf: Optional[LdfDatabase] = None
        self._model = DecodedSignalModel()
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        info_box = QGroupBox("帧信息")
        form = QFormLayout(info_box)
        self.frame_id_label = QLabel("-")
        self.name_label = QLabel("-")
        self.direction_label = QLabel("-")
        self.length_label = QLabel("-")
        self.data_label = QLabel("-")
        self.checksum_label = QLabel("-")
        self.note_label = QLabel("-")
        for label in (
            self.frame_id_label,
            self.name_label,
            self.direction_label,
            self.length_label,
            self.data_label,
            self.checksum_label,
            self.note_label,
        ):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("帧 ID", self.frame_id_label)
        form.addRow("帧名", self.name_label)
        form.addRow("方向", self.direction_label)
        form.addRow("数据长度", self.length_label)
        form.addRow("数据", self.data_label)
        form.addRow("校验和", self.checksum_label)
        form.addRow("备注", self.note_label)
        layout.addWidget(info_box)

        sig_box = QGroupBox("信号解码(依据 LDF)")
        sig_layout = QVBoxLayout(sig_box)
        self.table = QTableView()
        self.table.setModel(self._model)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        sig_layout.addWidget(self.table)
        self.hint = QLabel("在左侧报文列表里选中一帧即可查看解码结果。")
        self.hint.setStyleSheet("color:#555;")
        self.hint.setWordWrap(True)
        sig_layout.addWidget(self.hint)
        layout.addWidget(sig_box, 1)

    # ------------------------------------------------------------ 数据

    def set_ldf(self, ldf: Optional[LdfDatabase]) -> None:
        """切换 LDF(会影响信号解码)。"""
        self._ldf = ldf

    def show_frame(self, frame: LinFrame) -> None:
        """显示一帧。"""
        self.frame_id_label.setText(f"0x{frame.frame_id:02X}")
        self.name_label.setText(frame.display_name or "-")
        self.direction_label.setText("接收 (RX)" if frame.direction == "RX" else "发送 (TX)")
        self.length_label.setText(f"{frame.dlc} 字节")
        self.data_label.setText(format_data(frame.data) or "(空)")
        self.checksum_label.setText(
            f"0x{frame.checksum:02X} ({frame.checksum_type.label}) "
            f"{'校验通过' if frame.checksum_valid else '校验失败'}"
        )
        self.note_label.setText(frame.note or "-")

        frame_def = self._ldf.frame_by_id(frame.frame_id) if self._ldf else None
        if frame_def is not None and frame_def.signals:
            signals = decode_frame(frame_def, frame.data)
            self._model.set_signals(frame_def.name, signals)
            self.hint.setText(f"共 {len(signals)} 个信号,来自 LDF 帧 {frame_def.name}。")
        else:
            self._model.clear()
            self.hint.setText("没有 LDF 定义或该帧没有信号,无法解码。")
