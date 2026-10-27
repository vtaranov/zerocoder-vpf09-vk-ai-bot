"""Контекст только активного диалога и его текущая роль."""

from modes import system_prompt


class AIService:
    def __init__(self, client, store, model, max_tokens=800):
        self.client, self.store = client, store
        self.model, self.max_tokens = model, max_tokens

    def answer(self, user, question):
        dialog = self.store.active(user)
        # 98 прежних реплик + новый вопрос + будущий ответ = 100.
        messages = [{"role": "system", "content": system_prompt(dialog["modeId"])}]
        messages.extend({"role": m["role"], "content": m["content"]}
                        for m in dialog["messages"][-98:])
        messages.append({"role": "user", "content": question})
        response = self.client.chat(model=self.model, messages=messages, max_tokens=self.max_tokens)
        self.store.append(user, question, response)
        return response
