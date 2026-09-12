"""XDG Desktop Portal ``GlobalShortcuts`` 后端。

流程：``CreateSession`` → ``BindShortcuts`` → 订阅会话对象上的 ``Activated`` 信号。

两条来自实测的硬性约束（见规格附录 C）：

1. **全异步**。在已经跑起来的主循环里调用同步 D-Bus API（``bus_get_sync`` /
   ``call_sync``）会自我等待派发而卡死主线程，因此这里的连接获取与所有方法调用
   一律使用异步形式。
2. **必须有应用身份**。xdg-desktop-portal 通过 ``<app_id>.desktop`` 确认调用方身份，
   宿主注册失败会导致后续 ``CreateSession`` 被拒（``An app id is required``）。

GNOME 的门户实现忽略 ``preferred_trigger``，改为弹窗让用户手动按键；实测该确认
**每个应用只需一次**，GNOME 会按 app id 记住绑定，之后启动静默恢复。
GlobalShortcuts 协议中并不存在 restore_token 之类的持久化字段。
"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Gio, GLib

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
GLOBAL_SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"
HOST_REGISTRY_IFACE = "org.freedesktop.host.portal.Registry"

RESPONSE_SUCCESS = 0
CALL_TIMEOUT_MS = 5000

log = logging.getLogger(__name__)


class ShortcutUnavailable(RuntimeError):
    """门户不可用、用户取消或注册被拒绝。"""


class PortalGlobalShortcuts:
    """门户 GlobalShortcuts 的异步客户端。"""

    def __init__(self, app_id: str) -> None:
        self._app_id = app_id
        self._token_prefix = "".join(
            char.lower() if char.isalnum() else "_" for char in app_id
        )
        self._conn: Gio.DBusConnection | None = None
        self._counter = 0
        self._pending: dict[str, tuple[int, Callable[[int, dict], None]]] = {}
        self._session_handle: str | None = None
        self._activated_sub: int | None = None
        self._on_activated: Callable[[str, int, str | None], None] | None = None

        # 注册请求参数，异步链路中反复用到
        self._shortcut_id = ""
        self._description = ""
        self._preferred_trigger = ""
        self._on_result: Callable[[Exception | None], None] | None = None

    # ------------------------------------------------------------------ 公共 API

    def register(
        self,
        *,
        shortcut_id: str,
        description: str,
        preferred_trigger: str,
        on_activated: Callable[[str, int, str | None], None],
        on_result: Callable[[Exception | None], None],
    ) -> None:
        """异步注册快捷键；``on_result(error)`` 在成功或失败时回调一次。"""
        self._on_activated = on_activated
        self._on_result = on_result
        self._shortcut_id = shortcut_id
        self._description = description
        self._preferred_trigger = preferred_trigger

        log.debug("portal: requesting session bus connection")
        Gio.bus_get(Gio.BusType.SESSION, None, self._on_bus_ready, None)
        log.debug("portal: bus connection requested")

    def close(self) -> None:
        """关闭门户会话并退订信号。退出时调用，快捷键随之失效。"""
        if self._activated_sub is not None and self._conn is not None:
            self._conn.signal_unsubscribe(self._activated_sub)
        self._activated_sub = None

        if self._conn is not None and self._session_handle is not None:
            # 异步关闭：退出流程同样不能阻塞主循环
            self._conn.call(
                PORTAL_BUS_NAME, self._session_handle, SESSION_IFACE, "Close",
                None, None, Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None,
                self._on_session_closed, None,
            )
        self._session_handle = None

    # ------------------------------------------------------------------ 异步链路

    def _on_bus_ready(self, _source, result, *_user_data) -> None:
        try:
            self._conn = Gio.bus_get_finish(result)
        except GLib.Error as exc:
            self._finish(exc)
            return
        log.debug("portal: connection ready (%s)", self._conn.get_unique_name())
        self._register_host_app()

    def _on_session_closed(self, _conn, result, _data) -> None:
        try:
            _conn.call_finish(result)
            log.debug("portal: session closed")
        except GLib.Error as exc:
            log.debug("portal: closing session failed: %s", exc.message)

    def _finish(self, error: Exception | None) -> None:
        """结束一次注册尝试，把结果交给上层（只会触发一次）。"""
        callback, self._on_result = self._on_result, None
        if callback is not None:
            callback(error)

    def _register_host_app(self) -> None:
        """让非沙箱应用以自身 app id 被门户识别（需要系统里存在对应 .desktop）。"""
        assert self._conn is not None
        self._conn.call(
            PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, HOST_REGISTRY_IFACE, "Register",
            GLib.Variant("(sa{sv})", (self._app_id, {})), None,
            Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None,
            self._on_host_registered, None,
        )

    def _on_host_registered(self, conn, result, _data) -> None:
        try:
            conn.call_finish(result)
            log.debug("portal: host app registered as %s", self._app_id)
        except GLib.Error as exc:
            if "already associated" in exc.message:
                # GtkApplication 通常已经替我们注册过应用身份，这里属正常情况
                log.debug("portal: host app already registered by GTK")
            else:
                # 最常见的原因是缺少 .desktop；继续尝试，让门户给出更明确的报错
                log.warning("portal: host app registration failed (%s)", exc.message)
        self._create_session()

    def _create_session(self) -> None:
        session_token = self._next_token("session")

        def build(handle_token: str) -> GLib.Variant:
            options = {
                "handle_token": GLib.Variant("s", handle_token),
                "session_handle_token": GLib.Variant("s", session_token),
            }
            return GLib.Variant("(a{sv})", (options,))

        self._send_request("CreateSession", build, self._on_session_created)

    def _on_session_created(self, response_code: int, results: dict) -> None:
        error = results.get("_error")
        if error is not None:
            self._finish(error)
            return
        if response_code != RESPONSE_SUCCESS:
            self._finish(ShortcutUnavailable("session request was rejected"))
            return
        session_handle = results.get("session_handle")
        if not session_handle:
            self._finish(ShortcutUnavailable("portal returned no session handle"))
            return
        self._session_handle = session_handle
        log.debug("portal: session created (%s)", session_handle)
        self._bind_shortcuts()

    def _bind_shortcuts(self) -> None:
        session_handle = self._session_handle
        assert session_handle is not None

        def build(handle_token: str) -> GLib.Variant:
            shortcuts = [
                (
                    self._shortcut_id,
                    {
                        "description": GLib.Variant("s", self._description),
                        "preferred_trigger": GLib.Variant("s", self._preferred_trigger),
                    },
                )
            ]
            options = {"handle_token": GLib.Variant("s", handle_token)}
            return GLib.Variant(
                "(oa(sa{sv})sa{sv})", (session_handle, shortcuts, "", options)
            )

        self._send_request("BindShortcuts", build, self._on_shortcuts_bound)

    def _on_shortcuts_bound(self, response_code: int, results: dict) -> None:
        error = results.get("_error")
        if error is not None:
            self._finish(error)
            return
        if response_code != RESPONSE_SUCCESS:
            self._finish(ShortcutUnavailable("shortcut binding was cancelled"))
            return
        if self._session_handle is not None:
            self._subscribe_activated(self._session_handle)
        log.debug("portal: shortcuts bound: %s", results.get("shortcuts"))
        self._finish(None)

    # ------------------------------------------------------------------ 请求与信号

    def _next_token(self, kind: str) -> str:
        self._counter += 1
        return f"{self._token_prefix}_{kind}{self._counter}"

    def _request_path(self, handle_token: str) -> str:
        assert self._conn is not None
        sender = self._conn.get_unique_name() or ":0"
        return (
            f"/org/freedesktop/portal/desktop/request/"
            f"{sender.lstrip(':').replace('.', '_')}/{handle_token}"
        )

    def _send_request(
        self,
        method: str,
        build_params: Callable[[str], GLib.Variant],
        callback: Callable[[int, dict], None],
    ) -> None:
        """发出带 handle_token 的门户请求，并把 Response 信号路由到回调。"""
        assert self._conn is not None
        handle_token = self._next_token("req")
        request_path = self._request_path(handle_token)
        sub_id = self._conn.signal_subscribe(
            PORTAL_BUS_NAME, REQUEST_IFACE, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, self._on_request_response, None,
        )
        self._pending[request_path] = (sub_id, callback)
        self._conn.call(
            PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, GLOBAL_SHORTCUTS_IFACE, method,
            build_params(handle_token), None, Gio.DBusCallFlags.NONE, -1, None,
            self._on_call_reply, request_path,
        )

    def _on_call_reply(self, conn, result, request_path: str) -> None:
        try:
            returned_path = conn.call_finish(result).unpack()[0]
        except GLib.Error as exc:
            self._fail_pending(request_path, ShortcutUnavailable(exc.message))
            return

        # 门户理论上会使用我们提供的 handle_token；若没有，把订阅迁移过去
        if returned_path and returned_path != request_path:
            entry = self._pending.pop(request_path, None)
            if entry is None:
                return
            sub_id, callback = entry
            conn.signal_unsubscribe(sub_id)
            new_sub = conn.signal_subscribe(
                PORTAL_BUS_NAME, REQUEST_IFACE, "Response", returned_path, None,
                Gio.DBusSignalFlags.NONE, self._on_request_response, None,
            )
            self._pending[returned_path] = (new_sub, callback)

    def _on_request_response(
        self, conn, sender_name, object_path, interface_name, signal_name, parameters,
        *_user_data,
    ) -> None:
        entry = self._pending.pop(object_path, None)
        if entry is None:
            return
        sub_id, callback = entry
        conn.signal_unsubscribe(sub_id)
        response_code, results = parameters.unpack()
        callback(response_code, results)

    def _fail_pending(self, request_path: str, error: Exception) -> None:
        entry = self._pending.pop(request_path, None)
        if entry is None:
            return
        sub_id, callback = entry
        if self._conn is not None:
            self._conn.signal_unsubscribe(sub_id)
        callback(1, {"_error": error})

    def _subscribe_activated(self, session_handle: str) -> None:
        # 注意：实测 GNOME 把 Activated 发在 /org/freedesktop/portal/desktop 上，
        # 会话句柄是信号的第一个参数（而不是信号的对象路径），因此这里按门户对象
        # 路径订阅，再在回调里用 session_handle 过滤。
        assert self._conn is not None
        self._activated_sub = self._conn.signal_subscribe(
            PORTAL_BUS_NAME, GLOBAL_SHORTCUTS_IFACE, "Activated", PORTAL_OBJECT_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_activated_signal, None,
        )

    def _on_activated_signal(
        self, conn, sender_name, object_path, interface_name, signal_name, parameters,
        *_user_data,
    ) -> None:
        try:
            session, shortcut_id, timestamp, options = parameters.unpack()
        except (ValueError, TypeError) as exc:
            log.warning("portal: unexpected Activated payload (%s)", exc)
            return
        # 同一连接上可能有多个会话，只处理自己的
        if self._session_handle is not None and str(session) != self._session_handle:
            log.debug("portal: ignoring Activated for foreign session %s", session)
            return
        # activation_token 交给窗口，走合成器认可的正规激活路径
        activation_token = None
        if isinstance(options, dict):
            token = options.get("activation_token")
            if token:
                activation_token = str(token)
        if self._on_activated is not None:
            self._on_activated(shortcut_id, timestamp, activation_token)
