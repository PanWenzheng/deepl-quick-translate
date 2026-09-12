"""应用主体：单实例、命令行路由与生命周期。"""

from __future__ import annotations

import logging
import os
import faulthandler
import signal
import sys

from gi.repository import Adw, Gio, GLib

from .config.manager import Config, ConfigManager
from .constants import APP_ID, APP_NAME, VERSION
from .logging_setup import setup_logging
from .shortcuts.manager import ShortcutManager
from .ui.translator_window import TranslatorWindow

log = logging.getLogger(__name__)

# 按住全局快捷键时 GNOME 会连续触发 Activated，这里做一次去抖
ACTIVATION_DEBOUNCE_US = 300_000

USAGE = f"""用法：{APP_NAME} [选项]

  --toggle        唤起翻译窗口（不带参数时的默认行为）
  --background    后台启动，不打开窗口（供开机自启动使用）
  --settings      打开设置窗口
  --quit          退出正在运行的实例
  --verbose       输出调试日志
  --version       显示版本
  -h, --help      显示本帮助
"""

# 已知选项。未知选项只记一条警告，不影响启动。
KNOWN_FLAGS = frozenset(
    {"--toggle", "--background", "--settings", "--quit", "--verbose", "--version", "-h", "--help"}
)


class TranslatorApplication(Adw.Application):
    """常驻后台的翻译应用。窗口全部隐藏时进程仍存活。"""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._config_manager = ConfigManager()
        self._config: Config = Config()
        self._window: TranslatorWindow | None = None
        self._shortcuts: ShortcutManager | None = None
        self._last_activation_us = 0
        self._debounced_activations = 0
        self.shortcut_ok: bool = False
        self.shortcut_error: str | None = None

    # ------------------------------------------------------------------ 生命周期

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        self._config = self._config_manager.load()
        setup_logging(self._config.log_level)
        log.info("%s %s starting (pid %s)", APP_NAME, VERSION, os.getpid())

        # 即使没有任何窗口，进程也要常驻
        self.hold()

        self._shortcuts = ShortcutManager(
            APP_ID,
            self._config,
            self._on_shortcut_activated,
            on_config_changed=lambda config: self._config_manager.save(config),
        )
        # 放在空闲回调里注册，避免拖慢启动
        GLib.idle_add(self._register_shortcuts)

        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)

    def do_shutdown(self) -> None:
        if self._shortcuts is not None:
            self._shortcuts.unregister()
        log.info("shutdown complete")
        Adw.Application.do_shutdown(self)

    def do_activate(self) -> None:
        self.show_window()

    # ------------------------------------------------------------------ 命令行

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """主实例统一处理参数；第二个实例的参数会由 GApplication 转到这里。"""
        args = command_line.get_arguments()[1:]

        if any(arg in ("-h", "--help") for arg in args):
            print(USAGE, file=sys.stderr)
            return 0
        if "--version" in args:
            print(VERSION)
            return 0
        if "--verbose" in args:
            setup_logging("DEBUG")
            # 开发诊断：SIGUSR1 打印所有线程的 Python 栈，便于排查卡死
            faulthandler.register(signal.SIGUSR1)
        if "--quit" in args:
            log.info("quit requested")
            self.quit()
            return 0
        if "--settings" in args:
            log.info("settings window arrives in M4")
            return 0
        if "--background" in args:
            log.info("started in background; waiting for the global shortcut")
            return 0

        unknown = [arg for arg in args if arg.startswith("-") and arg not in KNOWN_FLAGS]
        if unknown:
            log.warning("unknown options ignored: %s", " ".join(unknown))

        self.show_window()
        return 0

    # ------------------------------------------------------------------ 内部

    def _register_shortcuts(self) -> bool:
        # 单独兜住异常：空闲回调里抛错只会静默丢失，排查起来很痛
        try:
            if self._shortcuts is not None:
                log.info("registering global shortcut")
                self._shortcuts.register(self._on_shortcut_status)
        except Exception:  # noqa: BLE001 - 兜底，避免注册流程静默失败
            log.exception("global shortcut registration crashed")
        return GLib.SOURCE_REMOVE

    def _on_shortcut_status(self, ok: bool, error: str | None) -> None:
        self.shortcut_ok = ok
        self.shortcut_error = error
        if ok:
            self._config_manager.save(self._config)
        else:
            log.error("global shortcut is unavailable: %s", error)

    def _on_shortcut_activated(
        self, shortcut_id: str, _timestamp: int, activation_token: str | None = None
    ) -> None:
        now = GLib.get_monotonic_time()
        if now - self._last_activation_us < ACTIVATION_DEBOUNCE_US:
            # 按住热键会连续触发，这里只计数，恢复正常后再汇总一条
            self._debounced_activations += 1
            return
        if self._debounced_activations:
            log.debug("ignored %d repeated shortcut activation(s)", self._debounced_activations)
            self._debounced_activations = 0
        self._last_activation_us = now
        log.debug("shortcut activated: %s", shortcut_id)
        self.show_window(arm_key_guard=True, activation_token=activation_token)

    def show_window(
        self, *, arm_key_guard: bool = False, activation_token: str | None = None
    ) -> None:
        if self._window is None:
            self._window = TranslatorWindow(self, self._config, on_submit=self._on_submit)
        self._window.present_with_focus(
            arm_key_guard=arm_key_guard, activation_token=activation_token
        )

    def _on_submit(self, text: str) -> None:
        """提交入口。真正的 DeepL 请求在 M3 接入；这里只记录长度，不记录内容。"""
        # 隐私约束：日志中绝不出现用户文本
        log.debug("translation requested (%d chars); DeepL call lands in M3", len(text))

    def _on_signal(self) -> bool:
        log.info("signal received; quitting")
        self.quit()
        return GLib.SOURCE_REMOVE


def main(argv: list[str] | None = None) -> int:
    app = TranslatorApplication()
    return app.run(argv if argv is not None else sys.argv)
