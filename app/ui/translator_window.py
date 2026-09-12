"""翻译窗口（M1 骨架）。

本阶段只负责"能被唤起、能拿到焦点、能关闭"，剪贴板预填（M2）、输入法守卫（M2）
与翻译请求（M3）随后接入。
"""

from __future__ import annotations

import logging

from gi.repository import Gdk, GLib, Gtk

from ..config.manager import Config
from ..constants import APP_NAME

log = logging.getLogger(__name__)

WINDOW_WIDTH = 640
# 热键激活后的"按键守卫"。触发快捷键里的那个键（默认 space）会在窗口拿到焦点后才被
# 合成器补投递过来；更糟的是实测**松开事件往往根本送不到窗口**，于是 GTK 认为该键
# 一直被按着，按系统重复率无限重复出空格（实测 3 秒内 80+ 次，间隔约 27ms）。
# 因此守卫不能"按时间到期"，只能等该键松开、用户按了别的键、或到达兜底上限。
# 代价：窗口刚出现时，若这个触发键恰好是用户想输入的第一个字符，会被吞掉一次
# （其松开事件会随即解除守卫，之后即可正常输入）。
TRIGGER_KEY_GUARD_MAX_US = 30_000_000
# 吞掉的按键超过这个数量就基本可以断定是"按键卡在按下状态"，记一条警告便于排查
TRIGGER_KEY_SWALLOW_WARN = 500

CSS = b"""
.translator-surface {
    background-color: @theme_bg_color;
    border-radius: 12px;
    border: 1px solid alpha(currentColor, 0.12);
}
.translator-input {
    font-size: 15px;
}
.translator-hint {
    font-size: 12px;
    opacity: 0.55;
}
.translator-result {
    font-size: 15px;
}
"""


def _install_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


