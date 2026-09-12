"""GSettings 兜底后端：写入 GNOME 的自定义快捷键。

仅在门户不可用或用户拒绝授权时使用。缺点是会修改用户的 dconf，因此设置页需要
明确告知；优点是能静默绑定任意组合键，不弹系统窗口。
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_BINDINGS_KEY = "custom-keybindings"
CUSTOM_BINDING_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
CUSTOM_BINDING_BASE_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
)
MAX_CUSTOM_SLOTS = 32

# 冲突检测用：这些 schema 里的已有绑定若与我们相同，说明快捷键已被占用
CONFLICT_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
)

log = logging.getLogger(__name__)


class GSettingsUnavailable(RuntimeError):
    """GSettings / dconf 不可用。"""


def _string_list(settings: Gio.Settings, key: str) -> list[str]:
    value = settings.get_value(key)
    if value is None:
        return []
    return list(value.unpack())


class GSettingsShortcuts:
    """管理 ``custom-keybindings`` 下的一个条目。"""

    def __init__(self) -> None:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup(CUSTOM_BINDING_SCHEMA, True) is None:
            raise GSettingsUnavailable("custom-keybinding schema is not installed")
        self._settings = Gio.Settings.new(MEDIA_KEYS_SCHEMA)

    def bind(self, *, accel: str, command: str, name: str) -> str:
        """写入一条自定义快捷键，返回它的 GSettings 路径。"""
        current = _string_list(self._settings, CUSTOM_BINDINGS_KEY)
        path = self._pick_slot(current)
        entry = Gio.Settings.new_with_path(CUSTOM_BINDING_SCHEMA, path)
        entry.set_string("name", name)
        entry.set_string("command", command)
        entry.set_string("binding", accel)
        Gio.Settings.sync()

        if path not in current:
            current.append(path)
            self._settings.set_value(CUSTOM_BINDINGS_KEY, GLib.Variant("as", current))
            Gio.Settings.sync()
        log.debug("gsettings: bound %s to %s (%s)", accel, command, path)
        return path

    def unbind(self, path: str) -> None:
        """移除指定的自定义快捷键条目。"""
        current = _string_list(self._settings, CUSTOM_BINDINGS_KEY)
        if path in current:
            current = [item for item in current if item != path]
            self._settings.set_value(CUSTOM_BINDINGS_KEY, GLib.Variant("as", current))
        entry = Gio.Settings.new_with_path(CUSTOM_BINDING_SCHEMA, path)
        for key in ("name", "command", "binding"):
            entry.reset(key)
        Gio.Settings.sync()
        log.debug("gsettings: unbound %s", path)

    def find_conflicts(self, accel: str) -> list[str]:
        """返回占用了同一组合键的已有绑定，形如 ``schema::key``。"""
        conflicts: list[str] = []

        for schema_id in CONFLICT_SCHEMAS:
            try:
                settings = Gio.Settings.new(schema_id)
                schema = settings.props.settings_schema
                keys = schema.list_keys() if schema is not None else []
            except (TypeError, GLib.Error) as exc:
                log.debug("gsettings: cannot inspect %s (%s)", schema_id, exc)
                continue

            for key in keys:
                try:
                    value = settings.get_value(key)
                except GLib.Error:
                    continue
                if value is None:
                    continue
                if value.get_type_string() == "s" and value.unpack() == accel:
                    conflicts.append(f"{schema_id}::{key}")
                elif value.get_type_string() == "as" and accel in value.unpack():
                    conflicts.append(f"{schema_id}::{key}")

        # 自定义快捷键里的重复项
        for path in _string_list(self._settings, CUSTOM_BINDINGS_KEY):
            try:
                entry = Gio.Settings.new_with_path(CUSTOM_BINDING_SCHEMA, path)
                if entry.get_string("binding") == accel:
                    conflicts.append(path)
            except GLib.Error:
                continue
        return conflicts

    def _pick_slot(self, current: list[str]) -> str:
        """找一个未被占用的 customN 槽位。"""
        for index in range(MAX_CUSTOM_SLOTS):
            candidate = f"{CUSTOM_BINDING_BASE_PATH}custom{index}/"
            if candidate not in current:
                return candidate
        raise GSettingsUnavailable("no free custom keybinding slot")
