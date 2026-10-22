"""PCAN CAN 上位机 —— 程序入口。

用法:
    python main.py
    双击 启动.bat        (推荐,会自动挑选已装好依赖的 Python)
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 允许直接运行本文件(而不是 python -m pcan_tool)
sys.path.insert(0, BASE_DIR)

MB_ICONERROR = 0x10
MB_TOPMOST = 0x40000


def _log_path() -> str:
    return os.path.join(BASE_DIR, "logs", "app.log")


def _setup_logging() -> None:
    log_dir = os.path.join(BASE_DIR, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            handlers=[logging.FileHandler(_log_path(), encoding="utf-8")],
        )
    except OSError:
        # 日志目录不可写时退回只输出到 stderr,不影响程序运行
        logging.basicConfig(level=logging.WARNING)


def _fatal(title: str, message: str) -> None:
    """致命错误提示。

    特意用 Windows 原生 MessageBox 而不是 QMessageBox —— 最常见的启动失败
    恰恰是"PyQt5 没装",此时 Qt 根本不可用;再叠加双击运行时控制台会一闪而过,
    用户只会看到"双击了没反应"。原生对话框不依赖任何第三方库,一定能弹出来。
    """
    print(f"\n[{title}]\n{message}\n", file=sys.stderr)
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"\n[{title}] {message}\n")
    except OSError:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, MB_ICONERROR | MB_TOPMOST)
    except Exception:
        pass


def main() -> int:
    _setup_logging()

    # ---- 依赖检查(顺序重要:先 PyQt5,再 python-can)----
    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError:
        _fatal(
            "缺少 PyQt5",
            "当前 Python 没有安装 PyQt5。\n\n"
            f"当前解释器: {sys.executable}\n"
            f"版本: {sys.version.split()[0]}\n\n"
            "解决办法(二选一):\n"
            "  1. 给当前这个 Python 装上依赖:\n"
            f'     "{sys.executable}" -m pip install -r requirements.txt\n\n'
            "  2. 双击项目里的 启动.bat,它会自动挑选已装好依赖的 Python。",
        )
        return 1

    try:
        import can  # noqa: F401
    except ImportError:
        _fatal(
            "缺少 python-can",
            "当前 Python 没有安装 python-can。\n\n"
            f"当前解释器: {sys.executable}\n\n"
            f'解决办法: "{sys.executable}" -m pip install -r requirements.txt',
        )
        return 1

    try:
        from pcan_tool import APP_NAME
        from pcan_tool.ui.main_window import MainWindow
    except Exception:
        detail = traceback.format_exc()
        logging.getLogger(__name__).error("导入失败:\n%s", detail)
        _fatal("启动失败", f"导入程序模块时出错:\n\n{detail}")
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    try:
        window = MainWindow()
        window.show()
    except Exception:
        detail = traceback.format_exc()
        logging.getLogger(__name__).error("创建主窗口失败:\n%s", detail)
        _fatal("启动失败", f"创建主窗口时出错:\n\n{detail}")
        return 1

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())