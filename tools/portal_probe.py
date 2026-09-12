#!/usr/bin/env python3
"""门户 GlobalShortcuts 诊断脚本。

用法：python3 tools/portal_probe.py [--seconds 20]

逐个阶段打印门户交互过程（宿主注册 → CreateSession → BindShortcuts → Activated 信号），
用于排查"快捷键注册卡住 / 无响应"这类问题。

绑定成功后会继续运行到超时，期间按下的快捷键会打印出来；同时挂一个不做 sender
过滤的订阅做对照，用于区分"信号没到"和"订阅写错"。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gi  # noqa: E402

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from gi.repository import Gio  # noqa: E402

from app.constants import APP_ID, DEFAULT_SHORTCUT_ACCEL, SHORTCUT_DESCRIPTION, SHORTCUT_ID  # noqa: E402
from app.shortcuts.accels import gtk_accel_to_xdg  # noqa: E402
from app.shortcuts.portal import GLOBAL_SHORTCUTS_IFACE, PortalGlobalShortcuts  # noqa: E402


def _trace(message: str) -> None:
    """直写 fd 2：绕开 logging，避免被日志级别或缓冲影响。"""
    import os

    os.write(2, f"[probe] {message}\n".encode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("probe")

    portal = PortalGlobalShortcuts(APP_ID)
    loop = GLib.MainLoop()

    # 对照组：不带 sender 过滤的订阅
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def on_any_activated(_conn, _sender, path, _iface, _signal, params, *_rest):
        _trace(f"raw subscription fired: path={path} params={params.print_(True)}")

    conn.signal_subscribe(
        None, GLOBAL_SHORTCUTS_IFACE, "Activated", None, None,
        Gio.DBusSignalFlags.NONE, on_any_activated, None,
    )
    _trace("raw subscription installed (no sender filter)")

    def on_activated(shortcut_id: str, timestamp: int) -> None:
        _trace(f"portal client callback: shortcut_id={shortcut_id} timestamp={timestamp}")

    def on_result(error) -> None:
        if error is not None:
            log.error("RESULT error: %r", error)
            loop.quit()
        else:
            _trace("binding ok; 等待快捷键按下（Ctrl+Alt+Space）...")

    log.info("calling register() ...")
    portal.register(
        shortcut_id=SHORTCUT_ID,
        description=SHORTCUT_DESCRIPTION,
        preferred_trigger=gtk_accel_to_xdg(DEFAULT_SHORTCUT_ACCEL),
        on_activated=on_activated,
        on_result=on_result,
    )

    def on_timeout() -> bool:
        log.warning("TIMEOUT after %.0fs: 门户没有在预期时间内给出响应", args.seconds)
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.timeout_add_seconds(int(args.seconds), on_timeout)
    loop.run()
    portal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
