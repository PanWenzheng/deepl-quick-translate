"""DeepL 快捷翻译：Ubuntu GNOME 下的极简 DeepL 桌面入口。"""

import gi

# 在导入任何 Gtk/Adw 符号之前锁定版本，避免 PyGI 加载到错误的主版本
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .constants import APP_ID, APP_NAME, VERSION

__all__ = ["APP_ID", "APP_NAME", "VERSION"]
