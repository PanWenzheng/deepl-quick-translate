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
