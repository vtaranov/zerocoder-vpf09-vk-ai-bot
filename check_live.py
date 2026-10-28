"""Реальная проверка ProxyAPI, памяти и ролей; расходует API-токены."""

import json
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from config import ROOT, Settings
from proxyapi_client import ProxyAPIClient
from service import AIService
from storage import UserStore


def main():
    settings = Settings.load()
    client = ProxyAPIClient(settings.api_key, settings.base_url, 60)
    evidence = {"created_at": datetime.now(timezone.utc).isoformat(),
                "model": settings.model, "transport": "ProxyAPI, без VK", "checks": []}
    with tempfile.TemporaryDirectory() as tmp:
        store = UserStore(Path(tmp) / "users")
        ai = AIService(client, store, settings.model, settings.max_tokens)
        user = store.load(1)
        first = user["active_dialog"]

        def ask(name, question, expected):
            response = ai.answer(user, question)
            ok = expected.lower() in response.text.lower()
            evidence["checks"].append({"name": name, "question": question,
                                       "answer": response.text, "usage": asdict(response.usage),
                                       "model": response.model, "passed": ok})
            print(name, "OK" if ok else "FAIL", response.usage.total_tokens, flush=True)
            if not ok:
                raise AssertionError(name)

        ask("Ответ модели", "Запомни: кодовое слово моего диалога — кедр-742. Ответь только: принято.", "принято")
        ask("Контекст", "Какое кодовое слово я назвал? Ответь только словом и числом.", "кедр-742")
        user = UserStore(store.directory).load(1)
        ask("Перезапуск JSON", "Повтори моё кодовое слово без пояснений.", "кедр-742")
        store.active(user)["modeId"] = "teacher"
        store.save(user)
        ask("Смена роли без потери контекста", "Как преподаватель объясни, что такое переменная в Python. В конце повтори моё кодовое слово.", "кедр-742")
        store.create(user, "Отдельный")
        ask("Изоляция нового диалога", "Если в этой беседе я ещё не называл кодовое слово, ответь только НЕИЗВЕСТНО. Иначе назови его.", "неизвестно")
        user["active_dialog"] = first
        store.save(user)
        ask("Возврат к первому диалогу", "Повтори моё кодовое слово без пояснений.", "кедр-742")
        evidence["total_usage"] = user["usage"]
        evidence["json_persistence_verified"] = (store.directory / "1.json").exists()
    output = ROOT / "artifacts" / "proxyapi-live-check.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Итого:", evidence["total_usage"], flush=True)


if __name__ == "__main__":
    main()
