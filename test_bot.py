"""Проверки требований VPf09 без запросов к платному API."""

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

from bot import Conversation, MENU, keyboard, create_bot
from config import Settings
from polling import serve
from vkbottle.exception_factory import VKAPIError
from modes import MODES, system_prompt
from proxyapi_client import AssistantResponse, ProxyAPIError, Usage, parse_response
from service import AIService
from storage import UserStore


def answer(text="Ответ"):
    return AssistantResponse(text, "test", "stop", 0.1, Usage(10, 3, 13, None))


class Setup:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UserStore(Path(self.tmp.name) / "users")
        self.client = Mock()
        self.client.chat.return_value = answer()
        self.ai = AIService(self.client, self.store, "test")


class StorageTests(Setup, unittest.TestCase):
    def test_creates_separate_json_and_reloads(self):
        user = self.store.load(10)
        self.ai.answer(user, "Привет")
        reloaded = UserStore(self.store.directory).load(10)
        self.assertEqual(reloaded["user_id"], 10)
        self.assertEqual(self.store.active(reloaded)["messages"][0]["content"], "Привет")
        self.assertTrue((self.store.directory / "10.json").exists())
        self.assertEqual(reloaded["usage"]["total_tokens"], 13)

    def test_users_are_isolated(self):
        self.ai.answer(self.store.load(10), "Секрет пользователя A")
        self.ai.answer(self.store.load(20), "Вопрос B")
        sent = self.client.chat.call_args.kwargs["messages"]
        self.assertNotIn("Секрет пользователя A", str(sent))

    def test_dialogs_are_isolated_and_recover_context(self):
        user = self.store.load(10)
        first = user["active_dialog"]
        self.ai.answer(user, "Кодовое слово: сосна")
        self.store.create(user, "Вторая беседа")
        self.ai.answer(user, "Какое слово?")
        self.assertNotIn("сосна", str(self.client.chat.call_args.kwargs["messages"]))
        user["active_dialog"] = first
        self.ai.answer(user, "Повтори слово")
        self.assertIn("сосна", str(self.client.chat.call_args.kwargs["messages"]))

    def test_role_changes_system_prompt_and_keeps_history(self):
        user = self.store.load(10)
        self.ai.answer(user, "Меня зовут Виктор")
        self.store.active(user)["modeId"] = "teacher"
        self.ai.answer(user, "Как меня зовут?")
        sent = self.client.chat.call_args.kwargs["messages"]
        self.assertEqual(sent[0]["content"], system_prompt("teacher"))
        self.assertIn("Виктор", str(sent))

    def test_history_is_limited_to_100_complete_messages(self):
        user = self.store.load(10)
        for i in range(55):
            self.ai.answer(user, f"Вопрос {i}")
        history = self.store.active(self.store.load(10))["messages"]
        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["content"], "Вопрос 5")
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[-1]["role"], "assistant")
        sent = self.client.chat.call_args.kwargs["messages"]
        self.assertEqual(len(sent), 100)  # system + 98 истории + текущий вопрос

    def test_failed_api_does_not_change_saved_history(self):
        user = self.store.load(10)
        before = self.store.path(10).read_bytes()
        self.client.chat.side_effect = ProxyAPIError("Сбой")
        with self.assertRaises(ProxyAPIError):
            self.ai.answer(user, "Вопрос")
        self.assertEqual(self.store.path(10).read_bytes(), before)

    def test_corrupt_json_is_not_overwritten(self):
        self.store.path(10).write_text("broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.store.load(10)
        self.assertEqual(self.store.path(10).read_text(), "broken")

    def test_delete_last_dialog_leaves_valid_active_dialog(self):
        user = self.store.load(10)
        self.store.delete_active(user)
        self.assertEqual(len(user["dialogs"]), 1)
        self.assertIn(user["active_dialog"], user["dialogs"])


class InterfaceTests(Setup, unittest.IsolatedAsyncioTestCase):
    async def test_menu_create_list_switch_rename(self):
        app = Conversation(self.store, self.ai)
        reply = await app.handle(10, "Начать")
        self.assertIn("Главное меню", reply.text)
        first = self.store.load(10)["active_dialog"]
        await app.handle(10, "Новый диалог")
        await app.handle(10, "Учёба")
        self.assertEqual(len(self.store.load(10)["dialogs"]), 2)
        reply = await app.handle(10, "Список диалогов")
        self.assertIn("Учёба", reply.text)
        await app.handle(10, "", {"action": "open", "value": first})
        await app.handle(10, "", {"action": "rename"})
        await app.handle(10, "Основной")
        self.assertEqual(self.store.active(self.store.load(10))["name"], "Основной")

    async def test_mode_clear_settings_and_restart(self):
        app = Conversation(self.store, self.ai)
        await app.handle(10, "Запомни слово сосна")
        await app.handle(10, "", {"action": "mode", "value": "teacher"})
        app = Conversation(UserStore(self.store.directory), self.ai)
        reply = await app.handle(10, "Настройки")
        self.assertIn("Преподаватель", reply.text)
        self.assertIn("Токены: 13", reply.text)
        await app.handle(10, "", {"action": "clear"})
        self.assertEqual(self.store.active(self.store.load(10))["messages"], [])

    async def test_menu_cancels_pending_name(self):
        app = Conversation(self.store, self.ai)
        await app.handle(10, "Новый диалог")
        await app.handle(10, "Меню")
        await app.handle(10, "Вопрос")
        self.assertEqual(len(self.store.load(10)["dialogs"]), 1)
        self.assertEqual(self.client.chat.call_count, 1)

    async def test_list_pagination_and_vk_keyboard(self):
        user = self.store.load(10)
        for i in range(10):
            self.store.create(user, f"Диалог {i}")
        app = Conversation(self.store, self.ai)
        reply = await app.handle(10, "Список диалогов")
        data = json.loads(keyboard(reply.rows))
        self.assertLessEqual(len(data["buttons"]), 10)
        reply = await app.handle(10, "", {"action": "list", "value": 1})
        self.assertIn("Диалог 9", reply.text)
        self.assertLessEqual(len(json.loads(keyboard(reply.rows))["buttons"]), 10)

    async def test_stale_foreign_dialog_button_cannot_open(self):
        app = Conversation(self.store, self.ai)
        other = self.store.load(20)["active_dialog"]
        reply = await app.handle(10, "", {"action": "open", "value": other})
        self.assertIn("не найден", reply.text)
        self.assertNotEqual(self.store.load(10)["active_dialog"], other)

    async def test_ai_failure_shows_error_and_no_history(self):
        self.client.chat.side_effect = ProxyAPIError("Секретные детали")
        app = Conversation(self.store, self.ai)
        reply = await app.handle(10, "Вопрос")
        self.assertNotIn("Секретные", reply.text)
        self.assertEqual(self.store.active(self.store.load(10))["messages"], [])


class UsageTests(unittest.TestCase):
    def test_provider_usage_is_parsed(self):
        response = parse_response({"model": "test", "choices": [{"message": {"content": "OK"},
                                    "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 17, "completion_tokens": 2,
                                             "total_tokens": 19}}, 0)
        self.assertEqual(response.usage.total_tokens, 19)

    def test_all_five_roles_have_distinct_instructions(self):
        self.assertEqual(len({system_prompt(mode) for mode in MODES}), 5)
        self.assertEqual(len(json.loads(keyboard(MENU))["buttons"]), 3)


class TransportTests(Setup, unittest.IsolatedAsyncioTestCase):
    async def test_vk_message_new_reaches_menu_handler(self):
        settings = Settings("test-token", "test-key", "test", "https://example.invalid", 100)
        with patch("bot.ROOT", Path(self.tmp.name)):
            bot = create_bot(settings)
        bot.api.request = AsyncMock(return_value={"response": [
            {"peer_id": 10, "message_id": 42, "conversation_message_id": 2}]})
        event = {"type": "message_new", "group_id": 239167324, "event_id": "test-start",
                 "object": {"message": {"id": 1, "date": 1793100000, "peer_id": 10,
                                         "from_id": 10, "text": "Начать", "out": 0,
                                         "attachments": [], "conversation_message_id": 1,
                                         "fwd_messages": [], "important": False,
                                         "is_hidden": False, "random_id": 0, "version": 10000000},
                            "client_info": {"button_actions": ["text"], "keyboard": True,
                                            "inline_keyboard": True, "lang_id": 0}}}
        await bot.process_event(event, bot.api)
        calls = [call for call in bot.api.request.call_args_list if call.args[0] == "messages.send"]
        self.assertEqual(len(calls), 1)
        sent = calls[0].args[1]
        self.assertIn("Главное меню", sent["message"])
        self.assertEqual(json.loads(sent["keyboard"])["buttons"][0][0]["action"]["label"], "Новый диалог")

    async def test_vk_rate_limit_waits_and_closes_cleanly(self):
        bot = Mock()
        bot.run_polling = AsyncMock(side_effect=[VKAPIError[29](error_msg="Rate limit reached"),
                                                asyncio.CancelledError()])
        bot.api.http_client.close = AsyncMock()
        with patch("polling.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await serve(bot)
        sleep.assert_awaited_once_with(300)
        bot.api.http_client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
