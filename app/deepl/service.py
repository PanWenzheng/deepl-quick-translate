"""翻译编排：语言方向判定、长度校验与请求下发（对应规格 §4.7 / §4.8）。"""

from __future__ import annotations

import logging
import unicodedata

from ..config.manager import Config
from ..config.secret import SecretManager
from ..constants import ENDPOINT_FREE, ENDPOINT_PRO
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
        # 记住当前客户端对应的 (Key, Endpoint)：任一变化都要重建，
        # 否则设置页改了 Key/Endpoint 之后仍会拿旧连接去发请求
        self._client_signature: tuple[str, str] | None = None

    async def _get_client(self, api_key: str) -> DeepLClient:
        signature = (api_key, self._config.endpoint)
        if self._client is not None and self._client_signature == signature:
            return self._client
        if self._client is not None:
            await self._client.aclose()
        self._client = DeepLClient(api_key=api_key, endpoint=self._config.endpoint)
        self._client_signature = signature
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
        self._check_endpoint_matches_key(api_key)

        source_lang, target_lang = detect_direction(text)
        log.debug(
            "deepl: translating %d chars (source=%s, target=%s)",
            len(text), source_lang or "auto", target_lang,
        )
        client = await self._get_client(api_key)
        return await client.translate(
            text, source_lang=source_lang, target_lang=target_lang
        )

    async def check_connection(self) -> dict:
        """设置页的 Test Connection：用 /v2/usage，不消耗额度。"""
        api_key = self._secrets.lookup()
        if not api_key:
            raise TranslationError("no_api_key")
        self._check_endpoint_matches_key(api_key)
        client = await self._get_client(api_key)
        return await client.check_usage()

    def _check_endpoint_matches_key(self, api_key: str) -> None:
        """本地先做一次便宜的校验：DeepL 的 Free key 以 ':fx' 结尾。

        端点配错时官方会返回 403 且文案是 Wrong endpoint，但那要等一次网络往返；
        这种不匹配在本地就能判定，直接给用户更准确的提示。
        """
        is_free_key = api_key.endswith(":fx")
        endpoint = self._config.endpoint
        if is_free_key and endpoint == ENDPOINT_PRO:
            log.warning("deepl: free key configured with the pro endpoint")
            raise TranslationError("endpoint_mismatch")
        if not is_free_key and endpoint == ENDPOINT_FREE:
            log.warning("deepl: pro key configured with the free endpoint")
            raise TranslationError("endpoint_mismatch")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
