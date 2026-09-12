"""翻译编排：语言方向判定、长度校验与请求下发（对应规格 §4.7 / §4.8）。"""

from __future__ import annotations

import logging
import unicodedata

from ..config.manager import Config
from ..config.secret import SecretManager
from .client import DeepLClient, TranslationResult
from .errors import TranslationError

log = logging.getLogger(__name__)

# 目标语言显式指定变体：中文用简体，英文用美式（源语言不用变体代码）
TARGET_ZH = "ZH-HANS"
TARGET_EN = "EN-US"

# 方向判定的占比阈值（规格 FR-LANG-1）
HAN_THRESHOLD = 0.2
LATIN_THRESHOLD = 0.2

# 官方单请求总大小上限 128 KiB；留出 JSON 包装的余量
MAX_TEXT_BYTES = 128 * 1024 - 4096

HAN_RANGES = (
    (0x3400, 0x4DBF),    # 扩展 A
    (0x4E00, 0x9FFF),    # 基本区
    (0xF900, 0xFAFF),    # 兼容表意文字
    (0x20000, 0x2FA1F),  # 扩展 B 及之后
)


def is_han(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in HAN_RANGES)


def is_latin(char: str) -> bool:
    return "LATIN" in unicodedata.name(char, "")


def detect_direction(text: str) -> tuple[str | None, str]:
    """返回（source_lang, target_lang）。source 为 None 表示交给 DeepL 自动检测。"""
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return None, TARGET_ZH
    total = len(chars)
    han_ratio = sum(1 for char in chars if is_han(char)) / total
    latin_ratio = sum(1 for char in chars if is_latin(char)) / total

    if han_ratio >= HAN_THRESHOLD and latin_ratio < LATIN_THRESHOLD:
        return "ZH", TARGET_EN
    if latin_ratio >= LATIN_THRESHOLD and han_ratio < HAN_THRESHOLD:
        return "EN", TARGET_ZH
    # 混合语言（例如"你好 hello"）与其他语言一律译为中文，交给 DeepL 自动检测源语言
    return None, TARGET_ZH


class TranslationService:
    """把输入变成一次 DeepL 请求。校验在本地完成，避免发送必然失败的请求。"""

    def __init__(self, config: Config, secrets: SecretManager) -> None:
        self._config = config
        self._secrets = secrets
        self._client: DeepLClient | None = None

    def _get_client(self, api_key: str) -> DeepLClient:
        # 复用同一个客户端以保持连接；endpoint 或 key 变化时重建
        if self._client is None:
            self._client = DeepLClient(api_key=api_key, endpoint=self._config.endpoint)
        return self._client

    async def translate(self, text: str) -> TranslationResult:
        if not text.strip():
            raise TranslationError("empty")

        size = len(text.encode("utf-8"))
        if size > MAX_TEXT_BYTES:
            log.warning("deepl: text too large (%d bytes)", size)
            raise TranslationError("too_large")

        api_key = self._secrets.lookup()
        if not api_key:
            raise TranslationError("no_api_key")

        source_lang, target_lang = detect_direction(text)
        log.debug(
            "deepl: translating %d chars (source=%s, target=%s)",
            len(text), source_lang or "auto", target_lang,
        )
        return await self._get_client(api_key).translate(
            text, source_lang=source_lang, target_lang=target_lang
        )

    async def check_connection(self) -> dict:
        """设置页的 Test Connection：用 /v2/usage，不消耗额度。"""
        api_key = self._secrets.lookup()
        if not api_key:
            raise TranslationError("no_api_key")
        return await self._get_client(api_key).check_usage()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
