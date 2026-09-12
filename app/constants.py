"""项目级常量。

内部统一使用 GTK accelerator 语法（例如 ``<Control><Alt>space``）表示快捷键，
仅在传给 XDG 桌面门户时转换为 shortcuts 规范要求的 xkbcommon 形式
（``CTRL+ALT+space``）。转换函数见 :mod:`app.shortcuts.accels`。
"""

from __future__ import annotations

APP_ID = "io.github.panwenzheng.DeepLQuickTranslate"
APP_NAME = "DeepL 快捷翻译"
BINARY_NAME = "deepl-quick-translate"
VERSION = "1.0.0"

# 全局快捷键
DEFAULT_SHORTCUT_ACCEL = "<Control><Alt>space"
SHORTCUT_ID = "toggle-translator"
SHORTCUT_DESCRIPTION = "唤起翻译窗口"

# DeepL
ENDPOINT_FREE = "https://api-free.deepl.com"
ENDPOINT_PRO = "https://api.deepl.com"
DEFAULT_ENDPOINT = ENDPOINT_FREE

# GSettings 兜底后端启动的命令（由已在运行的主实例接管）
GSETTINGS_TOGGLE_COMMAND = f"{BINARY_NAME} --toggle"
