"""LLM 客户端（OpenAI 兼容），只用于「看板简介润色」这类小文案。

不引入第三方依赖：标准库 urllib 直接调 /chat/completions。
没配置 LLM_BASE_URL / LLM_API_KEY 时 available() 为 False，
调用方自动回退到规则生成的摘要，平台功能不受影响。

环境变量：
    LLM_BASE_URL  例如 https://api.deepseek.com/v1 （或完整 .../chat/completions）
    LLM_API_KEY   sk-...
    LLM_MODEL     默认 deepseek-chat
    LDR_LLM_TIMEOUT 超时秒数，默认 25
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config


class LLMError(Exception):
    pass


def available() -> bool:
    return bool(config.LLM_BASE_URL and config.LLM_API_KEY)


def _endpoint() -> str:
    base = (config.LLM_BASE_URL or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def chat(messages: list[dict], *, model: str | None = None,
         temperature: float = 0.6, max_tokens: int = 600,
         timeout: float | None = None) -> str:
    """发一次 chat 请求，返回 assistant 文本；失败抛 LLMError。"""
    if not available():
        raise LLMError("未配置 LLM（需要 LLM_BASE_URL / LLM_API_KEY）")

    payload = {
        "model": model or config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise LLMError(f"LLM HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"LLM 连接失败: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise LLMError(f"LLM 请求失败: {e}") from e
    except ValueError as e:
        raise LLMError(f"LLM 返回不是 JSON: {e}") from e

    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"LLM 返回结构异常: {str(data)[:300]}") from e
