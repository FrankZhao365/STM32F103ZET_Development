"""GUI 冒烟测试:离屏启动主窗口,校验设备枚举与面板装配。

运行:  python _guitest.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcan_tool.ui.main_window import MainWindow

FAILURES = []


def pcan_present() -> bool:
    """系统里是否存在 PCAN 设备(不依赖本项目的代码)。"""
    try:
        import can.interfaces.pcan.pcan as pcan_mod

        # 直接问驱动,不经过我们的封装
        return bool(pcan_mod.PcanBus._detect_available_configs())
    except Exception:
        return False


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    print("\n== 界面装配 ==")
    check("主窗口创建", win is not None, win.windowTitle())
    check("监控面板有表格模型", win.monitor_panel.model is not None)
    check("监控面板过滤代理", win.monitor_panel.proxy is not None)
    check("统计面板创建", win.stats_panel.table.columnCount() == 8)
    check("系统消息面板创建", win.message_panel.view is not None)
    check("停靠窗口 3 个", len(win.findChildren(type(win.left_dock))) == 3)

    print("\n== PCAN 设备枚举 ==")
    combo = win.device_panel.device_combo
    n = combo.count()
    if n == 0 and not pcan_present():
        print(f"  [SKIP] 本机未接 PCAN 设备,跳过设备相关断言"
              f"(接入后重跑可覆盖)")
        n = None
    elif n:
        check("枚举到 PCAN 通道", True, f"{n} 个")
    else:
        # 系统里能看到 PCAN 却枚举不到,那才是真问题
        check("枚举到 PCAN 通道", False, "系统有 PCAN 设备但枚举结果为 0")
    if n:
        for i in range(n):
            dev = combo.itemData(i)
            print(f"      - {dev.display}")
        check("设备信息已填充", win.device_panel.device_info.text() not in ("", "-"))
        check("打开通道按钮可用", win.device_panel.connect_btn.isEnabled())
        check("初始波特率 500k", win.device_panel.bitrate_combo.currentData() == 500000)

    print("\n== 输入解析 ==")
    from pcan_tool.ui.utils import parse_can_id, parse_data

    check("解析 0x1A3 -> [0x1A3]", parse_can_id("0x1A3") == [0x1A3])
    check("解析 '1A3,737' 多 ID", parse_can_id("1A3,737") == [0x1A3, 0x737])
    check("解析数据 '01 02 03'", parse_data("01 02 03") == b"\x01\x02\x03")
    check("解析数据 '010203'", parse_data("010203") == b"\x01\x02\x03")
    for bad, why in (("0x800", "标准帧越界"), ("ZZ", "非十六进制")):
        try:
            parse_can_id(bad)
            check(f"拒绝{bad}", False)
        except ValueError:
            check(f"拒绝{bad} ({why})", True)
    try:
        parse_data("01 02 03 04 05 06 07 08 09")
        check("拒绝超长数据", False)
    except ValueError:
        check("拒绝超长数据 (>8 字节)", True)

    print("\n== 关闭 ==")
    win.close()
    check("主窗口可正常关闭", True)

    print("\n" + "=" * 54)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: " + ", ".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())