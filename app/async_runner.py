"""把 asyncio 协程放到后台线程的事件循环里执行。

GTK 的主循环不是 asyncio 循环，直接在 UI 线程里跑 httpx 会卡住界面。这里用后台线程
持有事件循环：协程在那边跑，结果通过 ``GLib.idle_add`` 送回主线程更新 UI。
这样既满足"UI 不阻塞"，又能真正取消请求（Future.cancel → 任务被取消）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Any, Callable, Coroutine

from gi.repository import GLib

log = logging.getLogger(__name__)


class AsyncRunner:
    """后台 asyncio 事件循环。整个进程共用一个。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="async-runner", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        on_done: Callable[[Any, Exception | None], Any],
    ) -> Future:
        """提交协程；``on_done(result, error)`` 在主线程被调用。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def finished(done: Future) -> None:
            try:
                result = done.result()
            except asyncio.CancelledError:
                log.debug("async: task cancelled, no callback")
                return
            except Exception as exc:  # noqa: BLE001 - 统一转交调用方处理
                GLib.idle_add(on_done, None, exc)
                return
            GLib.idle_add(on_done, result, None)

        future.add_done_callback(finished)
        return future

    def shutdown(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
