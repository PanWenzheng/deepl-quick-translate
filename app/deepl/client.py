"""DeepL HTTP 客户端。

若干细节来自官方文档核对（规格附录 B）：

* 认证头 ``Authorization: DeepL-Auth-Key <key>``；
* ``source_lang`` 省略即自动检测（协议里没有 ``Auto`` 取值）；
* 错误体有 ``{message, code}`` 与 ``{"error": {"message"}}`` 两种形态，且**不能**靠
  ``message`` 文本判断，要按状态码与 ``code`` 分支；
* 响应头 ``X-Trace-ID`` 是排障用的，允许记录（不含用户内容）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .errors import TranslationError

log = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0

# 官方 /v2/translate 列出的状态码 → 我们的错误分类
STATUS_KINDS = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "unexpected",
    413: "too_large",
    414: "unexpected",
    429: "rate_limit",
    456: "quota",
    500: "server",
    503: "server",
    504: "server",
    529: "rate_limit",
}


@dataclass
class TranslationResult:
    text: str
    detected_source_language: str | None = None


def parse_error_body(response: httpx.Response) -> tuple[str | None, str | None]:
    """返回（code, message），同时兼容两种错误体形态。"""
    try:
        body = response.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    code = body.get("code")
    message = body.get("message")
    if message is None:
        nested = body.get("error")
        if isinstance(nested, dict):
            message = nested.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


class DeepLClient:
    """异步 DeepL 客户端。实例应只在同一个事件循环里使用。"""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ 内部

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"DeepL-Auth-Key {self._api_key}"}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _log_response(self, operation: str, response: httpx.Response, elapsed_ms: int) -> None:
        # 只记录状态码、耗时与排障 ID；绝不记录请求内容或 Key
        trace_id = response.headers.get("X-Trace-ID")
        log.debug(
            "deepl: %s → %s in %dms (trace-id=%s)",
            operation, response.status_code, elapsed_ms, trace_id or "-",
        )

    def _error_for(self, response: httpx.Response) -> TranslationError:
        kind = STATUS_KINDS.get(response.status_code, "unexpected")
        code, message = parse_error_body(response)
        trace_id = response.headers.get("X-Trace-ID")
        if kind in ("auth", "quota", "unexpected", "bad_request") or response.status_code >= 500:
            # 这些属于需要人看一眼的情况，记一条（不含用户文本）
            log.warning(
                "deepl: request failed with %s (code=%s, trace-id=%s, message=%s)",
                response.status_code, code or "-", trace_id or "-", message or "-",
            )
        return TranslationError(
            kind, status=response.status_code, code=code, trace_id=trace_id, detail=message
        )

    async def _post(self, path: str, payload: dict, operation: str) -> httpx.Response:
        url = f"{self._endpoint}{path}"
        started = time.monotonic()
        try:
            response = await self._http().post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise TranslationError("timeout", detail=str(exc)) from exc
        except httpx.TransportError as exc:
            raise TranslationError("network", detail=str(exc)) from exc
        self._log_response(operation, response, int((time.monotonic() - started) * 1000))
        return response

    # ------------------------------------------------------------------ 公共 API

    async def translate(
        self, text: str, *, source_lang: str | None, target_lang: str
    ) -> TranslationResult:
        """翻译一段文本。``source_lang`` 为 None 表示交给 DeepL 自动检测。"""
        payload: dict[str, object] = {"text": [text], "target_lang": target_lang}
        if source_lang:
            payload["source_lang"] = source_lang

        response = await self._post("/v2/translate", payload, "translate")
        if response.status_code != 200:
            raise self._error_for(response)
        try:
            body = response.json()
            first = body["translations"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError("unexpected", detail=str(exc)) from exc
        return TranslationResult(
            text=first.get("text", ""),
            detected_source_language=first.get("detected_source_language"),
        )

    async def check_usage(self) -> dict:
        """``GET /v2/usage``：验证 Key/Endpoint/网络，且不消耗翻译额度。"""
        url = f"{self._endpoint}/v2/usage"
        started = time.monotonic()
        try:
            response = await self._http().get(url, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise TranslationError("timeout", detail=str(exc)) from exc
        except httpx.TransportError as exc:
            raise TranslationError("network", detail=str(exc)) from exc
        self._log_response("usage", response, int((time.monotonic() - started) * 1000))
        if response.status_code != 200:
            raise self._error_for(response)
        try:
            return response.json()
        except ValueError as exc:
            raise TranslationError("unexpected", detail=str(exc)) from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
