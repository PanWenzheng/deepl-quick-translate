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
from ..deepl.service import MAX_TEXT_BYTES

log = logging.getLogger(__name__)

WINDOW_WIDTH = 680
# 窗口本身透明，可见的是一张"卡片"，四周留出投影所需的空间
SURFACE_MARGIN = 20
# 热键激活后的"按键守卫"。触发快捷键里的那个键（默认 space）会在窗口拿到焦点后才被
# 合成器补投递过来；更糟的是实测**松开事件往往根本送不到窗口**，于是 GTK 认为该键
# 一直被按着，按系统重复率无限重复出空格（实测 3 秒内 80+ 次，间隔约 27ms）。
# 因此守卫不能"按时间到期"，只能等该键松开、用户按了别的键、或到达兜底上限。
# 代价：窗口刚出现时，若这个触发键恰好是用户想输入的第一个字符，会被吞掉一次
# （其松开事件会随即解除守卫，之后即可正常输入）。
TRIGGER_KEY_GUARD_MAX_US = 30_000_000
# 吞掉的按键超过这个数量就基本可以断定是"按键卡在按下状态"，记一条警告便于排查
TRIGGER_KEY_SWALLOW_WARN = 500

HINT_SUBMIT = "Enter 翻译"
HINT_NEWLINE = "Shift+Enter 换行"
HINT_CLOSE = "Esc 关闭"
HINT_COPY_CLOSE = "Ctrl+C 复制并关闭"
HINT_COPY = "Ctrl+C 复制译文"
# 复制后关闭时，先让"已复制"闪一下再关，否则用户看不到任何反馈
COPIED_FEEDBACK_MS = 200

# 窗口本体透明：屏幕上只留卡片和它的投影，避免出现一圈与壁纸颜色相近的方框。
# （CSS 字面量必须是纯 ASCII，注释一律留在 Python 这边）
CSS = b"""
window.translator-window {
    background-color: transparent;
}
.translator-surface {
    background-color: @theme_bg_color;
    border-radius: 12px;
    outline: 1px solid alpha(currentColor, 0.10);
    outline-offset: -1px;
    box-shadow: 0 1px 2px alpha(black, 0.28), 0 8px 20px alpha(black, 0.28);
    padding: 14px 16px;
}
.translator-input {
    font-size: 15px;
}
/* The TextView paints a @theme_base_color block by default; drop it */
.translator-input, .translator-input text {
    background-color: transparent;
}
.translator-input-scroller {
    background-color: transparent;
}
.translator-placeholder {
    font-size: 15px;
    opacity: 0.45;
}
.translator-result {
    font-size: 15px;
    line-height: 145%;
}
.translator-detail {
    font-size: 13px;
}
.translator-error-text {
    color: var(--error-color);
}
"""

