"""日志配置。

隐私约束（见规格 §4.12）：日志中**禁止**出现 DeepL API Key、用户翻译文本与
剪贴板内容。允许记录的排障信息包括请求耗时、HTTP 状态码、响应头
``X-Trace-ID`` 以及程序生命周期事件。
"""

from __future__ import annotations

import logging
import sys

DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}

# 第三方库的日志不归我们控制：httpx / httpcore 在 DEBUG 下会打印请求对象，
# 一旦其 repr 行为变化就可能把 Authorization 头写进日志。规格 §32 禁止记录 Key，
# 因此这两个 logger 一律压到 WARNING，不随 --verbose 放开。
SUPPRESSED_LOGGERS = ("httpx", "httpcore", "asyncio")


def normalize_level(level_name: str | None) -> str:
    """把配置里的日志级别规范化为合法值，非法则回退到 INFO。"""
    if isinstance(level_name, str):
        candidate = level_name.strip().upper()
        if candidate in _LEVELS:
            return candidate
    return DEFAULT_LOG_LEVEL


def setup_logging(level_name: str | None = DEFAULT_LOG_LEVEL, *, stream=None) -> None:
    """配置根 logger。默认输出到 stderr，便于 autostart 与终端排查。"""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(normalize_level(level_name))

    for name in SUPPRESSED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
