"""VK-интерфейс базового AI-бота VPf09 (VKBottle LongPoll)."""

import asyncio
import fcntl
import json
import logging
from collections import defaultdict
from dataclasses import dataclass

from vkbottle import Keyboard, Text
from vkbottle.bot import Bot, Message

from config import ROOT, Settings
from modes import MODES
from proxyapi_client import ProxyAPIClient, ProxyAPIError
from service import AIService
from storage import UserStore


def button(label, action, value=None):
    payload = {"action": action}
    if value is not None:
        payload["value"] = value
    return label, payload


MENU = [
    [button("Новый диалог", "new"), button("Список диалогов", "list")],
    [button("Сменить режим", "modes"), button("Настройки", "settings")],
    [button("Помощь", "help")],
]
BACK = [[button("Меню", "menu")]]
TEXT_ACTIONS = {"начать": "menu", "/start": "menu", "меню": "menu",
                "новый диалог": "new", "список диалогов": "list",
                "сменить режим": "modes", "настройки": "settings", "помощь": "help"}


@dataclass
class Reply:
    text: str
    rows: list


def keyboard(rows):
    result = Keyboard(one_time=False)
    for i, row in enumerate(rows):
        if i:
            result.row()
        for label, payload in row:
            result.add(Text(label, payload=payload))
    return result.get_json()


class Conversation:
    def __init__(self, store, ai):
        self.store, self.ai = store, ai
        self.pending = {}
        self.locks = defaultdict(asyncio.Lock)

    def overview(self, user, prefix="Главное меню"):
        dialog = self.store.active(user)
        return Reply(f"{prefix}\nДиалог: {dialog['name']}\nРежим: {MODES[dialog['modeId']][0]}", MENU)

    async def handle(self, user_id, text, payload=None):
        async with self.locks[user_id]:
            return await self._handle(user_id, (text or "").strip(), payload or {})

    async def _handle(self, user_id, text, payload):
        user = self.store.load(user_id)
        dialog = self.store.active(user)
        action = payload.get("action") or TEXT_ACTIONS.get(text.lower())
        value = payload.get("value")
        if action:
            self.pending.pop(user_id, None)
        elif user_id in self.pending:
            if not text or len(text) > 60:
                return Reply("Введите название длиной от 1 до 60 символов.", BACK)
            pending = self.pending.pop(user_id)
            if pending == "new":
                self.store.create(user, text)
            else:
                dialog["name"] = text
                self.store.save(user)
            return self.overview(user, "Диалог сохранён")

        if action == "menu":
            return self.overview(user)
        if action == "new":
            self.pending[user_id] = "new"
            return Reply("Как назвать новый диалог? Напишите название или нажмите «Меню» для отмены.", BACK)
        if action == "rename":
            self.pending[user_id] = "rename"
            return Reply("Введите новое название текущего диалога.", BACK)
        if action == "list":
            dialogs = list(user["dialogs"].values())
            page = int(value or 0)
            page = min(max(page, 0), (len(dialogs) - 1) // 8)
            rows, lines = [], ["Список диалогов:"]
            for item in dialogs[page * 8:page * 8 + 8]:
                active = "✓ " if item["id"] == user["active_dialog"] else ""
                lines.append(f"{active}{item['name']} — {MODES[item['modeId']][0]}, сообщений: {len(item['messages'])}")
                rows.append([button((active + item["name"])[:40], "open", item["id"])])
            navigation = []
            if page:
                navigation.append(button("Назад", "list", page - 1))
            if (page + 1) * 8 < len(dialogs):
                navigation.append(button("Далее", "list", page + 1))
            if navigation:
                rows.append(navigation)
            return Reply("\n".join(lines), rows + BACK)
        if action == "open":
            if value not in user["dialogs"]:
                return Reply("Диалог не найден. Откройте список заново.", MENU)
            user["active_dialog"] = value
            self.store.save(user)
            return self.overview(user, "Диалог выбран")
        if action == "modes":
            return Reply("Выберите режим текущего диалога:",
                         [[button(label, "mode", mode)] for mode, (label, _) in MODES.items()] + BACK)
        if action == "mode":
            if value not in MODES:
                return Reply("Режим не найден.", MENU)
            dialog["modeId"] = value
            self.store.save(user)
            return self.overview(user, "Режим изменён, контекст сохранён")
        if action == "settings":
            usage = user["usage"]
            note = " (часть запросов без счётчика)" if usage["unmetered_requests"] else ""
            return Reply(
                f"Диалог: {dialog['name']}\nРежим: {MODES[dialog['modeId']][0]}\n"
                f"Модель: {self.ai.model}\nЗапросов: {usage['requests']}\n"
                f"Токены: {usage['total_tokens']}{note}\n"
                f"Вход: {usage['prompt_tokens']}; выход: {usage['completion_tokens']}",
                [[button("Переименовать", "rename")], [button("Очистить историю", "clear")],
                 [button("Удалить диалог", "delete")]] + BACK)
        if action == "clear":
            dialog["messages"] = []
            self.store.save(user)
            return self.overview(user, "История текущего диалога очищена")
        if action == "delete":
            self.store.delete_active(user)
            return self.overview(user, "Диалог удалён")
        if action == "help":
            return Reply("Напишите вопрос — я отвечу с учётом текущего диалога.\n"
                         "«Сменить режим» меняет роль, сохраняя контекст.\n"
                         "«Новый диалог» начинает отдельную беседу; старые доступны в списке.\n"
                         "В настройках — модель, токены, переименование и очистка истории.\n"
                         "Последние 100 сообщений сохраняются после перезапуска.", MENU)
        if action:
            return Reply("Команда устарела. Выберите действие в меню.", MENU)
        if not text:
            return Reply("Отправьте текстовый вопрос.", MENU)
        if len(text) > 8000:
            return Reply("Сократите сообщение до 8000 символов.", MENU)
        try:
            response = await asyncio.to_thread(self.ai.answer, user, text)
        except (ProxyAPIError, OSError):
            logging.warning("Ошибка ответа модели; история запроса не добавлена")
            return Reply("Не удалось получить или сохранить ответ. Попробуйте ещё раз.", MENU)
        return Reply(response.text, MENU)


def create_bot(settings):
    store = UserStore(ROOT / "users")
    client = ProxyAPIClient(settings.api_key, settings.base_url, 60)
    conversation = Conversation(store, AIService(client, store, settings.model, settings.max_tokens))
    bot = Bot(token=settings.vk_token)

    @bot.on.private_message()
    async def handler(message: Message):
        try:
            payload = json.loads(message.payload) if message.payload else {}
            if not isinstance(payload, dict):
                payload = {}
            reply = await conversation.handle(message.from_id, message.text, payload)
        except (ValueError, KeyError, TypeError, OSError):
            logging.warning("Ошибка состояния пользователя; исходный JSON сохранён")
            reply = Reply("Не удалось прочитать состояние. Сообщите владельцу бота.", MENU)
        chunks = [reply.text[i:i + 3500] for i in range(0, len(reply.text), 3500)]
        for index, chunk in enumerate(chunks):
            kwargs = {"keyboard": keyboard(reply.rows)} if index == len(chunks) - 1 else {}
            await message.answer(chunk, **kwargs)

    return bot


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    with (ROOT / ".bot.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Этот бот уже запущен")
        logging.info("VPf09: VK LongPoll, модель %s", settings.model)
        create_bot(settings).run()


if __name__ == "__main__":
    main()
