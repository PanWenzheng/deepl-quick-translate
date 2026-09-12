"""设置窗口（libadwaita 偏好页，规格 §4.10）。"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Adw, Gdk, GObject, Gtk

from ..config import autostart
from ..config.manager import Config
from ..config.secret import SecretManager, mask
from ..constants import APP_ID, ENDPOINT_FREE, ENDPOINT_PRO
from ..deepl.errors import TranslationError
from ..shortcuts.accels import gtk_accel_to_display

log = logging.getLogger(__name__)

ENDPOINT_CUSTOM = "custom"

# 这些键名本身就是修饰键，不能当作"按键本体"
MODIFIER_KEY_NAMES = frozenset(
    {
        "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R",
        "Shift_L", "Shift_R", "Meta_L", "Meta_R", "Hyper_L", "Hyper_R",
        "Caps_Lock", "Num_Lock", "ISO_Level3_Shift",
    }
)


class SettingsWindow(Adw.PreferencesWindow):
    """General + DeepL 两页，V1 不再加别的配置。"""

    def __init__(
        self,
        application: Gtk.Application,
        *,
        config: Config,
        secrets: SecretManager,
        on_config_changed: Callable[[], None],
        on_shortcut_changed: Callable[[], None],
        check_connection: Callable[[Callable[[dict | None, Exception | None], None]], None],
    ) -> None:
        super().__init__(application=application, title="设置")
        self._config = config
        self._secrets = secrets
        self._on_config_changed = on_config_changed
        self._on_shortcut_changed = on_shortcut_changed
        self._check_connection = check_connection

        self.set_search_enabled(False)
        self.set_default_size(520, 560)
        # 关闭即隐藏：实例复用，避免关闭后引用到已销毁的窗口
        self.set_hide_on_close(True)
        self.add(self._build_general_page())
        self.add(self._build_deepl_page())

    # ------------------------------------------------------------------ General

    def _build_general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="通用", icon_name="preferences-system-symbolic")
        group = Adw.PreferencesGroup(
            title="快捷键",
            description="快捷键写在 GNOME 的自定义快捷键里，可在系统设置中查看。",
        )

        self._shortcut_row = Adw.ActionRow(
            title="全局快捷键",
            subtitle=gtk_accel_to_display(self._config.shortcut.preferred_trigger),
        )
        change_button = Gtk.Button(label="更改")
        change_button.set_valign(Gtk.Align.CENTER)
        change_button.connect("clicked", self._on_change_shortcut)
        self._shortcut_row.add_suffix(change_button)
        group.add(self._shortcut_row)
        page.add(group)

        behaviour = Adw.PreferencesGroup(title="行为")
        self._autostart_row = Adw.SwitchRow(
            title="开机自动启动",
            subtitle="登录后后台常驻，不打开窗口",
            active=autostart.is_enabled(),
        )
        self._autostart_row.connect("notify::active", self._on_autostart_toggled)
        behaviour.add(self._autostart_row)

        self._close_after_copy_row = Adw.SwitchRow(
            title="复制后关闭窗口",
            subtitle="按 Ctrl+C 复制译文后自动关闭",
            active=self._config.close_after_copy,
        )
        self._close_after_copy_row.connect("notify::active", self._on_close_after_copy_toggled)
        behaviour.add(self._close_after_copy_row)

        self._hide_on_focus_loss_row = Adw.SwitchRow(
            title="失去焦点时隐藏",
            subtitle="切到别的窗口、或打开桌面/Dock 菜单时自动收起窗口",
            active=self._config.hide_on_focus_loss,
        )
        self._hide_on_focus_loss_row.connect(
            "notify::active", self._on_hide_on_focus_loss_toggled
        )
        behaviour.add(self._hide_on_focus_loss_row)
        page.add(behaviour)
        return page

    def _on_change_shortcut(self, _button: Gtk.Button) -> None:
        dialog = _ShortcutCaptureDialog(self, gtk_accel_to_display(self._config.shortcut.preferred_trigger))
        dialog.connect("captured", self._on_shortcut_captured)
        dialog.present()

    def _on_shortcut_captured(self, _dialog, accel: str) -> None:
        self._config.shortcut.preferred_trigger = accel
        self._shortcut_row.set_subtitle(gtk_accel_to_display(accel))
        self._on_shortcut_changed()

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _param) -> None:
        enabled = row.get_active()
        if autostart.set_enabled(enabled):
            self._config.start_on_login = enabled
            self._on_config_changed()

    def _on_close_after_copy_toggled(self, row: Adw.SwitchRow, _param) -> None:
        self._config.close_after_copy = row.get_active()
        self._on_config_changed()

    def _on_hide_on_focus_loss_toggled(self, row: Adw.SwitchRow, _param) -> None:
        self._config.hide_on_focus_loss = row.get_active()
        self._on_config_changed()

    # ------------------------------------------------------------------ DeepL

    def _build_deepl_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="DeepL", icon_name="translate-symbolic")
        group = Adw.PreferencesGroup(title="账户")

        configured, masked, source = self._secrets.status()
        self._api_key_row = Adw.PasswordEntryRow(title="API Key")
        self._api_key_row.set_show_apply_button(True)
        self._api_key_row.connect("apply", self._on_api_key_applied)
        group.add(self._api_key_row)

        # 输入框刻意留空：里面不放任何 Key 内容，点眼睛也无从泄漏；
        # 掩码单独用一行展示（规格 FR-SET-4）。
        self._key_status_row = Adw.ActionRow(title="已保存的 Key")
        self._key_status_row.set_sensitive(False)
        group.add(self._key_status_row)
        self._update_key_status()

        self._endpoint_row = Adw.ComboRow(
            title="Endpoint",
            model=Gtk.StringList.new(["DeepL API Free", "DeepL API Pro", "自定义"]),
        )
        self._endpoint_row.set_selected(self._endpoint_index())
        self._endpoint_row.set_subtitle(self._config.endpoint)
        self._endpoint_row.connect("notify::selected", self._on_endpoint_changed)
        group.add(self._endpoint_row)

        self._custom_endpoint_row = Adw.EntryRow(title="自定义 Endpoint")
        self._custom_endpoint_row.set_text(self._config.endpoint)
        self._custom_endpoint_row.set_show_apply_button(True)
        self._custom_endpoint_row.set_visible(self._endpoint_row.get_selected() == 2)
        self._custom_endpoint_row.connect("apply", self._on_custom_endpoint_applied)
        # 光靠"应用"按钮/回车容易漏：输入过程中直接生效，但只在自定义模式下
        self._custom_endpoint_row.connect("changed", self._on_custom_endpoint_changed)
        group.add(self._custom_endpoint_row)
        page.add(group)

        test_group = Adw.PreferencesGroup(title="连接")
        self._test_row = Adw.ActionRow(title="测试连接", subtitle="不会消耗翻译额度")
        self._test_button = Gtk.Button(label="测试")
        self._test_button.set_valign(Gtk.Align.CENTER)
        self._test_button.connect("clicked", self._on_test_connection)
        self._test_row.add_suffix(self._test_button)
        test_group.add(self._test_row)
        page.add(test_group)
        return page

    def _endpoint_index(self) -> int:
        if self._config.endpoint == ENDPOINT_FREE:
            return 0
        if self._config.endpoint == ENDPOINT_PRO:
            return 1
        return 2

    def _on_api_key_applied(self, row: Adw.PasswordEntryRow) -> None:
        value = row.get_text().strip()
        if not value:
            # 没输入内容就是"不改动"，避免误删已保存的 Key
            return
        if self._secrets.store(value):
            row.set_text("")
            self._update_key_status()
            self._test_row.set_subtitle("已保存 API Key，可测试连接")
        else:
            self._test_row.set_subtitle("保存失败：系统密钥环不可用")

    def _update_key_status(self) -> None:
        configured, masked, source = self._secrets.status()
        if not configured:
            self._key_status_row.set_subtitle("未配置")
            return
        origin = "系统密钥环" if source == "keyring" else "环境变量 DEEPL_API_KEY"
        self._key_status_row.set_subtitle(f"{masked}　（来源：{origin}）")

    def _on_endpoint_changed(self, row: Adw.ComboRow, _param) -> None:
        selected = row.get_selected()
        self._custom_endpoint_row.set_visible(selected == 2)
        if selected == 0:
            self._config.endpoint = ENDPOINT_FREE
        elif selected == 1:
            self._config.endpoint = ENDPOINT_PRO
        else:
            # 切到"自定义"时立刻按输入框里的当前文本生效：否则文本框内容没变就
            # 不会触发 changed，界面显示自定义 URL 而实际仍在用 Free/Pro 端点
            self._on_custom_endpoint_changed(self._custom_endpoint_row)
            return
        self._endpoint_row.set_subtitle(self._config.endpoint)
        self._on_config_changed()

    def _on_custom_endpoint_applied(self, row: Adw.EntryRow) -> None:
        value = row.get_text().strip().rstrip("/")
        if not value:
            return
        self._config.endpoint = value
        self._endpoint_row.set_subtitle(value)
        self._on_config_changed()

    def _on_custom_endpoint_changed(self, row: Adw.EntryRow) -> None:
        """只在用户确实选了"自定义"时才写入，避免覆盖 Free/Pro 预设。"""
        if self._endpoint_row.get_selected() != 2:
            return
        value = row.get_text().strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            return
        if value == self._config.endpoint:
            return
        self._config.endpoint = value
        self._endpoint_row.set_subtitle(value)
        self._on_config_changed()

    def _on_test_connection(self, _button: Gtk.Button) -> None:
        self._test_button.set_sensitive(False)
        self._test_row.set_subtitle("正在连接…")

        def done(_result, error: Exception | None) -> None:
            self._test_button.set_sensitive(True)
            if error is None:
                self._test_row.set_subtitle("Connection successful")
                return
            if isinstance(error, TranslationError):
                _main, detail = error.user_message
                self._test_row.set_subtitle(f"Connection failed — {detail}")
            else:
                self._test_row.set_subtitle("Connection failed")
            log.debug("settings: test connection failed: %s", error)

        self._check_connection(done)


class _ShortcutCaptureDialog(Gtk.Window):
    """让用户直接按下新的组合键。"""

    __gsignals__ = {
        "captured": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, parent: Gtk.Window, current: str) -> None:
        super().__init__(transient_for=parent, modal=True, title="更改快捷键")
        self.set_default_size(360, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        # GTK4 的按键事件按"焦点控件"投递：窗口里只有标签时焦点控件为 None，
        # 控制器永远收不到按键。所以必须自己造一个可聚焦的接收者。
        box.set_focusable(True)

        title = Gtk.Label(label="请按下新的组合键")
        title.add_css_class("title-4")
        self._hint = Gtk.Label(
            label=(
                f"当前：{current}　（Esc 取消）\n"
                "按住 Ctrl / Alt / Super 中的至少一个，再按一个普通键。\n"
                "注意：含 Super 的组合常被系统占用（如 Super+Space 切换输入法），"
                "那种情况不会传到本窗口，建议优先用 Ctrl+Alt+…"
            )
        )
        self._hint.add_css_class("dim-label")
        self._hint.set_wrap(True)
        box.append(title)
        box.append(self._hint)
        self.set_child(box)
        self._box = box

        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)
        self.connect("map", lambda *_: box.grab_focus())

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        try:
            return self._capture_key(keyval, state)
        except Exception:  # noqa: BLE001 - 捕获失败时吞掉按键，避免漏到别处
            log.exception("settings: shortcut capture failed")
            return True

    def _capture_key(self, keyval: int, state: Gdk.ModifierType) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        mods = state & Gtk.accelerator_get_default_mod_mask()
        accel = Gtk.accelerator_name(keyval, mods)
        # GTK 加速键语法形如 <Control><Alt>t：修饰键在尖括号内，后面才是按键本体。
        # 只按修饰键时 accel 以 ">" 结尾（例如 "<Control>"），此时不构成有效组合。
        last_gt = accel.rfind(">") if accel else -1
        key_part = accel[last_gt + 1 :] if last_gt != -1 else ""
        if not accel or last_gt == -1 or not key_part or key_part in MODIFIER_KEY_NAMES:
            held = " + ".join(
                name
                for mask, name in (
                    (Gdk.ModifierType.CONTROL_MASK, "Ctrl"),
                    (Gdk.ModifierType.ALT_MASK, "Alt"),
                    (Gdk.ModifierType.SUPER_MASK, "Super"),
                    (Gdk.ModifierType.SHIFT_MASK, "Shift"),
                )
                if mods & mask
            ) or "未按住修饰键"
            log.debug("settings: modifier-only press (%s), waiting for a normal key", held)
            self._hint.set_text(f"已按住：{held}　再按一个普通键（Esc 取消）")
            return True
        log.info("settings: new shortcut captured (%s)", accel)
        self.emit("captured", accel)
        self.close()
        return True
