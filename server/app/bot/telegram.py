"""Управление конвейером из Телеграма.

Сценарий один: кинул ссылку — выбрал пресет — выбрал аккаунты — поехало.
Всё остальное (ревью, монтаж, публикация) делается автоматически, а бот
присылает отчёт о ходе работы.

Библиотеку не берём: нужен один цикл опроса и десяток кнопок, а httpx уже есть
в зависимостях. Прокси для api.telegram.org, наоборот, не отключаем — он там
как раз может понадобиться.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import Account, Preset, Project, ProjectStatus
from ..queue import enqueue

log = logging.getLogger("bot")

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 25  # столько держим长 long polling, секунды
URL_PATTERN = re.compile(r"https?://\S+")

HELP = (
    "Пришли ссылку на видео — дальше я спрошу пресет и аккаунты.\n\n"
    "Команды:\n"
    "/projects — что в работе\n"
    "/help — эта справка"
)


@dataclass
class Draft:
    """Незаконченный выбор пользователя: живёт до нажатия «Поехали»."""

    url: str
    preset_id: int | None = None
    account_ids: set[int] = field(default_factory=set)
    message_id: int | None = None


class TelegramBot:
    def __init__(self, token: str, allowed: set[int]) -> None:
        self.token = token
        self.allowed = allowed
        self.drafts: dict[int, Draft] = {}
        self.offset = 0
        # Долгий таймаут запроса — это ожидание сообщений, а не медленная сеть.
        self.http = httpx.Client(timeout=httpx.Timeout(POLL_TIMEOUT + 15, connect=15))

    # --- транспорт -------------------------------------------------------

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        response = self.http.post(API.format(token=self.token, method=method), json=payload)
        data = response.json()
        if not data.get("ok"):
            log.warning("телеграм отклонил %s: %s", method, data.get("description"))
        return data.get("result") or {}

    def send(self, chat_id: int, text: str, keyboard: list | None = None) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return (self.call("sendMessage", **payload) or {}).get("message_id")

    def edit(self, chat_id: int, message_id: int, text: str, keyboard: list | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self.call("editMessageText", **payload)

    # --- клавиатуры ------------------------------------------------------

    def preset_keyboard(self) -> tuple[str, list]:
        with SessionLocal() as db:
            presets = db.scalars(select(Preset).order_by(Preset.is_default.desc(), Preset.name)).all()
            rows = [
                [{"text": f"{p.name}{' ★' if p.is_default else ''}", "callback_data": f"preset:{p.id}"}]
                for p in presets
            ]
        if not rows:
            return "На сервере нет ни одного пресета. Создай его в панели.", []
        return "Каким пресетом монтировать?", rows

    def account_keyboard(self, draft: Draft) -> tuple[str, list]:
        """Только те аккаунты, что заведены на сервере: свои вписать нельзя."""
        with SessionLocal() as db:
            accounts = db.scalars(
                select(Account).where(Account.is_active.is_(True)).order_by(Account.platform, Account.name)
            ).all()
            rows = [
                [
                    {
                        "text": f"{'☑' if a.id in draft.account_ids else '☐'} {a.name} · {a.platform}",
                        "callback_data": f"acc:{a.id}",
                    }
                ]
                for a in accounts
            ]
        if not rows:
            return (
                "Нет подключённых аккаунтов. Добавь их в панели на странице «Аккаунты», "
                "либо жми «Только смонтировать».",
                [[{"text": "Только смонтировать", "callback_data": "go"}]],
            )
        rows.append([{"text": "▶ Поехали", "callback_data": "go"}])
        rows.append([{"text": "✖ Отмена", "callback_data": "cancel"}])
        return "Куда публиковать? Отметь аккаунты и жми «Поехали».", rows

    # --- обработка -------------------------------------------------------

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()

        # /id отвечаем всем: это единственный способ узнать номер чата,
        # чтобы вписать его в список разрешённых. Ничего чужого он не выдаёт —
        # человек и так знает, из какого чата пишет.
        if text.startswith("/id"):
            self.send(
                chat_id,
                f"Номер этого чата: {chat_id}\n\n"
                "Впиши его в SHORTS_TELEGRAM_ALLOWED_CHATS в файле .env "
                "и перезапусти бота.",
            )
            return

        if chat_id not in self.allowed:
            # Номер пишем в лог: владелец увидит его и добавит, не бегая за
            # сторонними ботами. Заодно видно, что бота кто-то нашёл.
            name = (message.get("from") or {}).get("username") or "без имени"
            log.warning("чат %s (@%s) не в списке разрешённых", chat_id, name)
            self.send(chat_id, "Этот бот вас не знает. Попроси владельца добавить ваш id.")
            return

        if text.startswith("/start") or text.startswith("/help"):
            self.send(chat_id, HELP)
            return
        if text.startswith("/projects"):
            self.send(chat_id, self.projects_summary())
            return

        found = URL_PATTERN.search(text)
        if not found:
            self.send(chat_id, "Нужна ссылка на видео. " + HELP)
            return

        self.drafts[chat_id] = Draft(url=found.group(0))
        caption, keyboard = self.preset_keyboard()
        self.drafts[chat_id].message_id = self.send(chat_id, caption, keyboard)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        data = callback.get("data") or ""
        self.call("answerCallbackQuery", callback_query_id=callback["id"])

        if chat_id not in self.allowed:
            return
        draft = self.drafts.get(chat_id)
        if draft is None:
            self.edit(chat_id, message_id, "Заявка устарела — пришли ссылку заново.")
            return

        if data == "cancel":
            self.drafts.pop(chat_id, None)
            self.edit(chat_id, message_id, "Отменено.")
            return

        if data.startswith("preset:"):
            draft.preset_id = int(data.split(":", 1)[1])
            caption, keyboard = self.account_keyboard(draft)
            self.edit(chat_id, message_id, caption, keyboard)
            return

        if data.startswith("acc:"):
            account_id = int(data.split(":", 1)[1])
            # Повторное нажатие снимает галочку — обычное поведение чек-листа.
            draft.account_ids.symmetric_difference_update({account_id})
            caption, keyboard = self.account_keyboard(draft)
            self.edit(chat_id, message_id, caption, keyboard)
            return

        if data == "go":
            self.drafts.pop(chat_id, None)
            summary = self.start_project(chat_id, draft)
            self.edit(chat_id, message_id, summary)

    # --- работа с конвейером ---------------------------------------------

    def start_project(self, chat_id: int, draft: Draft) -> str:
        with SessionLocal() as db:
            accounts: list[Account] = []
            names: list[str] = []
            if draft.account_ids:
                accounts = list(
                    db.scalars(select(Account).where(Account.id.in_(draft.account_ids))).all()
                )
                names = [a.name for a in accounts]

            overrides: dict[str, Any] = {}
            if accounts:
                # Аккаунты живут связью, а не конфигом. В переопределениях
                # остаётся только сам факт автопубликации.
                overrides["publish"] = {"auto": True}

            project = Project(
                source_url=draft.url,
                preset_id=draft.preset_id,
                # Автопрогон: ради него бот и нужен — руками ничего не подтверждаем.
                auto_publish=True,
                config_overrides=overrides,
                status=ProjectStatus.NEW,
                telegram_chat_id=chat_id,
            )
            project.accounts = accounts
            db.add(project)
            db.commit()
            db.refresh(project)

            enqueue(db, "project.ingest", project_id=project.id,
                    payload={"auto": True}, priority=50)

            preset = db.get(Preset, draft.preset_id) if draft.preset_id else None

        lines = [
            f"Взял в работу проект №{project.id}.",
            f"Ссылка: {draft.url}",
            f"Пресет: {preset.name if preset else 'по умолчанию'}",
            f"Публикация: {', '.join(names) if names else 'только монтаж, без выкладки'}",
            "",
            "Дальше всё само. Напишу, когда будет результат.",
        ]
        return "\n".join(lines)

    def projects_summary(self) -> str:
        with SessionLocal() as db:
            projects = db.scalars(
                select(Project).order_by(Project.created_at.desc()).limit(8)
            ).all()
            if not projects:
                return "Пока ничего нет."
            rows = []
            for project in projects:
                title = (project.title or project.source_url)[:44]
                rows.append(f"№{project.id} · {STATUS_RU.get(project.status.value, project.status.value)} · {title}")
        return "\n".join(rows)

    # --- цикл ------------------------------------------------------------

    def poll_once(self) -> None:
        updates = self.call("getUpdates", offset=self.offset, timeout=POLL_TIMEOUT)
        for update in updates if isinstance(updates, list) else []:
            self.offset = update["update_id"] + 1
            try:
                if "message" in update:
                    self.handle_message(update["message"])
                elif "callback_query" in update:
                    self.handle_callback(update["callback_query"])
            except Exception:  # noqa: BLE001 — одно битое сообщение не должно ронять бота
                log.exception("не смог обработать обновление %s", update.get("update_id"))


STATUS_RU = {
    "new": "новый",
    "downloading": "качаю",
    "transcribing": "распознаю",
    "analyzing": "анализ",
    "ready": "на ревью",
    "rendering": "монтаж",
    "done": "готово",
    "failed": "ошибка",
}
