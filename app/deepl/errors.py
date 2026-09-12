"""DeepL 调用的错误类型与用户可见文案（对应规格 §8）。"""

from __future__ import annotations

# kind → （英文主文案, 中文说明行）
KIND_MESSAGES: dict[str, tuple[str, str]] = {
    "no_api_key": (
        "API key required",
        "请先设置 DeepL API Key：点右上角齿轮进入设置，或运行 deepl-quick-translate --set-api-key",
    ),
    "auth": ("Translation failed", "API Key 无效或缺少权限，请检查 API Key"),
    "endpoint_mismatch": (
        "Wrong endpoint",
        "Endpoint 与账户类型不匹配：Free 账户请用 api-free.deepl.com，Pro 账户用 api.deepl.com",
    ),
    "bad_request": ("Translation failed", "请求参数无效（属程序缺陷，不应出现）"),
    "unexpected": ("Translation failed", "收到意外的服务端响应"),
    "too_large": ("Text too long", "文本超出 DeepL 单次上限（128 KiB）"),
    "rate_limit": ("Translation failed", "请求过于频繁，请稍后重试"),
    "quota": ("Translation failed", "DeepL 额度已用完（Free 每月 50 万字符）"),
    "server": ("Translation failed", "DeepL 服务暂时不可用"),
    "network": ("Translation failed", "无法连接 DeepL，请检查网络"),
    "timeout": ("Translation timeout", "请求超时，未自动重试"),
    # 空输入不该走到这里（提交前已拦截），留作防御
    "empty": ("", ""),
}


class TranslationError(RuntimeError):
    """翻译失败。``kind`` 决定界面文案，``status`` / ``code`` / ``trace_id`` 用于排查。"""

    def __init__(
        self,
        kind: str,
        *,
        status: int | None = None,
        code: str | None = None,
        trace_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.status = status
        self.code = code
        self.trace_id = trace_id
        self.detail = detail

    @property
    def user_message(self) -> tuple[str, str]:
        """界面用的（主文案, 说明行）。"""
        return KIND_MESSAGES.get(self.kind, KIND_MESSAGES["unexpected"])
