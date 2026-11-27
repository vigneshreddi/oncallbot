"""One interface for Anthropic, OpenAI-compatible (OpenAI, Ollama) and a no-LLM fallback.
Returns either a tool call request or a final text answer."""
import json
from dataclasses import dataclass, field
import time

import httpx

from . import config


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""
    error: str | None = None


@dataclass
class Turn:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_message: dict | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def post(url, **kwargs):
    for attempt in range(config.LLM_RETRIES + 1):
        try:
            response = httpx.post(url, timeout=config.LLM_TIMEOUT_SECONDS, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {408, 429, 500, 502, 503, 504}
            if not retryable or attempt == config.LLM_RETRIES:
                raise
            time.sleep(min(2 ** attempt, 4))


def tool_result_messages(client, results):
    if isinstance(client, Anthropic):
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": failed}
            for call, content, failed in results
        ]}]
    return [{"role": "tool", "tool_call_id": call.id, "content": content}
            for call, content, failed in results]


class NoLLM:
    def step(self, system, messages, tools) -> Turn:
        return Turn(text=None)


class OpenAICompat:
    """Works for OpenAI and Ollama's /v1 endpoint."""

    def __init__(self):
        self.base = (config.LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
        self.headers = {"Authorization": f"Bearer {config.LLM_API_KEY or 'ollama'}"}

    def step(self, system, messages, tools) -> Turn:
        payload = {
            "model": config.LLM_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "tools": [{"type": "function", "function": t} for t in tools],
        }
        data = post(f"{self.base}/chat/completions", json=payload, headers=self.headers)
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        turn = Turn(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                assistant_message={key: msg[key] for key in ("role", "content", "tool_calls") if key in msg})
        calls = msg.get("tool_calls") or []
        if calls:
            for call in calls:
                fn = call["function"]
                args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be an object")
                    turn.tool_calls.append(ToolCall(fn["name"], args, call["id"]))
                except (ValueError, TypeError) as exc:
                    turn.tool_calls.append(ToolCall(fn["name"], {}, call["id"], str(exc)))
        else:
            turn.text = msg.get("content") or ""
        return turn


class Anthropic:
    def __init__(self):
        self.base = (config.LLM_BASE_URL or "https://api.anthropic.com").rstrip("/")
        self.headers = {"x-api-key": config.LLM_API_KEY, "anthropic-version": "2023-06-01"}

    def step(self, system, messages, tools) -> Turn:
        payload = {
            "model": config.LLM_MODEL,
            "max_tokens": 1500,
            "system": system,
            "messages": messages,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
        }
        data = post(f"{self.base}/v1/messages", json=payload, headers=self.headers)
        usage = data.get("usage", {})
        turn = Turn(input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                assistant_message={"role": "assistant", "content": data.get("content", [])})
        for block in data.get("content", []):
            if block["type"] == "tool_use":
                args = block.get("input") or {}
                turn.tool_calls.append(ToolCall(block["name"], args if isinstance(args, dict) else {}, block["id"],
                                               None if isinstance(args, dict) else "tool arguments must be an object"))
            if block["type"] == "text":
                turn.text = (turn.text or "") + block["text"]
        return turn


def make_client():
    provider = config.LLM_PROVIDER.lower()
    if provider == "anthropic":
        return Anthropic()
    if provider in {"openai", "ollama"}:
        return OpenAICompat()
    if provider == "none":
        return NoLLM()
    raise ValueError("unsupported LLM provider")
