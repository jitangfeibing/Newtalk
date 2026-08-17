from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI


class OpenAICompatibleChatModel:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        system_prompt: str | None = None,
        timeout_seconds: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
        )

    async def stream(self, user_text: str) -> AsyncIterator[str]:
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_text})

        async with self._client.chat.completions.stream(
            model=self.model,
            messages=messages,
        ) as stream:
            async for event in stream:
                if event.type != "content.delta":
                    continue
                delta = event.delta
                if isinstance(delta, str) and delta:
                    yield delta

    async def aclose(self) -> None:
        await self._client.close()
