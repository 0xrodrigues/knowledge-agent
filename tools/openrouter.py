"""Thin OpenRouter HTTP client built on httpx."""
from __future__ import annotations

import json
from typing import Optional

import httpx

from config.settings import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    require_openrouter,
)

DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class OpenRouterError(RuntimeError):
    """Raised when the OpenRouter API returns an error or malformed response."""


def chat(
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.0,
    response_format_json: bool = False,
) -> str:
    """Send a single-turn chat completion request and return the assistant text."""
    require_openrouter()
    payload: dict = {
        "model": model or OPENROUTER_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/knowledge-agent",
        "X-Title": "knowledge-agent",
    }

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            response = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise OpenRouterError(f"HTTP transport failure: {exc}") from exc

    if response.status_code >= 400:
        raise OpenRouterError(
            f"OpenRouter returned HTTP {response.status_code}: {response.text}"
        )

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise OpenRouterError(
            f"OpenRouter returned non-JSON body: {response.text!r}"
        ) from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"Unexpected OpenRouter payload shape: {data!r}"
        ) from exc
