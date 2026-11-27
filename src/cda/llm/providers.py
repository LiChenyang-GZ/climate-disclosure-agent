"""Route each call to the adapter of the model key's provider (D90)."""

from collections.abc import Callable

from cda.llm.client import LLMClient
from cda.llm.cost import ModelTable
from cda.llm.types import LLMResponse, Message, ToolSpec


def _openai(models: ModelTable) -> LLMClient:
    from cda.llm.openai_client import OpenAIClient

    return OpenAIClient(models)


def _gemini(models: ModelTable) -> LLMClient:
    from cda.llm.gemini_client import GeminiClient

    return GeminiClient(models)


FACTORIES: dict[str, Callable[[ModelTable], LLMClient]] = {"openai": _openai, "gemini": _gemini}


class ProviderRouter:
    """Builds each provider's client on first use, so one provider's missing key never
    affects runs on another."""

    def __init__(
        self,
        models: ModelTable,
        factories: dict[str, Callable[[ModelTable], LLMClient]] | None = None,
    ) -> None:
        self._models = models
        self._factories = factories or FACTORIES
        self._clients: dict[str, LLMClient] = {}

    def client_for(self, model_key: str) -> LLMClient:
        provider = self._models[model_key].provider
        if provider not in self._clients:
            self._clients[provider] = self._factories[provider](self._models)
        return self._clients[provider]

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSpec],
        model_key: str,
        seed: int | None = None,
        tool_choice: str = "auto",
        max_output_tokens: int = 2000,
    ) -> LLMResponse:
        return await self.client_for(model_key).complete(
            messages=messages,
            tools=tools,
            model_key=model_key,
            seed=seed,
            tool_choice=tool_choice,
            max_output_tokens=max_output_tokens,
        )


def make_llm(models: ModelTable) -> LLMClient:
    return ProviderRouter(models)
