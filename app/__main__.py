"""``python3 -m app`` 入口。

本地命令（设置/清除/查看 API Key）在图形应用之前处理，避免被转发给运行中的主实例。
"""

from __future__ import annotations

import logging
import sys

from gi.repository import GLib

from .application import main
from .cli import handle_local_command, is_local_command

# 进程内最早的时刻：用于衡量"启动到转交完成"的开销（不含解释器自身启动时间）
PROCESS_START_US = GLib.get_monotonic_time()

if __name__ == "__main__":
    arguments = sys.argv[1:]
    if is_local_command(arguments):
        sys.exit(handle_local_command(arguments))
    status = main()
    if status == 0:
        # 第二个实例的全部工作就是把命令行转交给主实例后退出；
        # 这段耗时（主要是 Python 与 GI 的导入）是"按键到窗口出现"的真实组成部分
        logging.getLogger("app").debug(
            "secondary instance finished in %.1f ms (import + forward)",
            (GLib.get_monotonic_time() - PROCESS_START_US) / 1000,
        )
    sys.exit(status)
