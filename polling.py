"""Контролируемый запуск LongPoll без частых обновлений сервера."""

import asyncio
import logging
import time

from aiohttp import ClientError
from vkbottle.exception_factory import VKAPIError
from vkbottle.polling.bot_polling import BotPolling


class Polling(BotPolling):
    last_refresh = 0.0

    async def get_server(self):
        delay = max(0, 10 - (time.monotonic() - self.last_refresh))
        if delay:
            await asyncio.sleep(delay)
        self.last_refresh = time.monotonic()
        server = await asyncio.wait_for(super().get_server(), timeout=30)
        logging.info("LongPoll-сервер получен, группа %s: приём событий запущен", self.group_id)
        return server


async def serve(bot):
    try:
        while True:
            try:
                await bot.run_polling()
                return
            except VKAPIError[29]:
                logging.warning("VK API 29: достигнут лимит. LongPoll недоступен; повтор через 300 секунд")
                await asyncio.sleep(300)
            except (VKAPIError[5], VKAPIError[27]):
                raise SystemExit("VK отклонил токен сообщества; проверьте локальный .env")
            except (VKAPIError, ClientError, OSError):
                logging.warning("Ошибка подключения LongPoll; повтор через 30 секунд")
                await asyncio.sleep(30)
    finally:
        await bot.api.http_client.close()
