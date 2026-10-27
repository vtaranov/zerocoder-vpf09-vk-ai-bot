"""Локальные настройки бота; секреты не входят в репозиторий."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    vk_token: str
    api_key: str
    model: str
    base_url: str
    max_tokens: int

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        missing = [k for k in ("VK_BOT_TOKEN", "PROXYAPI_KEY") if not os.getenv(k, "").strip()]
        if missing:
            raise ValueError("Заполните .env: " + ", ".join(missing))
        return cls(
            os.environ["VK_BOT_TOKEN"].strip(), os.environ["PROXYAPI_KEY"].strip(),
            os.getenv("MODEL", "openai/gpt-6-luna"),
            os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru/v1"),
            int(os.getenv("MAX_OUTPUT_TOKENS", "800")),
        )
