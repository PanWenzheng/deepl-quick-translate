"""剪贴板读写。

两条硬约束（Wayland）：

* **读取需要键盘焦点**：Mutter 只允许持有焦点的客户端读剪贴板，因此读取必须发生在
  窗口已显示并获得焦点之后。
* **写入后要保活**：译文写进剪贴板后进程不能退出，否则内容随之消失。本项目是常驻
  单实例，窗口只隐藏不销毁，因此天然满足。

隐私约束：日志里绝不出现剪贴板内容，只记录"有没有文本"。
"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Gdk, GLib

log = logging.getLogger(__name__)

# 只认纯文本；图片、HTML、文件等一律按"没有文本"处理
TEXT_MIME = "text/plain"
TEXT_MIME_UTF8 = "text/plain;charset=utf-8"

# 文件管理器复制文件时，剪贴板会带上这些 MIME 标记，同时**也会**提供一份
# text/plain（内容是文件的路径或 URI）。只判断 text/plain 会把路径当成待翻译文本，
# 因此要结合这些标记识别"这是文件而不是文本"。
FILE_LIST_MIME_TYPES = (
    "x-special/gnome-copied-files",
    "x-special/nautilus-clipboard",
    "text/uri-list",
)


def looks_like_file_paths(text: str) -> bool:
    """判断一段文本是否只是文件路径列表（每行都是绝对路径或 ``file://`` URI）。"""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return False
    return all(
        line.startswith("/") or line.startswith("file://") for line in lines
    )


class ClipboardManager:
    """只读 ``text/plain`` 的剪贴板封装。"""

    def __init__(self, display: Gdk.Display | None = None) -> None:
        self._display = display or Gdk.Display.get_default()

    @property
    def available(self) -> bool:
        return self._display is not None

    def _clipboard(self) -> Gdk.Clipboard | None:
        if self._display is None:
            return None
        return self._display.get_clipboard()

    # ------------------------------------------------------------------ 读

    def read_text(self, callback: Callable[[str | None], None]) -> None:
        """异步读取一次纯文本；无文本、格式不支持或失败时回调 ``None``。

        只接受 ``text/plain``：``Gdk.Clipboard.read_text_async`` 请求的就是文本类
        MIME，因此图片 / HTML / 文件剪贴板不会命中，符合规格 FR-CLIP-1。
        """
        clipboard = self._clipboard()
        if clipboard is None:
            callback(None)
            return
        clipboard.read_text_async(
            None, self._on_read_text, (callback, self._looks_like_file_clipboard(clipboard))
        )

    def _looks_like_file_clipboard(self, clipboard: Gdk.Clipboard) -> bool:
        try:
            mime_types = set(clipboard.get_formats().get_mime_types() or [])
        except Exception as exc:  # noqa: BLE001 - 拿不到格式就按普通文本处理
            log.debug("clipboard: cannot inspect formats (%s)", exc)
            return False
        return any(mime in mime_types for mime in FILE_LIST_MIME_TYPES)

    def _on_read_text(self, clipboard, result, user_data) -> None:
        callback, file_clipboard = user_data
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error as exc:
            log.debug("clipboard: read failed (%s)", exc.message)
            text = None
        # 文件复制：text/plain 里是路径，按规格当作"没有文本"
        if text and file_clipboard and looks_like_file_paths(text):
            log.debug("clipboard: file clipboard (path only), ignoring")
            text = None
        # 空白内容一律按"没有文本"处理，避免把一串空白填进输入框
        if not text or not text.strip():
            log.debug("clipboard: no plain text available")
            callback(None)
            return
        log.debug("clipboard: plain text available (%d chars)", len(text))
        callback(text)

    # ------------------------------------------------------------------ 写

    def write_text(self, text: str) -> None:
        """写入译文。常驻进程会继续持有 selection，因此隐藏窗口后粘贴依然有效。"""
        clipboard = self._clipboard()
        if clipboard is None:
            log.warning("clipboard: no display, cannot write")
            return
        # GTK4 的 GdkClipboard 没有 set_text()（那是我记错的 API），要经由 ContentProvider
        clipboard.set_content(Gdk.ContentProvider.new_for_value(text))
        log.debug("clipboard: result copied (%d chars)", len(text))
