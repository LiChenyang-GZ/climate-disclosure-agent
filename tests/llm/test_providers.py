"""Provider routing (D90)."""

from cda.llm.cost import ModelTable
from cda.llm.fake import FakeLLM, FakeTurn
from cda.llm.gemini_client import GeminiClient
from cda.llm.openai_client import OpenAIClient
from cda.llm.providers import ProviderRouter, make_llm
from cda.llm.types import Message


async def test_routes_by_provider_and_builds_each_client_once():
    models = ModelTable.load()
    built = []

    def factory(name):
        def build(table):
            built.append(name)
            return FakeLLM([FakeTurn(content=name)] * 5, table)

        return build

    router = ProviderRouter(models, {"openai": factory("openai"), "gemini": factory("gemini")})
    answers = []
    for key in ("luna", "flash", "sol", "lite"):
        response = await router.complete(messages=[Message("user", "hi")], tools=[], model_key=key)
        answers.append(response.message.content)
    assert answers == ["openai", "gemini", "openai", "gemini"]
    assert built == ["openai", "gemini"]


def test_make_llm_is_lazy_and_needs_no_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    router = make_llm(ModelTable.load())
    assert isinstance(router, ProviderRouter)
    assert isinstance(router.client_for("luna"), OpenAIClient)
    assert isinstance(router.client_for("flash"), GeminiClient)
    assert router.client_for("lite") is router.client_for("flash")
