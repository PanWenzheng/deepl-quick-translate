"""快捷键字符串在两种语法间的转换。

* 内部/GTK 语法：``<Control><Alt>space``（GTK accelerator 格式，也是 GSettings 要求的格式）
* 门户语法：``CTRL+ALT+space``（XDG shortcuts 规范，修饰键取自 xkbcommon-names，按键名取自 xkbcommon-keysyms 并去掉 ``XKB_KEY_`` 前缀）
"""

from __future__ import annotations

GTK_TO_XDG_MODIFIERS = {
    "<Control>": "CTRL",
    "<Primary>": "CTRL",
    "<Ctrl>": "CTRL",
    "<Alt>": "ALT",
    "<Mod1>": "ALT",
    "<Shift>": "SHIFT",
    "<Super>": "LOGO",
    "<Meta>": "LOGO",
}

XDG_TO_DISPLAY_MODIFIERS = {
    "CTRL": "Ctrl",
    "ALT": "Alt",
    "SHIFT": "Shift",
    "LOGO": "Super",
    "NUM": "Num",
}

XDG_MODIFIER_ORDER = ["CTRL", "ALT", "SHIFT", "LOGO", "NUM"]


def _split_gtk_accel(accel: str) -> tuple[list[str], str]:
    """把 GTK accelerator 拆成（修饰键列表, 键名）。"""
    rest = accel.strip()
    modifiers: list[str] = []
    while rest.startswith("<"):
        end = rest.find(">")
        if end == -1:
            break
        modifiers.append(rest[: end + 1])
        rest = rest[end + 1 :]
    return modifiers, rest


def gtk_accel_to_xdg(accel: str) -> str:
    """``<Control><Alt>space`` → ``CTRL+ALT+space``。"""
    modifiers, key = _split_gtk_accel(accel)
    parts = [GTK_TO_XDG_MODIFIERS.get(mod, mod.strip("<>").upper()) for mod in modifiers]
    parts.append(key or "space")
    return "+".join(parts)


def gtk_accel_to_display(accel: str) -> str:
    """``<Control><Alt>space`` → ``Ctrl+Alt+Space``（用于界面展示）。"""
    modifiers, key = _split_gtk_accel(accel)
    parts = [GTK_TO_XDG_MODIFIERS.get(mod, mod.strip("<>").upper()) for mod in modifiers]
    parts = [XDG_TO_DISPLAY_MODIFIERS.get(part, part) for part in parts]
    if key:
        parts.append(key.capitalize() if len(key) > 1 else key.upper())
    return "+".join(parts)


def is_valid_gtk_accel(accel: str) -> bool:
    """粗略校验：至少有一个非修饰键（GTK 更严格的校验留给 Gtk.accelerator_parse）。"""
    if not isinstance(accel, str) or not accel.strip():
        return False
    _, key = _split_gtk_accel(accel)
    return bool(key)
