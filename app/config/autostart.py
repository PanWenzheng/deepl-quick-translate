"""开机自启动：写/删 XDG autostart 文件（规格 §4.11 / FR-SET-2）。

只在用户于设置页显式开启时才创建，不做"悄悄开机启动"。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ..constants import APP_ID, APP_NAME, BINARY_NAME

log = logging.getLogger(__name__)

AUTOSTART_TEMPLATE = """[Desktop Entry]
Type=Application
Name={name}
Comment=DeepL 快捷翻译（后台常驻）
Exec={command}
Icon={app_id}
Terminal=false
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""


def autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "autostart"


def autostart_path() -> Path:
    return autostart_dir() / f"{APP_ID}.desktop"


def launch_command(*extra_args: str) -> str:
    """优先用 PATH 里的正式二进制；开发目录里退回仓库的 run.sh。"""
    suffix = " ".join(extra_args)
    installed = shutil.which(BINARY_NAME)
    if installed:
        return f"{installed} {suffix}".strip()
    dev_entry = Path(__file__).resolve().parents[2] / "run.sh"
    if dev_entry.exists():
        return f"{dev_entry} {suffix}".strip()
    return f"{BINARY_NAME} {suffix}".strip()


def set_enabled(enabled: bool) -> bool:
    """开启/关闭开机自启动。返回是否操作成功。"""
    path = autostart_path()
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                AUTOSTART_TEMPLATE.format(
                    name=APP_NAME,
                    command=launch_command("--background"),
                    app_id=APP_ID,
                ),
                encoding="utf-8",
            )
            log.info("autostart: enabled (%s)", path)
        elif path.exists():
            path.unlink()
            log.info("autostart: disabled (%s)", path)
    except OSError as exc:
        log.warning("autostart: cannot %s (%s)", "enable" if enabled else "disable", exc)
        return False
    return True


def is_enabled() -> bool:
    return autostart_path().exists()


def refresh_if_needed() -> bool:
    """已启用时，若 Exec 指向的不是当前解析出的命令就重写。

    典型场景：先在开发目录用 ``run.sh`` 开启自启动，后来装了 .deb，此时应当把
    Exec 换成 ``/usr/bin/deepl-quick-translate``，否则自启动会一直跑旧路径。
    """
    path = autostart_path()
    if not path.exists():
        return False
    expected = launch_command("--background")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if f"Exec={expected}\n" in content:
        return False
    log.info("autostart: Exec changed, rewriting for %s", expected)
    return set_enabled(True)
