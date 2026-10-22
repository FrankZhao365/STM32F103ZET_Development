"""真实硬件集成测试:通过完整 GUI 栈打开 PCAN-USB 并发送报文。

运行:  python _hwtest.py
"""

from __future__ import annotations

import csv as _csv
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcan_tool.ui.main_window import MainWindow

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def main() -> int:
    app = QApplication(sys.argv)
    tmpdir = tempfile.mkdtemp(prefix="pcan_hw_")
    stem = os.path.join(tmpdir, "hw")

    win = MainWindow()
    win.show()

    # 把日志指向临时目录,避免污染正式 logs/
    win.device_panel.log_path_edit.setText(stem)
    win.device_panel.log_check.setChecked(True)
    win.device_panel.csv_check.setChecked(True)
    win.device_panel.blf_check.setChecked(True)

    combo = win.device_panel.device_combo
    print("\n== 1. 设备枚举 ==")
    if combo.count() == 0:
        # 先判跳过再计数,否则"没接硬件"会被误记成一次失败
        print("  [SKIP] 本机未接 PCAN 设备,硬件测试无法执行。")
        print("         接入 PCAN-USB 后重新运行本脚本即可。")
        return 2  # 2 = 跳过(区别于 1 = 失败)
    check("枚举到 PCAN 设备", True, f"{combo.count()} 个")
    dev = combo.itemData(0)
    print(f"      设备: {dev.display}")
    print(f"      详情: {dev.describe().replace(chr(10), ' | ')}")

    print("\n== 2. 打开通道 ==")
    win.device_panel._on_connect_clicked()  # 等同于点"打开通道"
    pump(app, 1.5)

    connected = win.device_panel.connected
    check("通道已打开", connected, win.device_panel.state_label.text())
    check("状态栏显示已连接", "已连接" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())
    check("发送按钮已启用", win.send_panel.send_btn.isEnabled())
    if not connected:
        print("      系统消息:")
        print("      " + win.message_panel.view.toPlainText().replace("\n", "\n      "))
        return 1

    print("\n== 3. 发送报文 ==")
    win.send_panel.id_edit.setText("0x123")
    win.send_panel.ext_check.setChecked(False)
    win.send_panel.data_edit.setText("DE AD BE EF")
    win.send_panel._on_send()
    pump(app, 0.5)

    win.send_panel.id_edit.setText("0x18FF50E5")
    win.send_panel.ext_check.setChecked(True)
    win.send_panel.data_edit.setText("01 02")
    win.send_panel._on_send()
    pump(app, 0.5)

    rows = win.monitor_panel.model.rowCount()
    check("监控表收到发送记录", rows >= 2, f"{rows} 行")
    check("发送历史 2 条", win.send_panel.history_table.rowCount() == 2,
          f"{win.send_panel.history_table.rowCount()} 条")

    print("\n== 4. 周期发送 ==")
    win.send_panel.id_edit.setText("0x300")
    win.send_panel.ext_check.setChecked(False)
    win.send_panel.data_edit.setText("AA")
    win.send_panel.interval_spin.setValue(10.0)
    win.send_panel.repeat_spin.setValue(20)
    win.send_panel._on_cyclic_start()
    pump(app, 1.2)
    win._on_stop_cyclic()
    pump(app, 0.5)

    total_rows = win.monitor_panel.model.rowCount()
    check("周期发送产生了报文", total_rows > rows, f"共 {total_rows} 行")
    check("周期任务已全部发完", "完成" in win.message_panel.view.toPlainText()
          or "停止" in win.message_panel.view.toPlainText())

    print("\n== 5. 过滤 ==")
    win.monitor_panel.filter_edit.setText("300")
    pump(app, 0.3)
    filtered = win.monitor_panel.proxy.rowCount()
    check("按 ID 过滤生效", 0 < filtered <= total_rows, f"{filtered}/{total_rows} 行")
    win.monitor_panel.filter_edit.setText("")
    pump(app, 0.3)
    check("清空过滤后恢复", win.monitor_panel.proxy.rowCount() == total_rows)

    print("\n== 6. 关闭通道与日志 ==")
    win.device_panel._on_connect_clicked()  # 关闭
    pump(app, 1.5)
    check("通道已关闭", not win.device_panel.connected)

    csv_path, blf_path = stem + ".csv", stem + ".blf"
    check("CSV 已生成", os.path.exists(csv_path),
          f"{os.path.getsize(csv_path)} 字节" if os.path.exists(csv_path) else "缺失")
    check("BLF 已生成", os.path.exists(blf_path),
          f"{os.path.getsize(blf_path)} 字节" if os.path.exists(blf_path) else "缺失")

    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            log_rows = list(_csv.DictReader(fh))
        check("CSV 有内容", len(log_rows) > 0, f"{len(log_rows)} 行")
        check("CSV 全部为 TX (总线上无对端)", all(r["dir"] == "TX" for r in log_rows))
        ids = sorted({r["id_hex"] for r in log_rows})
        check("CSV 记录了多个 ID", len(ids) >= 3, ", ".join(ids))
        ext = [r for r in log_rows if r["id_type"] == "EXT"]
        check("CSV 记录扩展帧", len(ext) >= 1, f"{len(ext)} 行")
        print(f"      ID 列表: {ids}")
        print(f"      示例行 : {log_rows[0]}")

    if os.path.exists(blf_path):
        from can.io.blf import BLFReader

        with open(blf_path, "rb") as fh:
            blf_msgs = list(BLFReader(fh))
        check("BLF 可被 PCAN-Basic 格式解析", len(blf_msgs) > 0, f"{len(blf_msgs)} 帧")
        if blf_msgs:
            print(f"      BLF 首帧: id=0x{blf_msgs[0].arbitration_id:X} "
                  f"data={bytes(blf_msgs[0].data).hex(' ')} tx={not blf_msgs[0].is_rx}")

    win.close()
    print("\n" + "=" * 54)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: " + ", ".join(FAILURES))
        print(f"日志目录: {tmpdir}")
        return 1
    print("全部通过")
    print(f"日志样例: {tmpdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())