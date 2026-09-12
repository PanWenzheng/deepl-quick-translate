"""``python3 -m app`` 入口。

本地命令（设置/清除/查看 API Key）在图形应用之前处理，避免被转发给运行中的主实例。
"""

from __future__ import annotations

import sys

from .application import main
from .cli import handle_local_command, is_local_command

if __name__ == "__main__":
    arguments = sys.argv[1:]
    if is_local_command(arguments):
        sys.exit(handle_local_command(arguments))
    sys.exit(main())