# 输入框自动增长的高度上限，超过后输入框内部滚动
INPUT_MAX_HEIGHT = 220
# 超过这个字符数就不再"随内容自适应高度"：GTK 为了算自然高度会布局整篇文本，
# 十几万字符时每次插入都卡一下。改为固定高度 + 内部滚动，只布局可见部分。
INPUT_AUTO_GROW_MAX_CHARS = 4000
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
        on_open_settings: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(application=application, title=APP_NAME)
        self._config = config
        self._on_submit = on_submit
        self._on_dismiss = on_dismiss
        self._on_open_settings = on_open_settings
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
        # 呈现后的一小段时间内允许"补全选"：焦点落定会清掉选择，需要重新选上
        self._pending_select_all = False
        # 每次呈现递增：让"复制后延迟关窗"的定时器能确认窗口没被重新唤起
        self._present_serial = 0
        self._trimming_input = False
        # 上一次实际填入输入框的剪贴板内容（None 表示"当时没有文本"）
        self._last_clipboard_text: str | None = None

        _install_css()

        # 无标题栏；关闭（点 X 或 Esc）只隐藏，进程继续持有剪贴板与快捷键
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(WINDOW_WIDTH, -1)
        self.set_hide_on_close(True)
        self.set_title(APP_NAME)
        self.add_css_class("translator-window")

        self._input = Gtk.TextView()
        self._input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._input.set_accepts_tab(False)
        self._input.set_hexpand(True)
        self._input.add_css_class("translator-input")
        self._input.set_top_margin(0)
        self._input.set_bottom_margin(0)

        # 输入框随内容增长，超过上限后内部滚动
        self._input_scroller = Gtk.ScrolledWindow()
        # 垂直策略用 EXTERNAL 而不是 AUTOMATIC：实测 AUTOMATIC 会让 ScrolledWindow
        # 无论内容多高都固定多报 36px 的自然高度（一行文字也要 58px），
        # EXTERNAL 则不占滚动条空间，高度贴合内容，滚轮/键盘滚动照常工作。
        self._input_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        self._input_scroller.set_propagate_natural_height(True)
        self._input_scroller.set_max_content_height(INPUT_MAX_HEIGHT)
        self._input_scroller.set_child(self._input)
        self._input_scroller.add_css_class("translator-input-scroller")
        self._input_grows_with_content = True

        # TextView 没有占位符，用一个不吃事件的标签叠上去
        self._placeholder = Gtk.Label(label="输入要翻译的内容")
        self._placeholder.set_xalign(0)
        self._placeholder.set_yalign(0)
        self._placeholder.set_can_target(False)
        self._placeholder.add_css_class("translator-placeholder")

        input_overlay = Gtk.Overlay()
        input_overlay.set_hexpand(True)
        input_overlay.set_child(self._input_scroller)
        input_overlay.add_overlay(self._placeholder)

        self._buffer = self._input.get_buffer()
        self._buffer.connect("changed", self._on_buffer_changed)

        self._settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        self._settings_button.set_has_frame(False)
        self._settings_button.set_tooltip_text("设置")
        self._settings_button.connect("clicked", lambda _button: self._open_settings())

        icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        icon.set_valign(Gtk.Align.START)
        icon.set_margin_top(5)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.append(icon)
        header.append(input_overlay)
        header.append(self._settings_button)

        # 结果区的标题行：左侧是"译文 / 翻译中 / 错误"，加载时右侧跟着转圈。
        # 标题始终在最左边，因此状态切换时它不会横跳。
        self._result_heading_label = Gtk.Label(label="")
        self._result_heading_label.set_xalign(0)
        self._result_heading_label.add_css_class("caption")
        self._result_heading_label.add_css_class("dim-label")

        self._result_spinner = Gtk.Spinner()
        self._result_spinner.set_size_request(16, 16)
        self._result_spinner.set_valign(Gtk.Align.CENTER)
        self._result_spinner.set_visible(False)

        self._result_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._result_heading.append(self._result_heading_label)
        self._result_heading.append(self._result_spinner)

        self._result_text = Gtk.Label(label="")
        self._result_text.set_xalign(0)
        self._result_text.set_wrap(True)
        self._result_text.set_selectable(True)
        self._result_text.add_css_class("translator-result")

        self._result_detail = Gtk.Label(label="")
        self._result_detail.set_xalign(0)
        self._result_detail.set_wrap(True)
        self._result_detail.add_css_class("translator-detail")
        self._result_detail.add_css_class("dim-label")
        self._result_detail.set_visible(False)

        self._result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._result_box.append(self._result_heading)
        self._result_box.append(self._result_text)
        self._result_box.append(self._result_detail)
        self._result_box.set_visible(False)

        self._result_separator = Gtk.Separator()
        self._result_separator.set_visible(False)

        self._hint = Gtk.Label(label=self._hint_text())
        self._hint.set_xalign(0)
        self._hint.add_css_class("caption")
        self._hint.add_css_class("dim-label")

        surface = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        surface.add_css_class("translator-surface")
        surface.set_margin_top(SURFACE_MARGIN)
        surface.set_margin_bottom(SURFACE_MARGIN)
        surface.set_margin_start(SURFACE_MARGIN)
        surface.set_margin_end(SURFACE_MARGIN)
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
        try:
            return self._handle_key_pressed_capture(_controller, keyval, _state)
        except Exception:  # noqa: BLE001 - 守卫绝不能因为日志/诊断代码出错而失效
            log.exception("window: key handler failed, ignoring key")
            return False

    def _handle_key_pressed_capture(self, _controller, keyval, _state) -> bool:
        now = GLib.get_monotonic_time()
        if self._guard_active:
            if now >= self._guard_deadline_us:
                self._end_key_guard("timeout")
            elif keyval == self._trigger_keyval:
                # 触发键按下，一律吞掉：实测松开事件送不到窗口，GTK 会一直重复它；
                # 注意**不能**用 im_context_filter_keypress 来判断"输入法是否在组合"——
                # 它对空格恒返回 True，会把守卫直接放掉，导致泄漏的空格灌进输入框。
                if self._swallowed_keys == 0:
                    self._reset_input_method_state(_controller)
                self._swallowed_keys += 1
                return True
            else:
                # 用户已经开始正常输入，守卫解除
                self._end_key_guard("user-typing")

        # 走到这里说明是用户的真实按键，后续到达的剪贴板预填不应再覆盖输入
        self._user_edited = True

        if keyval == self._trigger_keyval and not self._guard_active:
            self._diagnose_trigger_press(_controller, _state)

        # Ctrl+C：必须放在捕获阶段——输入框（GtkTextView）会自己消费 Ctrl+C 做复制，
        # 冒泡阶段根本看不到。仅当焦点在输入框、且它没有选中文字时才解释为"复制译文"。
        if (
            keyval in (Gdk.KEY_c, Gdk.KEY_C)
            and _state & Gdk.ModifierType.CONTROL_MASK
            and not _state & Gdk.ModifierType.SHIFT_MASK
            and self._input.has_focus()
        ):
            return self._copy_result_if_appropriate()

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if _state & Gdk.ModifierType.SHIFT_MASK:
                return False  # Shift+Enter 交给输入框插入换行
            if self._im_wants_key(_controller):
                return True  # 输入法正在组合，这一下回车用于上屏/确认候选
            self._submit()
            return True
        return False

    def _diagnose_trigger_press(self, controller, state) -> None:
        """临时诊断：记录触发键事件的时间戳/修饰键，并观察输入是否随之变化。

        - 若 300ms 后字符数不变 → 输入法根本没处理这一按（被当成重复键忽略）；
        - 若字符数增加 → 输入法提交了内容，问题在别处。
        """
        event_time = "-"
        try:
            event = controller.get_current_event()
            if event is not None:
                event_time = event.get_time()
        except Exception:  # noqa: BLE001 - 诊断代码不影响主流程
            pass
        chars_before = self._buffer.get_char_count()

        def report() -> bool:
            log.debug(
                "window: TRIGGER press time=%s state=%s chars %d→%d",
                event_time, state, chars_before, self._buffer.get_char_count(),
            )
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(300, report)

    def _reset_input_method_state(self, controller) -> None:
        """热键的按下/松开被系统抓走后，输入法上下文会以为该键仍按着。

        GTK 为此提供了 ``reset_im_context()``（文档：当控件状态与输入法不一致时使用），
        在检测到泄漏的瞬间调用它，避免用户随后按下的那个键被输入法当成"自动重复"忽略。
        """
        try:
            # 只取时间戳做日志；GdkKeyEvent 在 GTK4 里没有 get_state()，
            # 这里凡是取不到的一律跳过，绝不能影响吞键逻辑。
            event = controller.get_current_event()
            log.debug(
                "window: first leaked trigger key (time=%s), resetting IM state",
                event.get_time() if event is not None else "-",
            )
            self._input.reset_im_context()
        except Exception as exc:  # noqa: BLE001 - 诊断/重置失败都不影响吞键
            log.debug("window: cannot reset input method state (%s)", exc)

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

    def _copy_result_if_appropriate(self) -> bool:
        """复制译文（规格 FR-RESULT-4/6）。返回 True 表示已消费此次 Ctrl+C。"""
        result = self._result_text.get_text()
        if not result or self._busy or not self._result_box.get_visible():
            return False
        if self._buffer.get_has_selection():
            # 用户在输入框里选了文字，交回系统默认的复制行为
            return False
        self._clipboard.write_text(result)
        if self._config.close_after_copy:
            self._hint.set_text("已复制")
            serial = self._present_serial
            GLib.timeout_add(COPIED_FEEDBACK_MS, lambda: self._dismiss_and_restore(serial))
        else:
            self._show_copied_feedback()
        return True

    def _dismiss_and_restore(self, serial: int) -> bool:
        self._refresh_hint()
        if serial != self._present_serial:
            # 这期间窗口被重新唤起过，别把新窗口也关掉
            return GLib.SOURCE_REMOVE
        self._dismiss()
        return GLib.SOURCE_REMOVE

    def _show_copied_feedback(self) -> None:
        self._hint.set_text("已复制")

        def restore() -> bool:
            self._refresh_hint()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(1200, restore)

    def _hint_text(self) -> str:
        """底部提示按当前状态生成：没有结果时不要说"Ctrl+C 复制"。"""
        parts = [HINT_SUBMIT, HINT_NEWLINE]
        if self._result_box.get_visible() and self._result_text.get_text():
            parts.append(HINT_COPY_CLOSE if self._config.close_after_copy else HINT_COPY)
        parts.append(HINT_CLOSE)
        return " · ".join(parts)

    def _refresh_hint(self) -> None:
        self._hint.set_text(self._hint_text())

    def _open_settings(self) -> None:
        self._dismiss()
        if self._on_open_settings is not None:
            self._on_open_settings()

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
        # 提交后收敛选区：否则"重新唤起时的全选"会一直留着，让随后的 Ctrl+C
        # 被误判成"用户在复制选中的文字"，而不去复制译文
        self._collapse_selection()

    def _collapse_selection(self) -> None:
        """光标移到末尾并清除选区（select_range 传同一个 iter 即零长度选区）。"""
        self._pending_select_all = False
        end = self._buffer.get_end_iter()
        self._buffer.select_range(end, end)

    # ------------------------------------------------------------------ 内容

    @property
    def text(self) -> str:
        return self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False
        )

    def set_text(self, text: str, *, select_all: bool = True) -> None:
        self._buffer.set_text(text)
        if select_all:
            self._select_all()

    def _select_all(self) -> None:
        """全选输入框内容：重新唤起后直接键入即可整体替换。"""
        if not self.text:
            return
        self._pending_select_all = True
        self._buffer.select_range(
            self._buffer.get_start_iter(), self._buffer.get_end_iter()
        )
        self._input.scroll_to_iter(self._buffer.get_start_iter(), 0.0, False, 0.0, 0.0)
        expected = self.text
        log.debug(
            "window: select-all applied (has_selection=%s)", self._buffer.get_has_selection()
        )
        # 实测：窗口呈现后焦点才真正落定，GTK 会在那之后把选择清掉（文本不变）。
        # 因此在焦点稳定后补若干次选中；一旦用户开始输入或文本被改动就立即收手。
        for delay_ms in (250, 600):
            GLib.timeout_add(delay_ms, lambda: self._reassert_selection(expected))

    def _reassert_selection(self, expected_text: str) -> bool:
        if self._user_edited or self.text != expected_text:
            return GLib.SOURCE_REMOVE
        if not self._buffer.get_has_selection():
            log.debug("window: re-asserting select-all after focus settled")
            self._buffer.select_range(
                self._buffer.get_start_iter(), self._buffer.get_end_iter()
            )
        return GLib.SOURCE_REMOVE

    def _clear_pending_select_all(self) -> bool:
        self._pending_select_all = False
        return GLib.SOURCE_REMOVE

    def _on_buffer_changed(self, buffer) -> None:
        count = buffer.get_char_count()
        self._placeholder.set_visible(count == 0)
        self._update_input_sizing(count)
        if not self._trimming_input:
            self._enforce_length_limit()

    def _update_input_sizing(self, char_count: int) -> None:
        """大文本时关掉高度自适应，避免整篇布局导致的卡顿。"""
        should_grow = char_count <= INPUT_AUTO_GROW_MAX_CHARS
        if should_grow == self._input_grows_with_content:
            return
        self._input_grows_with_content = should_grow
        self._input_scroller.set_propagate_natural_height(should_grow)
        if should_grow:
            self._input_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
            self._input_scroller.set_min_content_height(-1)
        else:
            # 固定高度：只布局可见区域，粘贴大段文本也不会卡；这时才需要真的滚动条
            self._input_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self._input_scroller.set_min_content_height(INPUT_MAX_HEIGHT)
        log.debug(
            "window: input sizing mode → %s (%d chars)",
            "auto-grow" if should_grow else "fixed-height",
            char_count,
        )

    def _enforce_length_limit(self) -> None:
        """输入一旦超过 DeepL 单次上限就就地截断：别让用户攒出一份发不出去的文本。

        按 UTF-8 字节精确裁剪，并用 errors="ignore" 解码，避免把多字节字符截成半个。
        """
        text = self.text
        if len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
            return
        trimmed = text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
        kbytes = MAX_TEXT_BYTES // 1024
        log.info(
            "window: input exceeded %d KiB, truncated to %d chars", kbytes, len(trimmed)
        )
        self._trimming_input = True
        try:
            self._buffer.set_text(trimmed)
            self._buffer.place_cursor(self._buffer.get_end_iter())
        finally:
            self._trimming_input = False
        self._hint.set_text(f"已达到上限（{kbytes} KiB），多余部分已被截断")
        GLib.timeout_add(2500, lambda: (self._refresh_hint(), False)[-1])

    # ------------------------------------------------------------------ 结果与状态

    @property
    def busy(self) -> bool:
        return self._busy

    def show_loading(self) -> None:
        self._busy = True
        self._result_text.remove_css_class("translator-error-text")
        self._result_text.set_text("")
        self._result_text.set_visible(False)
        self._result_detail.set_visible(False)
        self._set_result_heading("翻译中", spinner=True)
        self._show_result_area(True)

    def show_result(self, text: str) -> None:
        self._busy = False
        self._result_text.remove_css_class("translator-error-text")
        self._result_text.set_text(text)
        self._result_text.set_visible(True)
        self._result_detail.set_visible(False)
        self._set_result_heading("译文", spinner=False)
        self._show_result_area(True)

    def show_error(self, error: TranslationError) -> None:
        self._busy = False
        main, detail = error.user_message
        if not main:
            self.clear_result()
            return
        self._result_text.add_css_class("translator-error-text")
        self._result_text.set_text(main)
        self._result_text.set_visible(True)
        self._result_detail.set_text(detail)
        self._result_detail.set_visible(bool(detail))
        self._set_result_heading("错误", spinner=False)
        self._show_result_area(True)

    def clear_result(self) -> None:
        self._busy = False
        self._result_text.set_text("")
        self._result_detail.set_text("")
        self._set_result_heading("", spinner=False)
        self._show_result_area(False)

    def _set_result_heading(self, text: str, *, spinner: bool) -> None:
        self._result_heading_label.set_text(text)
        self._result_heading_label.set_visible(bool(text))
        self._result_spinner.set_visible(spinner)
        if spinner:
            self._result_spinner.start()
        else:
            self._result_spinner.stop()

    def _show_result_area(self, visible: bool) -> None:
        self._result_separator.set_visible(visible)
        self._result_box.set_visible(visible)
        self._refresh_hint()

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
            # 内容不变就保留用户的编辑，但仍然全选，方便直接键入替换
            self._select_all()
            return
        # 没有文本 → 清空输入框（规格 §8）
        self._last_clipboard_text = text
        self.set_text(text or "")
        log.debug("clipboard: prefill applied (has_text=%s)", bool(text))

    # ------------------------------------------------------------------ 失焦

    def _on_active_changed(self, *_args) -> None:
        if self.is_active():
            # 焦点真正落定后再补一次全选（GTK 会在焦点切换时清掉之前的选择）
            if self._pending_select_all and not self._user_edited:
                self._buffer.select_range(
                    self._buffer.get_start_iter(), self._buffer.get_end_iter()
                )
            return
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
        """唤起窗口并确保输入框拿到焦点（后续步骤才能读剪贴板）。

        ``activation_token`` 目前**刻意不使用**：实测把它交给合成器
        （``Gdk.Toplevel.set_startup_id()``）之后，由快捷键激活的窗口会把触发按键
        一并回放给应用，导致输入法把用户随后的第一次空格当成自动重复而忽略。
        不带 token 呈现（等价于 ``--toggle`` 路径）则一切正常。
        """
        _ = activation_token
        self._last_present_us = GLib.get_monotonic_time()
        self._present_serial += 1
        if arm_key_guard:
            self._guard_active = True
            self._swallowed_keys = 0
            self._guard_deadline_us = self._last_present_us + TRIGGER_KEY_GUARD_MAX_US
        self.present()
        self._input.grab_focus()
        if not self._busy:
            # 上一次的翻译结果不属于这次唤起
            self.clear_result()
        # 这里刻意不动输入框内容与选区：剪贴板若变了，读回来时会整体覆盖并全选；
        # 若没变，则保留用户的编辑与光标位置。
        self._start_clipboard_prefill()
        # 只有呈现后的一小段时间内才允许反复补选，避免长期干扰用户的光标操作
        GLib.timeout_add(1500, self._clear_pending_select_all)
        log.debug("window: presented")