class TranslatorWindow(Gtk.ApplicationWindow):
    """Spotlight 风格的单窗口。窗口只隐藏、不销毁，进程继续常驻。"""

    def __init__(self, application: Gtk.Application, config: Config) -> None:
        super().__init__(application=application, title=APP_NAME)
        self._config = config
        self._last_present_us = 0
        self._guard_active = False
        self._guard_deadline_us = 0
        self._swallowed_keys = 0
        self._trigger_keyval = self._parse_trigger_keyval()

        _install_css()

        # 无标题栏；关闭（点 X 或 Esc）只隐藏，进程继续持有剪贴板与快捷键
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(WINDOW_WIDTH, -1)
        self.set_hide_on_close(True)
        self.set_title(APP_NAME)

        self._entry = Gtk.Entry()
        self._entry.set_hexpand(True)
        self._entry.set_placeholder_text("输入要翻译的内容")
        self._entry.add_css_class("translator-input")
        # 提交走 Entry 的 activate 信号：输入法组合（preedit）期间 GTK 的 IM context
        # 会先消费 Enter 用于"上屏"当前候选/原始字母，activate 不会被触发，
        # 因此不会出现"输入法还没确认就提交翻译"的误判。
        self._entry.connect("activate", self._on_entry_activate)

        self._settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        self._settings_button.set_has_frame(False)
        self._settings_button.set_tooltip_text("设置")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.append(Gtk.Image.new_from_icon_name("system-search-symbolic"))
        header.append(self._entry)
        header.append(self._settings_button)

        self._result = Gtk.Label(label="")
        self._result.set_xalign(0)
        self._result.set_wrap(True)
        self._result.set_selectable(True)
        self._result.add_css_class("translator-result")
        self._result.set_visible(False)

        self._hint = Gtk.Label(label="Enter 翻译 · Shift+Enter 换行 · Esc 关闭")
        self._hint.set_xalign(0)
        self._hint.add_css_class("translator-hint")

        surface = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        surface.add_css_class("translator-surface")
        surface.set_margin_top(16)
        surface.set_margin_bottom(12)
        surface.set_margin_start(16)
        surface.set_margin_end(16)
        surface.append(header)
        surface.append(self._result)
        surface.append(self._hint)
        self.set_child(surface)

        self._install_key_controllers()

    # ------------------------------------------------------------------ 行为

    def _parse_trigger_keyval(self) -> int:
        """取当前快捷键加速键的键值，用于识别"是不是热键里那个键"。"""
        ok, keyval, _mods = Gtk.accelerator_parse(self._config.shortcut.preferred_trigger)
        return keyval if ok else Gdk.KEY_space

    def _install_key_controllers(self) -> None:
        # 捕获阶段：只负责吞掉热键泄漏的按键。
        # 必须用捕获阶段，因为输入框会先消费空格这类文本按键，
        # 冒泡阶段的控制器根本看不到它们。
        guard = Gtk.EventControllerKey()
        guard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        guard.connect("key-pressed", self._on_key_pressed_capture)
        guard.connect("key-released", self._on_key_released_capture)
        self.add_controller(guard)

        # 冒泡阶段：只处理 Esc。放在冒泡阶段是有意的——输入法组合中按 Esc
        # 应当先取消候选，此时事件已被输入法消费，不会传到这里，窗口也就不关。
        bubble = Gtk.EventControllerKey()
        bubble.connect("key-pressed", self._on_key_pressed_bubble)
        self.add_controller(bubble)

    def _on_key_pressed_capture(self, _controller, keyval, _keycode, _state) -> bool:
        now = GLib.get_monotonic_time()
        if self._guard_active:
            if now >= self._guard_deadline_us:
                self._end_key_guard("timeout")
            elif keyval == self._trigger_keyval:
                # 按住热键时会被系统自动重复成一长串，这里只计数，结束时汇总一条日志
                self._swallowed_keys += 1
                return True
            else:
                # 用户已经开始正常输入，守卫解除
                self._end_key_guard("user-typing")
        return False

    def _on_key_released_capture(self, _controller, keyval, _keycode, _state) -> bool:
        # 触发键松开后系统不再重复，泄漏也就结束了
        if self._guard_active and keyval == self._trigger_keyval:
            self._end_key_guard("trigger-released")
        return False

    def _end_key_guard(self, reason: str) -> None:
        if self._guard_active and self._swallowed_keys:
            message = "window: swallowed %d leaked trigger key event(s) (%s)"
            if self._swallowed_keys >= TRIGGER_KEY_SWALLOW_WARN:
                log.warning(message, self._swallowed_keys, reason)
            else:
                log.debug(message, self._swallowed_keys, reason)
        self._guard_active = False
        self._swallowed_keys = 0

    def _on_key_pressed_bubble(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            log.debug("window: Esc pressed, hiding")
            self.hide()
            return True
        return False

    def _on_entry_activate(self, _entry) -> None:
        """Enter 提交（由输入法确认提交后的再次回车触发）。翻译逻辑在 M3 接入。"""
        log.debug("window: submit requested (translation lands in M3)")

    def present_with_focus(
        self, *, arm_key_guard: bool = False, activation_token: str | None = None
    ) -> None:
        """唤起窗口并确保输入框拿到焦点（后续步骤才能读剪贴板）。"""
        self._last_present_us = GLib.get_monotonic_time()
        if arm_key_guard:
            self._guard_active = True
            self._swallowed_keys = 0
            self._guard_deadline_us = self._last_present_us + TRIGGER_KEY_GUARD_MAX_US
        self._apply_activation_token(activation_token)
        self.present()
        self._entry.grab_focus()
        # 选中已有内容：用户直接键入即可整体替换（M2 会用剪贴板内容整体覆盖）
        self._entry.select_region(0, -1)
        log.debug("window: presented")

    def _apply_activation_token(self, activation_token: str | None) -> None:
        """把门户给的 activation token 交给合成器。

        这样合成器知道这次激活源自全局快捷键，才能正确处理焦点与触发按键
        （否则按键松开事件可能永远送不到窗口，GTK 会一直重复那个键）。
        """
        if not activation_token:
            return
        try:
            if not self.get_realized():
                self.realize()
            surface = self.get_surface()
            setter = getattr(surface, "set_startup_id", None)
            if setter is None:
                log.debug("window: surface does not support activation tokens")
                return
            setter(activation_token)
            log.debug("window: activation token applied")
        except Exception as exc:  # noqa: BLE001 - 失败不影响窗口呈现
            log.debug("window: cannot apply activation token (%s)", exc)
