"""系统消息面板:显示连接/发送/异常提示。"""

from __future__ import annotations

import time

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

MAX_LINES = 2000

COLORS = {
    "info": "#1b5e20",
    "warn": "#e65100",
    "error": "#c62828",
}


class MessagePanel(QWidget):
    """运行期间的文字提示。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        bar.addWidget(self.autoscroll)

        save_btn = QPushButton("保存…")
        save_btn.clicked.connect(self._save)
        bar.addWidget(save_btn)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(lambda: self.view.clear())
        bar.addWidget(clear_btn)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setMaximumBlockCount(MAX_LINES)
        layout.addWidget(self.view, 1)

    def log(self, level: str, text: str) -> None:
        """追加一条带颜色和时间戳的消息。"""
        stamp = time.strftime("%H:%M:%S")
        color = COLORS.get(level, "#000000")
        safe = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        self.view.appendHtml(
            f'<span style="color:{color};">[{stamp}] {safe}</span>'
        )
        if self.autoscroll.isChecked():
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _save(self):
        from PyQt5.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self, "保存系统消息", "pcan_messages.txt", "文本文件 (*.txt)"
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.view.toPlainText())