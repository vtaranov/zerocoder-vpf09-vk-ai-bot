"""Минимальный клиент OpenAI-совместимого Chat Completions ProxyAPI."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

Message = Dict[str, str]
Transport = Callable[[urllib.request.Request, int], Dict[str, Any]]


class ProxyAPIError(RuntimeError):
    """Безопасное для вывода пользователю описание ошибки API."""


@dataclass(frozen=True)
class Usage:
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    reasoning_tokens: Optional[int]


@dataclass(frozen=True)
class AssistantResponse:
    text: str
    model: str
    finish_reason: Optional[str]
    duration_seconds: float
    usage: Usage


def _default_transport(request: urllib.request.Request, timeout: int) -> Dict[str, Any]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _nested_int(value: Any, *paths: tuple[str, ...]) -> Optional[int]:
    for path in paths:
        current = value
        for key in path:
            if not isinstance(current, dict):
                break
            current = current.get(key)
        if isinstance(current, int):
            return current
    return None


def parse_response(payload: Dict[str, Any], duration_seconds: float) -> AssistantResponse:
    try:
        choice = payload["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProxyAPIError("Сервер вернул ответ неожиданного формата") from exc
    if not isinstance(text, str) or not text.strip():
        raise ProxyAPIError("Модель не вернула текстовый ответ")

    usage_payload = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    usage = Usage(
        prompt_tokens=_nested_int(usage_payload, ("prompt_tokens",), ("input_tokens",)),
        completion_tokens=_nested_int(usage_payload, ("completion_tokens",), ("output_tokens",)),
        total_tokens=_nested_int(usage_payload, ("total_tokens",)),
        reasoning_tokens=_nested_int(
            usage_payload,
            ("reasoning_tokens",),
            ("completion_tokens_details", "reasoning_tokens"),
            ("output_tokens_details", "reasoning_tokens"),
            ("breakdown", "reasoning_tokens"),
        ),
    )
    return AssistantResponse(
        text=text.strip(),
        model=str(payload.get("model", "unknown")),
        finish_reason=choice.get("finish_reason"),
        duration_seconds=duration_seconds,
        usage=usage,
    )


class ProxyAPIClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: int,
        transport: Transport = _default_transport,
    ) -> None:
        self.api_key = api_key
        self.endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def chat(
        self,
        model: str,
        messages: List[Message],
        max_tokens: int,
        reasoning_effort: Optional[str] = None,
    ) -> AssistantResponse:
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if model.startswith("openai/gpt-6") or model.startswith("gpt-6"):
            body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "zerocoder-vpf09-vk-ai-bot/1.0",
            },
            method="POST",
        )

        started = time.monotonic()
        try:
            payload = self.transport(request, self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            raise ProxyAPIError(f"ProxyAPI вернул HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProxyAPIError(f"Не удалось дождаться ответа ProxyAPI: {reason}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProxyAPIError("ProxyAPI вернул некорректный JSON") from exc

        return parse_response(payload, time.monotonic() - started)


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, AttributeError, KeyError):
        return exc.reason or "неизвестная ошибка"

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return "неизвестная ошибка"
