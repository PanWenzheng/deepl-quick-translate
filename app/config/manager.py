"""非敏感配置的读写。

只保存非敏感项；DeepL API Key 走 Secret Service（见 ``config/secret.py``，M4 实现）。
读取策略是"宽容"的：文件损坏、字段缺失或类型不对时回退到默认值并记一条警告，
绝不因为配置问题导致程序无法启动。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..constants import (
    APP_ID,
    DEFAULT_ENDPOINT,
    DEFAULT_SHORTCUT_ACCEL,
    GSETTINGS_TOGGLE_COMMAND,
)

log = logging.getLogger(__name__)

CONFIG_VERSION = 1
DEFAULT_LOG_LEVEL = "INFO"

SHORTCUT_BACKENDS = ("portal", "gsettings")


def config_dir() -> Path:
    """配置目录：``$XDG_CONFIG_HOME/<app_id>``，默认为 ``~/.config/<app_id>``。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / APP_ID


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class ShortcutConfig:
    """全局快捷键相关配置。"""

    # 默认用 GSettings 自定义快捷键：实测它没有门户路径的按键泄漏与输入法首键丢失问题，
    # 也不需要首次确认对话框（见规格附录 C）。portal 作为可选后端保留。
    backend: str = "gsettings"
    preferred_trigger: str = DEFAULT_SHORTCUT_ACCEL
    # GSettings 兜底后端占用的路径，便于退出时清理
    gsettings_path: str | None = None
    # GSettings 触发时要执行的命令（开发环境可覆盖）
    command: str = GSETTINGS_TOGGLE_COMMAND


@dataclass
class Config:
    """全部非敏感配置。"""

    version: int = CONFIG_VERSION
    endpoint: str = DEFAULT_ENDPOINT
    close_after_copy: bool = True
    start_on_login: bool = False
    hide_on_focus_loss: bool = True
    log_level: str = DEFAULT_LOG_LEVEL
    shortcut: ShortcutConfig = field(default_factory=ShortcutConfig)


def _as_bool(value: Any, default: bool, key: str) -> bool:
    if isinstance(value, bool):
        return value
    log.warning("config: %s expects a boolean, got %r; using %r", key, value, default)
    return default


def _as_str(value: Any, default: str, key: str) -> str:
    if isinstance(value, str) and value:
        return value
    if value is not None:
        log.warning("config: %s expects a non-empty string, got %r", key, value)
    return default


def _as_optional_str(value: Any, default: str | None, key: str) -> str | None:
    if value is None:
        return default
    if isinstance(value, str):
        return value or default
    log.warning("config: %s expects a string or null, got %r", key, value)
    return default


def config_from_dict(raw: Any) -> Config:
    """把外部字典解析成 :class:`Config`，缺失或非法字段一律回退默认值。"""
    config = Config()
    if not isinstance(raw, dict):
        if raw is not None:
            log.warning("config: root is %s, not an object; using defaults", type(raw).__name__)
        return config

    endpoint = raw.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        config.endpoint = endpoint.strip().rstrip("/")
    elif endpoint is not None:
        log.warning("config: endpoint expects a string, got %r", endpoint)

    config.close_after_copy = _as_bool(
        raw.get("close_after_copy", True), config.close_after_copy, "close_after_copy"
    )
    config.start_on_login = _as_bool(
        raw.get("start_on_login", False), config.start_on_login, "start_on_login"
    )
    config.hide_on_focus_loss = _as_bool(
        raw.get("hide_on_focus_loss", True),
        config.hide_on_focus_loss,
        "hide_on_focus_loss",
    )
    config.log_level = _as_str(raw.get("log_level", ""), config.log_level, "log_level")

    shortcut_raw = raw.get("shortcut")
    if isinstance(shortcut_raw, dict):
        backend = shortcut_raw.get("backend")
        if backend in SHORTCUT_BACKENDS:
            config.shortcut.backend = backend
        elif backend is not None:
            log.warning("config: unknown shortcut backend %r; using 'portal'", backend)

        config.shortcut.preferred_trigger = _as_str(
            shortcut_raw.get("preferred_trigger", ""),
            config.shortcut.preferred_trigger,
            "shortcut.preferred_trigger",
        )
        config.shortcut.gsettings_path = _as_optional_str(
            shortcut_raw.get("gsettings_path"), None, "shortcut.gsettings_path"
        )
        config.shortcut.command = _as_str(
            shortcut_raw.get("command", ""),
            config.shortcut.command,
            "shortcut.command",
        )
    elif shortcut_raw is not None:
        log.warning("config: shortcut expects an object, got %r", shortcut_raw)

    version = raw.get("version")
    if isinstance(version, int):
        config.version = version

    return config


class ConfigManager:
    """负责配置文件的加载与保存。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or config_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Config:
        """读取配置；文件不存在或损坏时返回默认配置。"""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.debug("config: %s not found; using defaults", self._path)
            return Config()
        except OSError as exc:
            log.warning("config: cannot read %s (%s); using defaults", self._path, exc)
            return Config()

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("config: %s is not valid JSON (%s); using defaults", self._path, exc)
            return Config()
        return config_from_dict(raw)

    def save(self, config: Config) -> bool:
        """原子写入配置；失败只记日志，不抛异常打断主流程。"""
        payload = asdict(config)
        payload["version"] = CONFIG_VERSION
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".config-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(tmp_name, self._path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("config: cannot write %s (%s)", self._path, exc)
            return False
        log.debug("config: saved to %s", self._path)
        return True
