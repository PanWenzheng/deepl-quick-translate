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
# 热键激活后的"按键守卫"上限。触发快捷键里的那个键（默认 space）会在窗口拿到焦点
# 后才被合成器补投递过来——实测比 present() 晚约 570ms，并且用户按住不放时还会被
# 系统自动重复一连串。守卫期间吞掉该键的按下事件，直到用户按了别的键或超时为止。
# 代价：窗口刚出现的极短时间内，无法把这个触发键本身当作第一个字符输入。
TRIGGER_KEY_GUARD_US = 1_500_000

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

        self._install_key_controller()

    # ------------------------------------------------------------------ 行为

    def _parse_trigger_keyval(self) -> int:
        """取当前快捷键加速键的键值，用于识别"是不是热键里那个键"。"""
        ok, keyval, _mods = Gtk.accelerator_parse(self._config.shortcut.preferred_trigger)
        return keyval if ok else Gdk.KEY_space

    def _install_key_controller(self) -> None:
        controller = Gtk.EventControllerKey()
        # 必须用捕获阶段：输入框（GtkEntry）会先消费空格这类文本按键，
        # 冒泡阶段的控制器根本看不到它们，也就没法拦掉热键泄漏的那个按键。
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        now = GLib.get_monotonic_time()
        if self._guard_active:
            if now >= self._guard_deadline_us:
                self._guard_active = False
            elif keyval == self._trigger_keyval:
                log.debug("window: swallowed leaked trigger key (hotkey spillover)")
                return True
            else:
                # 用户已经开始正常输入，守卫解除
                self._guard_active = False
        if keyval == Gdk.KEY_Escape:
            log.debug("window: Esc pressed, hiding")
            self.hide()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            # 提交逻辑在 M3 接入，这里只记录，避免误以为已实现
            log.debug("window: submit requested (translation lands in M3)")
            return True
        return False

    def present_with_focus(self, *, arm_key_guard: bool = False) -> None:
        """唤起窗口并确保输入框拿到焦点（后续步骤才能读剪贴板）。"""
        self._last_present_us = GLib.get_monotonic_time()
        if arm_key_guard:
            self._guard_active = True
            self._guard_deadline_us = self._last_present_us + TRIGGER_KEY_GUARD_US
        self.present()
        self._entry.grab_focus()
        log.debug("window: presented")
