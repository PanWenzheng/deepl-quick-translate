"""快捷键注册与注销：门户优先，GSettings 兜底。

两条路径的取舍：

* **门户（默认）**：标准做法，不需要写用户的 dconf；代价是首次注册需要用户在系统
  弹窗里按一次键，且要求系统里存在 ``<app_id>.desktop``（见规格附录 C）。
* **GSettings**：静默生效、可精确绑定任意组合键；代价是会修改用户的 dconf，因此只在
  门户不可用或被拒时使用。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from ..config.manager import Config
from ..constants import (
    BINARY_NAME,
    GSETTINGS_TOGGLE_COMMAND,
    SHORTCUT_DESCRIPTION,
    SHORTCUT_ID,
)
from .accels import gtk_accel_to_display, gtk_accel_to_xdg
from .gsettings_backend import GSettingsShortcuts, GSettingsUnavailable
from .portal import PortalGlobalShortcuts

log = logging.getLogger(__name__)


def resolve_toggle_command(configured: str) -> str:
    """决定 GSettings 触发时执行的命令。

    用户显式配置过就照用；否则优先用 PATH 里的正式二进制，在开发目录里则退回到
    仓库中的 ``run.sh``，这样未安装 .deb 时兜底路径也能真正把窗口唤起来。
    """
    if configured != GSETTINGS_TOGGLE_COMMAND:
        return configured
    if shutil.which(BINARY_NAME):
        return configured
    dev_entry = Path(__file__).resolve().parents[2] / "run.sh"
    if dev_entry.exists():
        log.info("gsettings: using development entry point %s", dev_entry)
        return f"{dev_entry} --toggle"
    return configured


class ShortcutManager:
    """对外只暴露 register / unregister 两个动作。"""

    def __init__(
        self,
        app_id: str,
        config: Config,
        on_activated: Callable[[str, int], None],
        *,
        on_config_changed: Callable[[Config], None] | None = None,
    ) -> None:
        self._app_id = app_id
        self._config = config
        self._on_activated = on_activated
        self._on_config_changed = on_config_changed
        self._portal: PortalGlobalShortcuts | None = None
        self._gsettings: GSettingsShortcuts | None = None
        self.backend_in_use: str | None = None

    # ------------------------------------------------------------------ 公共 API

    def register(self, on_status: Callable[[bool, str | None], None] | None = None) -> None:
        """异步注册。``on_status(ok, error_message)`` 只在流程结束时回调一次。"""
        log.debug("shortcut backend requested: %s", self._config.shortcut.backend)
        if self._config.shortcut.backend == "gsettings":
            self._register_gsettings(on_status)
            return
        self._register_portal(on_status)

    def unregister(self) -> None:
        """注销快捷键并清理门户会话 / dconf 条目。退出时调用。"""
        if self._portal is not None:
            self._portal.close()
            self._portal = None

        path = self._config.shortcut.gsettings_path
        if self._gsettings is not None and path:
            try:
                self._gsettings.unbind(path)
                log.debug("released custom keybinding %s", path)
            except (GSettingsUnavailable, GLib.Error) as exc:
                log.warning("failed to remove custom keybinding: %s", exc)
            # 绑定已经撤掉，配置里同步清空，避免下次启动拿着失效路径去操作
            self._config.shortcut.gsettings_path = None
            if self._on_config_changed is not None:
                self._on_config_changed(self._config)

        self.backend_in_use = None

    # ------------------------------------------------------------------ 门户

    def _register_portal(self, on_status: Callable[[bool, str | None], None] | None) -> None:
        try:
            self._portal = PortalGlobalShortcuts(self._app_id)
        except GLib.Error as exc:
            log.warning("portal unavailable (%s); falling back to GSettings", exc.message)
            self._register_gsettings(on_status, reason="portal unavailable")
            return

        def on_result(error: Exception | None) -> None:
            if error is not None:
                log.warning("portal binding failed (%s); falling back to GSettings", error)
                self._register_gsettings(on_status, reason=str(error))
                return
            self.backend_in_use = "portal"
            self._drop_gsettings_binding()
            log.info(
                "global shortcut registered via portal: %s",
                gtk_accel_to_display(self._config.shortcut.preferred_trigger),
            )
            if on_status is not None:
                on_status(True, None)

        self._portal.register(
            shortcut_id=SHORTCUT_ID,
            description=SHORTCUT_DESCRIPTION,
            preferred_trigger=gtk_accel_to_xdg(self._config.shortcut.preferred_trigger),
            on_activated=self._on_activated,
            on_result=on_result,
        )

    def _drop_gsettings_binding(self) -> None:
        """门户注册成功时，清理之前可能留下的 dconf 兜底绑定，避免重复注册。"""
        path = self._config.shortcut.gsettings_path
        if not path:
            return
        try:
            backend = self._gsettings or GSettingsShortcuts()
            backend.unbind(path)
            self._gsettings = None
            log.info("removed leftover GSettings binding %s", path)
        except (GSettingsUnavailable, GLib.Error) as exc:
            log.warning("cannot remove GSettings binding %s: %s", path, exc)
        self._config.shortcut.gsettings_path = None
        if self._on_config_changed is not None:
            self._on_config_changed(self._config)

    # ------------------------------------------------------------------ GSettings 兜底

    def _register_gsettings(
        self,
        on_status: Callable[[bool, str | None], None] | None,
        *,
        reason: str | None = None,
    ) -> None:
        accel = self._config.shortcut.preferred_trigger
        try:
            backend = GSettingsShortcuts()
        except (GSettingsUnavailable, GLib.Error) as exc:
            message = f"shortcut unavailable: {exc}"
            log.error("%s", message)
            if on_status is not None:
                on_status(False, message)
            return

        conflicts = backend.find_conflicts(accel)
        if conflicts:
            log.warning("shortcut %s is also used by: %s", accel, ", ".join(conflicts))

        try:
            previous = self._config.shortcut.gsettings_path
            if previous:
                backend.unbind(previous)
            path = backend.bind(
                accel=accel,
                command=resolve_toggle_command(self._config.shortcut.command),
                name=SHORTCUT_DESCRIPTION,
            )
        except (GSettingsUnavailable, GLib.Error) as exc:
            message = f"shortcut unavailable: {exc}"
            log.error("%s", message)
            if on_status is not None:
                on_status(False, message)
            return

        self._gsettings = backend
        self._config.shortcut.gsettings_path = path
        self.backend_in_use = "gsettings"
        if self._on_config_changed is not None:
            self._on_config_changed(self._config)
        log.info(
            "global shortcut registered via gsettings: %s%s",
            gtk_accel_to_display(accel),
            f" (fallback: {reason})" if reason else "",
        )
        if on_status is not None:
            on_status(True, None)
