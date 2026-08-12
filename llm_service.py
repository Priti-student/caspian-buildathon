"""Featherless chat-completion client for StudentPilot."""

import json

import httpx


FEATHERLESS_CHAT_URL = "https://api.featherless.ai/v1/chat/completions"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
SYSTEM_PROMPT = (
    "You are StudentPilot, a helpful AI assistant for students. "
    "Answer clearly and concisely."
)
FALLBACK_REPLY = "I’m having trouble reaching my AI service right now. Please try again shortly."


class LLMResponseError(Exception):
    """Raised when Featherless cannot provide a usable structured response."""


class FeatherlessLLM:
    """Generate chat responses through Featherless's OpenAI-compatible API."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def reply(self, user_text: str, history: list[dict[str, str]] | None = None) -> str:
        payload = {
            "model": MODEL,
            "messages": self._messages(SYSTEM_PROMPT, user_text, history),
            "temperature": 0.7,
            "max_tokens": 300,
        }

        try:
            response = httpx.post(
                FEATHERLESS_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            return content or FALLBACK_REPLY
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return FALLBACK_REPLY

    def complete_json(
        self, system_prompt: str, user_text: str, history: list[dict[str, str]] | None = None
    ) -> dict:
        """Request a JSON object and validate the API response before returning it."""
        payload = {
            "model": MODEL,
            "messages": self._messages(system_prompt, user_text, history),
            "temperature": 0,
            "max_tokens": 1800,
            "response_format": {"type": "json_object"},
        }

        try:
            response = httpx.post(
                FEATHERLESS_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=45.0,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LLMResponseError from error

        if not isinstance(result, dict):
            raise LLMResponseError
        return result

    @staticmethod
    def _messages(
        system_prompt: str, user_text: str, history: list[dict[str, str]] | None
    ) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": system_prompt}]
        for item in history or []:
            role, content = item.get("role"), item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        return messages
