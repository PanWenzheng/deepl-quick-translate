"""翻译窗口。

已实现：剪贴板预填、多行输入、输入法友好的 Enter/Shift+Enter、失焦自动隐藏，
以及翻译请求的加载态、结果与错误的展示。
"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Gdk, GLib, Gtk

from ..clipboard.manager import ClipboardManager
from ..config.manager import Config
from ..constants import APP_NAME
from ..deepl.errors import TranslationError

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
.translator-input text {
    background-color: transparent;
}
.translator-placeholder {
    font-size: 15px;
    opacity: 0.45;
}
.translator-hint {
    font-size: 12px;
    opacity: 0.55;
}
.translator-result {
    font-size: 15px;
}
.translator-detail {
    font-size: 12px;
    opacity: 0.7;
}
.translator-error {
    color: #c01c28;
}
"""

# 输入框自动增长的高度上限，超过后输入框内部滚动
INPUT_MAX_HEIGHT = 220
# 呈现后这段时间内的失焦不算"用户切走"，避免与合成器焦点交接打架
FOCUS_LOSS_GRACE_US = 250_000


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

    def __init__(
        self,
        application: Gtk.Application,
        config: Config,
        on_submit: Callable[[str], None] | None = None,
        on_dismiss: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(application=application, title=APP_NAME)
        self._config = config
        self._on_submit = on_submit
        self._on_dismiss = on_dismiss
        self._busy = False
        self._clipboard = ClipboardManager()
        self._last_present_us = 0
        self._guard_active = False
        self._guard_deadline_us = 0
        self._swallowed_keys = 0
        self._trigger_keyval = self._parse_trigger_keyval()
        # 剪贴板预填的会话序号：异步结果回来时用它判断是否已经过期
        self._prefill_serial = 0
        self._user_edited = False
        # 上一次实际填入输入框的剪贴板内容（None 表示"当时没有文本"）
        self._last_clipboard_text: str | None = None

        _install_css()

        # 无标题栏；关闭（点 X 或 Esc）只隐藏，进程继续持有剪贴板与快捷键
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(WINDOW_WIDTH, -1)
        self.set_hide_on_close(True)
        self.set_title(APP_NAME)

        self._input = Gtk.TextView()
        self._input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._input.set_accepts_tab(False)
        self._input.set_hexpand(True)
        self._input.add_css_class("translator-input")
        self._input.set_top_margin(0)
        self._input.set_bottom_margin(0)

        # 输入框随内容增长，超过上限后内部滚动
        input_scroller = Gtk.ScrolledWindow()
        input_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        input_scroller.set_propagate_natural_height(True)
        input_scroller.set_max_content_height(INPUT_MAX_HEIGHT)
        input_scroller.set_child(self._input)

        # TextView 没有占位符，用一个不吃事件的标签叠上去
        self._placeholder = Gtk.Label(label="输入要翻译的内容")
        self._placeholder.set_xalign(0)
        self._placeholder.set_yalign(0)
        self._placeholder.set_can_target(False)
        self._placeholder.add_css_class("translator-placeholder")

        input_overlay = Gtk.Overlay()
        input_overlay.set_hexpand(True)
        input_overlay.set_child(input_scroller)
        input_overlay.add_overlay(self._placeholder)

        self._buffer = self._input.get_buffer()
        self._buffer.connect("changed", self._on_buffer_changed)

        self._settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        self._settings_button.set_has_frame(False)
        self._settings_button.set_tooltip_text("设置")

        icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        icon.set_valign(Gtk.Align.START)
        icon.set_margin_top(4)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.append(icon)
        header.append(input_overlay)
        header.append(self._settings_button)

        self._result_text = Gtk.Label(label="")
        self._result_text.set_xalign(0)
        self._result_text.set_wrap(True)
        self._result_text.set_selectable(True)
        self._result_text.add_css_class("translator-result")

        self._result_detail = Gtk.Label(label="")
        self._result_detail.set_xalign(0)
        self._result_detail.set_wrap(True)
        self._result_detail.add_css_class("translator-detail")
        self._result_detail.set_visible(False)

        self._result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._result_box.append(self._result_text)
        self._result_box.append(self._result_detail)
        self._result_box.set_visible(False)

        self._result_separator = Gtk.Separator()
        self._result_separator.set_visible(False)

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
        surface.append(self._result_separator)
        surface.append(self._result_box)
        surface.append(self._hint)
        self.set_child(surface)

        self._install_key_controllers()
        self.connect("notify::is-active", self._on_active_changed)
        self._on_buffer_changed(self._buffer)

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

        # 走到这里说明是用户的真实按键，后续到达的剪贴板预填不应再覆盖输入
        self._user_edited = True

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if _state & Gdk.ModifierType.SHIFT_MASK:
                return False  # Shift+Enter 交给输入框插入换行
            if self._im_wants_key(_controller):
                return True  # 输入法正在组合，这一下回车用于上屏/确认候选
            self._submit()
            return True
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
            self._dismiss()
            return True
        return False

    def _im_wants_key(self, controller) -> bool:
        """询问输入法要不要这个按键（组合中会返回 True）。

        GTK 4.22 起 ``Gtk.TextView`` 不再暴露 IM context，但保留了
        ``im_context_filter_keypress``——它是唯一能区分"输入法正在组合"与
        "用户真的要提交"的公开途径。
        """
        event = controller.get_current_event() if hasattr(controller, "get_current_event") else None
        if event is None:
            return False
        try:
            return bool(self._input.im_context_filter_keypress(event))
        except (AttributeError, TypeError) as exc:
            log.debug("window: cannot query input method (%s)", exc)
            return False

    def _submit(self) -> None:
        if self._busy:
            # 规格 FR-SUBMIT-2：请求进行中再按 Enter 一律忽略
            log.debug("window: submit ignored (request already in flight)")
            return
        text = self.text
        if not text.strip():
            # 规格 FR-SUBMIT-3：空输入不发送请求，窗口保持打开
            log.debug("window: submit ignored (empty input)")
            return
        if self._on_submit is not None:
            self._on_submit(text)
        else:
            log.debug("window: submit requested (translation lands in M3)")

    # ------------------------------------------------------------------ 内容

    @property
    def text(self) -> str:
        return self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False
        )

    def set_text(self, text: str, *, select_all: bool = True) -> None:
        self._buffer.set_text(text)
        if select_all and text:
            self._buffer.select_range(
                self._buffer.get_start_iter(), self._buffer.get_end_iter()
            )
            self._input.scroll_to_iter(self._buffer.get_start_iter(), 0.0, False, 0.0, 0.0)

    def _on_buffer_changed(self, buffer) -> None:
        self._placeholder.set_visible(buffer.get_char_count() == 0)

    # ------------------------------------------------------------------ 结果与状态

    @property
    def busy(self) -> bool:
        return self._busy

    def show_loading(self) -> None:
        self._busy = True
        self._result_text.remove_css_class("translator-error")
        self._result_text.set_text("翻译中…")
        self._result_detail.set_visible(False)
        self._show_result_area(True)

    def show_result(self, text: str) -> None:
        self._busy = False
        self._result_text.remove_css_class("translator-error")
        self._result_text.set_text(text)
        self._result_detail.set_visible(False)
        self._show_result_area(True)

    def show_error(self, error: TranslationError) -> None:
        self._busy = False
        main, detail = error.user_message
        if not main:
            self.clear_result()
            return
        self._result_text.add_css_class("translator-error")
        self._result_text.set_text(main)
        self._result_detail.set_text(detail)
        self._result_detail.set_visible(bool(detail))
        self._show_result_area(True)

    def clear_result(self) -> None:
        self._busy = False
        self._result_text.set_text("")
        self._result_detail.set_text("")
        self._show_result_area(False)

    def _show_result_area(self, visible: bool) -> None:
        self._result_separator.set_visible(visible)
        self._result_box.set_visible(visible)

    # ------------------------------------------------------------------ 剪贴板预填

    def _start_clipboard_prefill(self) -> None:
        """唤起后读一次剪贴板。必须在窗口拿到焦点之后调用。"""
        self._prefill_serial += 1
        serial = self._prefill_serial
        self._user_edited = False
        self._clipboard.read_text(lambda text: self._apply_prefill(serial, text))

    def _apply_prefill(self, serial: int, text: str | None) -> None:
        if serial != self._prefill_serial:
            log.debug("clipboard: prefill dropped (window re-activated)")
            return
        if self._user_edited:
            log.debug("clipboard: prefill dropped (user already typed)")
            return
        # 剪贴板没变就不动输入框：这样中途切窗口复制别的东西再回来，
        # 用户已经改过的内容不会被冲掉
        if text == self._last_clipboard_text:
            log.debug("clipboard: unchanged, keeping current input")
            return
        # 没有文本 → 清空输入框（规格 §8）
        self._last_clipboard_text = text
        self.set_text(text or "")
        log.debug("clipboard: prefill applied (has_text=%s)", bool(text))

    # ------------------------------------------------------------------ 失焦

    def _on_active_changed(self, *_args) -> None:
        if self.is_active() or not self._config.hide_on_focus_loss:
            return
        if not self.get_visible():
            return
        if GLib.get_monotonic_time() - self._last_present_us < FOCUS_LOSS_GRACE_US:
            return
        log.debug("window: focus lost, hiding")
        self._dismiss()

    def _dismiss(self) -> None:
        """隐藏窗口并通知上层（用于取消进行中的请求）。"""
        self.hide()
        if self._on_dismiss is not None:
            self._on_dismiss()

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
        self._input.grab_focus()
        if not self._busy:
            # 上一次的翻译结果不属于这次唤起
            self.clear_result()
        # 这里刻意不动输入框内容与选区：剪贴板若变了，读回来时会整体覆盖并全选；
        # 若没变，则保留用户的编辑与光标位置。
        self._start_clipboard_prefill()
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
