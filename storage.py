"""Отдельный JSON на пользователя, независимые диалоги и атомарная запись."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def new_dialog(name="Основной диалог"):
    return {"id": uuid4().hex[:12], "name": name, "modeId": "assistant", "messages": []}


class UserStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, user_id):
        if int(user_id) <= 0:
            raise ValueError("Ожидается положительный VK user_id")
        return self.directory / f"{int(user_id)}.json"

    def load(self, user_id):
        path = self.path(user_id)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["active_dialog"] not in data["dialogs"]:
                raise ValueError("Активный диалог отсутствует в JSON")
            return data
        dialog = new_dialog()
        data = {"user_id": int(user_id), "active_dialog": dialog["id"],
                "dialogs": {dialog["id"]: dialog},
                "usage": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "unmetered_requests": 0}}
        self.save(data)
        return data

    def save(self, user):
        path = self.path(user["user_id"])
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as f:
            json.dump(user, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)

    @staticmethod
    def active(user):
        return user["dialogs"][user["active_dialog"]]

    def create(self, user, name):
        dialog = new_dialog(name)
        user["dialogs"][dialog["id"]] = dialog
        user["active_dialog"] = dialog["id"]
        self.save(user)

    def delete_active(self, user):
        del user["dialogs"][user["active_dialog"]]
        if not user["dialogs"]:
            dialog = new_dialog()
            user["dialogs"][dialog["id"]] = dialog
        user["active_dialog"] = next(iter(user["dialogs"]))
        self.save(user)

    def append(self, user, question, response):
        dialog = self.active(user)
        now = datetime.now(timezone.utc).isoformat()
        dialog["messages"].extend([
            {"role": "user", "content": question, "timestamp": now},
            {"role": "assistant", "content": response.text, "timestamp": now},
        ])
        dialog["messages"] = dialog["messages"][-100:]
        usage = user["usage"]
        usage["requests"] += 1
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(response.usage, field)
            if value is not None:
                usage[field] += value
        if response.usage.total_tokens is None:
            usage["unmetered_requests"] += 1
        self.save(user)
