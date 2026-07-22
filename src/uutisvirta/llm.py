from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMClient(ABC):
    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> LLMResponse:
        ...


class ClaudeClient(LLMClient):
    def __init__(self) -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> LLMResponse:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return LLMResponse(
            content=msg.content[0].text,
            model=msg.model,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
        )


class OpenAIClient(LLMClient):
    def __init__(self) -> None:
        from openai import OpenAI
        self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self._model = os.environ.get("OPENAI_MODEL", "gpt-4o")

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        choice = resp.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            model=resp.model,
            input_tokens=resp.usage.prompt_tokens,
            output_tokens=resp.usage.completion_tokens,
        )


def get_client() -> LLMClient:
    provider = os.environ.get("LLM_PROVIDER", "claude").lower()
    if provider == "claude":
        return ClaudeClient()
    elif provider == "openai":
        return OpenAIClient()
    else:
        raise ValueError(f"Tuntematon LLM_PROVIDER: {provider!r}. Käytä 'claude' tai 'openai'.")
