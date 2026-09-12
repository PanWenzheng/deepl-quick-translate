"""DeepL API Key 的存取（GNOME Secret Service / 密钥环）。

规格 §32：凭据优先存进 Secret Service，**不写明文配置文件**，日志中禁止出现 Key。

找不到密钥环时的兜底：只读环境变量 ``DEEPL_API_KEY``（便于开发与 CI），绝不落盘。
"""

from __future__ import annotations

import logging
import os

import gi

gi.require_version("Secret", "1")
from gi.repository import GLib, Secret  # noqa: E402

from ..constants import APP_ID  # noqa: E402

log = logging.getLogger(__name__)

ENV_VAR = "DEEPL_API_KEY"
ATTRIBUTE = "deepl_api_key"
LABEL = "DeepL API Key"


def mask(api_key: str) -> str:
    """按规格回显：``••••••••abcd``（仅末 4 位）。"""
    tail = api_key[-4:] if len(api_key) >= 4 else ""
    return f"{'•' * 8}{tail}"


class SecretManager:
    """API Key 的读写。读取结果带内存缓存，避免每次请求都走 D-Bus。"""

    def __init__(self) -> None:
        self._schema: Secret.Schema | None = None
        self._cached: str | None = None
        self._cache_valid = False
        self.source: str | None = None

    # ------------------------------------------------------------------ 内部

    def _get_schema(self) -> Secret.Schema:
        if self._schema is None:
            self._schema = Secret.Schema.new(
                APP_ID,
                Secret.SchemaFlags.NONE,
                {ATTRIBUTE: Secret.SchemaAttributeType.STRING},
            )
        return self._schema

    def _attributes(self) -> dict[str, str]:
        return {ATTRIBUTE: "api-key"}

    # ------------------------------------------------------------------ 公共 API

    def store(self, api_key: str) -> bool:
        """写入系统密钥环。失败返回 False（调用方负责提示用户）。"""
        api_key = api_key.strip()
        if not api_key:
            return False
        try:
            ok = Secret.password_store_sync(
                self._get_schema(),
                self._attributes(),
                Secret.COLLECTION_DEFAULT,
                LABEL,
                api_key,
                None,
            )
        except GLib.Error as exc:
            log.error("keyring: cannot store API key (%s)", exc.message)
            return False
        if ok:
            self._cached = api_key
            self._cache_valid = True
            self.source = "keyring"
            log.info("keyring: API key stored")
        return bool(ok)

    def lookup(self) -> str | None:
        """读取 API Key：优先密钥环，其次环境变量。返回值绝不写日志。"""
        if self._cache_valid:
            return self._cached

        value: str | None = None
        try:
            value = Secret.password_lookup_sync(
                self._get_schema(), self._attributes(), None
            )
        except GLib.Error as exc:
            log.debug("keyring: lookup failed (%s)", exc.message)

        if value:
            self.source = "keyring"
        else:
            value = os.environ.get(ENV_VAR) or None
            if value:
                self.source = "env"
                log.info("keyring: using API key from %s", ENV_VAR)

        self._cached = value
        self._cache_valid = True
        return value

    def clear(self) -> bool:
        """从密钥环删除 API Key。"""
        try:
            ok = Secret.password_clear_sync(
                self._get_schema(), self._attributes(), None
            )
        except GLib.Error as exc:
            log.error("keyring: cannot clear API key (%s)", exc.message)
            return False
        self._cached = None
        self._cache_valid = True
        self.source = None
        log.info("keyring: API key cleared")
        return bool(ok)

    def status(self) -> tuple[bool, str | None, str | None]:
        """返回（是否已配置, 掩码后的 Key, 来源）。"""
        value = self.lookup()
        if not value:
            return False, None, None
        return True, mask(value), self.source
