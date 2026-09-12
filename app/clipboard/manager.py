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
        clipboard.read_text_async(None, self._on_read_text, callback)

    def _on_read_text(self, clipboard, result, callback: Callable[[str | None], None]) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error as exc:
            log.debug("clipboard: read failed (%s)", exc.message)
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
        clipboard.set_text(text)
        log.debug("clipboard: result copied (%d chars)", len(text))
