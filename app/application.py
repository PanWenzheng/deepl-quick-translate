"""应用主体：单实例、命令行路由与生命周期。"""

from __future__ import annotations

import logging
import os
import faulthandler
import signal
import sys

from gi.repository import Adw, Gio, GLib

from .async_runner import AsyncRunner
from .config.manager import Config, ConfigManager
from .config.secret import SecretManager
from .constants import APP_ID, APP_NAME, VERSION
from .deepl.errors import TranslationError
from .deepl.service import TranslationService
from .logging_setup import setup_logging
from .shortcuts.manager import ShortcutManager
from .ui.settings_window import SettingsWindow
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
  --set-api-key   把 DeepL API Key 写入系统密钥环（不经过图形界面）
  --api-key-status 查看 API Key 配置状态
  --clear-api-key 从系统密钥环删除 API Key
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
        self._secrets = SecretManager()
        self._config: Config = Config()
        self._window: TranslatorWindow | None = None
        self._settings_window: SettingsWindow | None = None
        self._shortcuts: ShortcutManager | None = None
        self._runner: AsyncRunner | None = None
        self._service: TranslationService | None = None
        self._request_token: object | None = None
        self._request_future = None
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
        self._log_display_backend()

        self._runner = AsyncRunner()
        self._service = TranslationService(self._config, self._secrets)

        # 即使没有任何窗口，进程也要常驻
        self.hold()

        self._shortcuts = ShortcutManager(
            APP_ID,
            self._config,
            self._on_shortcut_activated,
            on_config_changed=lambda config: self._config_manager.save(config),
        )

        # 桌面文件里的 Settings 动作要在 GNOME 右键菜单里真正可用，应用还需要导出
        # 同名（小写）的 GApplication 动作——GNOME 对运行中的应用优先走 D-Bus 调用。
        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect("activate", lambda *_args: self.open_settings())
        self.add_action(settings_action)
        # 放在空闲回调里注册，避免拖慢启动
        GLib.idle_add(self._register_shortcuts)
        GLib.idle_add(self._ensure_autostart)

        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)

    def do_shutdown(self) -> None:
        self._cancel_current_request()
        if self._runner is not None:
            self._runner.shutdown()
            self._runner = None
        if self._shortcuts is not None:
            self._shortcuts.unregister()
        log.info("shutdown complete")
        Adw.Application.do_shutdown(self)

    def do_activate(self) -> None:
        self.show_window()

    # ------------------------------------------------------------------ 命令行

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """主实例统一处理参数；第二个实例的参数会由 GApplication 转到这里。"""
        started_us = GLib.get_monotonic_time()
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
            self.open_settings()
            return 0
        if "--background" in args:
            log.info("started in background; waiting for the global shortcut")
            return 0

        unknown = [arg for arg in args if arg.startswith("-") and arg not in KNOWN_FLAGS]
        if unknown:
            log.warning("unknown options ignored: %s", " ".join(unknown))

        self.show_window()
        log.info(
            "toggle handled in %.1f ms (primary side)",
            (GLib.get_monotonic_time() - started_us) / 1000,
        )
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

    @staticmethod
    def _log_display_backend() -> None:
        """记录实际生效的显示后端：XWayland 与原生 Wayland 的行为差异很大，排查时必须知道。"""
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        log.info("display backend: %s", type(display).__name__ if display else "none")
        log.debug(
            "identity: prgname=%s application_name=%s app_id=%s",
            GLib.get_prgname(), GLib.get_application_name(), APP_ID,
        )

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
        # 性能指标 PERF-1：快捷键按下到窗口呈现的耗时。
        # 起点是收到门户/键绑定通知，不含合成器把按键事件送到我们这里的网络/总线延迟，
        # 因此这是一个偏乐观的下界；但它覆盖了配置读取、剪贴板、窗口呈现等本地开销。
        elapsed_ms = (GLib.get_monotonic_time() - now) / 1000
        log.info("window presented %.1f ms after shortcut", elapsed_ms)

    def show_window(
        self, *, arm_key_guard: bool = False, activation_token: str | None = None
    ) -> None:
        # 翻译窗口是"临时覆盖层"：把它唤起来时先收起设置窗口，免得两层叠在一起
        # 让人分不清 Esc 会关掉谁
        if self._settings_window is not None and self._settings_window.get_visible():
            log.debug("settings: hiding because the translator window was summoned")
            self._settings_window.set_visible(False)
        if self._window is None:
            self._window = TranslatorWindow(
                self,
                self._config,
                on_submit=self._on_submit,
                on_dismiss=self._on_dismiss,
                on_open_settings=self.open_settings,
            )
        self._window.present_with_focus(
            arm_key_guard=arm_key_guard, activation_token=activation_token
        )

    def _on_submit(self, text: str) -> None:
        """提交翻译。日志只记录长度，绝不记录内容。"""
        log.debug("translation requested (%d chars)", len(text))
        if self._runner is None or self._service is None or self._window is None:
            return

        self._cancel_current_request()
        self._window.show_loading()

        token = object()
        self._request_token = token
        self._request_future = self._runner.submit(
            self._service.translate(text),
            lambda result, error: self._on_translation_done(token, result, error),
        )

    def _on_translation_done(self, token: object, result, error) -> bool:
        if token is not self._request_token:
            # 请求已被取消或被新请求取代：绝不更新界面（规格 FR-SUBMIT-6）
            log.debug("dropping stale translation result")
            return False
        self._request_token = None
        self._request_future = None
        if self._window is None:
            return False

        if error is not None:
            if isinstance(error, TranslationError):
                self._window.show_error(error)
            else:
                log.error("unexpected translation failure: %s", error)
                self._window.show_error(TranslationError("unexpected", detail=str(error)))
        else:
            self._window.show_result(result.text)
        return False

    def _cancel_current_request(self) -> None:
        """取消进行中的请求，并阻止迟到结果更新界面。"""
        if self._request_future is not None:
            if self._request_future.cancel():
                log.debug("translation request cancelled")
            self._request_future = None
        self._request_token = None
        if self._window is not None and self._window.busy:
            self._window.clear_result()

    def _on_dismiss(self) -> None:
        """窗口隐藏（Esc / 失焦）时调用。"""
        self._cancel_current_request()

    # ------------------------------------------------------------------ 设置

    def open_settings(self) -> None:
        if self._settings_window is None:
            log.debug("settings: creating window")
            self._settings_window = SettingsWindow(
                self,
                config=self._config,
                secrets=self._secrets,
                on_config_changed=self._save_config,
                on_shortcut_changed=self._apply_shortcut_change,
                check_connection=self._check_connection,
            )
        log.debug("settings: presenting window")
        self._settings_window.present()

    def _save_config(self) -> None:
        self._config_manager.save(self._config)

    def _apply_shortcut_change(self) -> None:
        """快捷键被改动：先注销旧的，再按新键重新注册。"""
        self._save_config()
        if self._shortcuts is not None:
            self._shortcuts.unregister()
            self._shortcuts.register(self._on_shortcut_status)

    def _ensure_autostart(self) -> bool:
        """按配置补齐开机自启动文件（默认关闭，只有用户开启过才创建）。"""
        from .config import autostart

        if self._config.start_on_login:
            if not autostart.is_enabled():
                autostart.set_enabled(True)
            else:
                # 从开发目录换成已安装的二进制时，把旧的 Exec 替换掉
                autostart.refresh_if_needed()
        return GLib.SOURCE_REMOVE

    def _check_connection(self, done) -> None:
        """设置页的 Test Connection：走 /v2/usage，不消耗翻译额度。"""
        if self._runner is None or self._service is None:
            done(None, TranslationError("unexpected", detail="service unavailable"))
            return
        self._runner.submit(
            self._service.check_connection(), lambda result, error: done(result, error)
        )

    def _on_signal(self) -> bool:
        log.info("signal received; quitting")
        self.quit()
        return GLib.SOURCE_REMOVE


def main(argv: list[str] | None = None) -> int:
    # 显式设定程序名 = 应用 ID：桌面环境靠它把窗口与 .desktop 匹配起来取图标。
    # 否则以 `python3 -m app` 启动时程序名是 python3，Shell 匹配不上，Dock 里只能画
    # 一个通用占位图标（表现就是"图标是黑的"）。
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    app = TranslatorApplication()
    return app.run(argv if argv is not None else sys.argv)
