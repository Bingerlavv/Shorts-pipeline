"""Локальная LLM через Ollama. Схема передаётся в поле format."""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ...config import settings
from .base import LLMError, LLMProvider

log = logging.getLogger(__name__)

# Модель по умолчанию — только запасной вариант: обычно её задаёт
# SHORTS_LLM_MODEL. Осмысленный размер для одной видеокарты — 8-14B: модель
# должна целиком помещаться в видеопамять, иначе Ollama читает веса с диска и
# счёт растягивается на десятки минут.
DEFAULT_MODEL = "qwen2.5:14b-instruct"

# Сколько держать модель загруженной между запросами. Длинная расшифровка
# режется на куски, и каждый идёт отдельным запросом; при выгрузке между ними
# каждая загрузка стоит ~45 с.
KEEP_ALIVE = "30m"

# Ollama отвечает 503, пока скачивает или загружает модель. Перед анализом это
# стоит переждать: к этому моменту уже потрачены минуты на загрузку исходника и
# распознавание речи, и ронять задачу из-за пары секунд занятости незачем.
READY_ATTEMPTS = 10
READY_BACKOFF = 6.0

# Размер окна контекста. Задавать обязательно: в настройках Ollama на этой
# машине стоит OLLAMA_CONTEXT_LENGTH=262144, и под такой KV-кэш модель на 14B
# в 12 ГБ видеопамяти не помещается ни при каких условиях. 16k с запасом
# покрывает кусок расшифровки (25 минут речи — это ~10-12 тысяч токенов)
# и экономит около 3 ГБ против значения по умолчанию.
NUM_CTX = 16384


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str = "") -> None:
        super().__init__(model or DEFAULT_MODEL)
        self.base_url = settings.ollama_base_url.rstrip("/")

    @property
    def _is_local(self) -> bool:
        return (urlparse(self.base_url).hostname or "").lower() in LOCAL_HOSTS

    def _client(self, timeout: Any) -> httpx.Client:
        """Клиент для запросов к Ollama.

        Для локального адреса системный прокси отключается принудительно.
        httpx по умолчанию читает HTTP_PROXY и заворачивает туда даже
        localhost — причём NO_PROXY не спасает: замеряли, запрос уходил в
        прокси и висел до таймаута. А если прокси-клиент выставил
        ALL_PROXY=socks5://…, httpx и вовсе падает с ImportError про socksio.
        Через прокси имеет смысл ходить только к Ollama на другой машине.
        """
        return httpx.Client(timeout=timeout, trust_env=not self._is_local)

    def is_available(self, attempts: int = 1) -> tuple[bool, str]:
        """Проверяет готовность. attempts > 1 — подождать, пока Ollama освободится.

        Пока Ollama скачивает или загружает модель, она отвечает 503 на любой
        запрос, включая /api/tags. Для страницы диагностики нужен быстрый ответ,
        а вот перед анализом ждать стоит: к этому моменту уже потрачены минуты
        на загрузку исходника и распознавание речи.
        """
        last = ""
        for attempt in range(max(1, attempts)):
            try:
                with self._client(10.0) as client:
                    response = client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if attempt + 1 < attempts:
                    log.info("Ollama пока не готова (%s), жду %.0f с", last[:80], READY_BACKOFF)
                    time.sleep(READY_BACKOFF)
        else:
            hint = (
                " — похоже, она занята загрузкой модели; попробуй ещё раз через минуту"
                if "503" in last
                else " — запусти её командой «ollama serve» или открой приложение Ollama"
            )
            return False, f"Ollama недоступна по {self.base_url}: {last}{hint}"

        names = sorted(m.get("name", "") for m in response.json().get("models", []))
        if self.model not in names and not any(n.startswith(self.model) for n in names):
            have = ", ".join(names) if names else "ни одной"
            return False, (
                f"модель {self.model} не загружена (есть: {have}). "
                f"Скачай её командой «ollama pull {self.model}» либо укажи в "
                f"SHORTS_LLM_MODEL одну из установленных"
            )
        return True, ""

    def _post_chat(self, *, json: dict[str, Any], timeout: httpx.Timeout) -> httpx.Response:
        """Отправляет запрос, переживая занятость Ollama.

        503 может прийти и здесь: между проверкой готовности и самим запросом
        Ollama успевает начать выгружать или подгружать модель. Всё остальное —
        ошибка в запросе или в модели — отдаётся сразу, повторять смысла нет.
        """
        last: httpx.HTTPError | None = None
        for attempt in range(READY_ATTEMPTS):
            try:
                with self._client(timeout) as client:
                    response = client.post(f"{self.base_url}/api/chat", json=json)
                if response.status_code == 503:
                    response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 503:
                    raise
                last = exc
            except httpx.TransportError as exc:
                last = exc

            if attempt + 1 < READY_ATTEMPTS:
                log.info("Ollama занята, повтор через %.0f с", READY_BACKOFF)
                time.sleep(READY_BACKOFF)

        raise LLMError(
            f"Ollama так и не освободилась за "
            f"{READY_ATTEMPTS * READY_BACKOFF:.0f} с: {last}"
        )

    def installed_models(self) -> list[str]:
        """Что реально загружено — показывается в диагностике панели."""
        try:
            with self._client(3.0) as client:
                response = client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
        except Exception:  # noqa: BLE001
            return []
        return sorted(m.get("name", "") for m in response.json().get("models", []))

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "result",
        max_tokens: int = 16000,
        cache_system: bool = True,
    ) -> dict[str, Any]:
        available, reason = self.is_available(attempts=READY_ATTEMPTS)
        if not available:
            raise LLMError(reason)

        try:
            response = self._post_chat(
                json={
                    "model": self.model,
                    "stream": False,
                    "format": schema,  # Ollama принимает JSON Schema напрямую
                    # Рассуждающие модели (gemma4, qwen3) по умолчанию пишут ход
                    # мысли в отдельное поле thinking и тратят на него бюджет
                    # токенов, оставляя content пустым. Для структурированного
                    # ответа рассуждение не нужно — и без него втрое быстрее.
                    "think": False,
                    "keep_alive": KEEP_ALIVE,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": 0.4,
                        "num_ctx": NUM_CTX,
                    },
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=httpx.Timeout(1800.0, connect=15.0),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama: {exc}") from exc

        payload = response.json()
        message = payload.get("message", {})
        content = message.get("content", "")
        if not content:
            if message.get("thinking"):
                raise LLMError(
                    f"модель {self.model} ушла в рассуждения и не выдала ответ "
                    f"(остановка: {payload.get('done_reason', 'неизвестно')}). "
                    "Возьми модель без режима размышления или подними лимит токенов"
                )
            raise LLMError(f"Ollama вернула пустой ответ (модель {self.model})")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"не удалось разобрать JSON от Ollama: {exc}") from exc
