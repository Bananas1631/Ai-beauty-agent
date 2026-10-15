"""Каталог моделей: короткое имя → провайдер, ID модели и цена.

    qwen3-235b       Cloud.ru, Qwen3-235B-A22B (основная)
    yandexgpt-lite   Yandex AI Studio, YandexGPT Lite
    yandexgpt-pro    Yandex AI Studio, YandexGPT Pro
    gigachat-2-lite  GigaChat 2 Lite (резервная)
    gigachat-2-max   GigaChat 2 Max
    custom           свой OpenAI-совместимый сервер (LLM_BASE_URL)

Клиент создаётся только для провайдера с ключом: без ключей словарь пуст, и агент работает
на офлайн-модели или фейке.
"""

from beauty_agent.adapters.llm.base import FREE, LLMClient, ModelPrice
from beauty_agent.adapters.llm.gigachat import GigaChatClient
from beauty_agent.adapters.llm.openai_compat import OpenAICompatClient
from beauty_agent.core.config import LLMSettings
from beauty_agent.rag.embeddings import EmbeddingsClient, HashingEmbeddings, OpenAICompatEmbeddings


def price_of(settings: LLMSettings, key: str) -> ModelPrice:
    pair = settings.prices.get(key)
    return ModelPrice(*pair) if pair else FREE


def build_clients(settings: LLMSettings) -> dict[str, LLMClient]:
    clients: dict[str, LLMClient] = {}
    if settings.cloudru_api_key is not None:
        clients["qwen3-235b"] = OpenAICompatClient(
            model_key="qwen3-235b",
            model=settings.qwen_model,
            base_url=settings.cloudru_base_url,
            api_key=settings.cloudru_api_key.get_secret_value(),
            price=price_of(settings, "qwen3-235b"),
            timeout_s=settings.timeout_s,
        )
    if settings.yandex_api_key is not None and settings.yandex_folder_id:
        for key, model in (
            ("yandexgpt-lite", settings.yandex_lite_model),
            ("yandexgpt-pro", settings.yandex_pro_model),
        ):
            clients[key] = OpenAICompatClient(
                model_key=key,
                model=f"gpt://{settings.yandex_folder_id}/{model}",
                base_url=settings.yandex_base_url,
                api_key=settings.yandex_api_key.get_secret_value(),
                price=price_of(settings, key),
                timeout_s=settings.timeout_s,
                project=settings.yandex_folder_id,
            )
    if settings.gigachat_auth_key is not None:
        for key, model in (
            ("gigachat-2-lite", settings.gigachat_lite_model),
            ("gigachat-2-max", settings.gigachat_max_model),
        ):
            clients[key] = GigaChatClient(
                model_key=key,
                model=model,
                auth_key=settings.gigachat_auth_key.get_secret_value(),
                scope=settings.gigachat_scope,
                price=price_of(settings, key),
                timeout_s=settings.timeout_s,
                ca_bundle=settings.gigachat_ca_bundle,
            )
    if settings.base_url and settings.model:
        clients["custom"] = OpenAICompatClient(
            model_key="custom",
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value() or "not-needed",
            price=price_of(settings, "custom"),
            timeout_s=settings.timeout_s,
        )
    return clients


def build_embedder(settings: LLMSettings) -> EmbeddingsClient:
    """Облачные эмбеддинги при ключе Cloud.ru, иначе офлайн-хеширование (демо, тесты, CI)."""
    if settings.cloudru_api_key is None:
        return HashingEmbeddings()
    return OpenAICompatEmbeddings(
        model_key="bge-m3",
        model=settings.embeddings_model,
        base_url=settings.cloudru_base_url,
        api_key=settings.cloudru_api_key.get_secret_value(),
        price=price_of(settings, "bge-m3"),
        timeout_s=settings.timeout_s,
    )
