"""Контролируемый запуск LongPoll без частых обновлений сервера."""

import asyncio
import logging
import time

from aiohttp import ClientError
from vkbottle.exception_factory import VKAPIError
from vkbottle.polling.bot_polling import BotPolling


class Polling(BotPolling):
    last_refresh = 0.0
    refreshes_without_events = 0
    channel_confirmed = False

    async def get_server(self):
        if self.refreshes_without_events >= 1:
            logging.warning("Новая сессия LongPoll не принята сервером; пауза перед обновлением 300 секунд")
            await asyncio.sleep(300)
        delay = max(0, 10 - (time.monotonic() - self.last_refresh))
        if delay:
            await asyncio.sleep(delay)
        self.last_refresh = time.monotonic()
        server = await asyncio.wait_for(super().get_server(), timeout=30)
        self.refreshes_without_events += 1
        logging.info("Параметры LongPoll получены, группа %s; канал ещё не подтверждён", self.group_id)
        return server

    async def get_event(self, server):
        event = await super().get_event(server)
        if "ts" in event and "failed" not in event:
            self.refreshes_without_events = 0
            if not self.channel_confirmed:
                logging.info("LongPoll отвечает корректно; приём событий подтверждён")
                self.channel_confirmed = True
        else:
            self.channel_confirmed = False
        return event


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
