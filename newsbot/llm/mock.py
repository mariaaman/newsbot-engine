"""Mock provider for --dry-run and tests: no network, deterministic.
responses: task -> str | dict/list (json-dumped) | callable(messages)->str|dict | list (queue, popped in order;
last item repeats). '*' is the default. Unknown task returns '{}'."""
from __future__ import annotations

import json

from newsbot.llm.base import Response, estimate_tokens


class MockProvider:
    name = "mock"

    def __init__(self, responses: dict | None = None, models: list[str] | None = None):
        self.responses = responses or {}
        self.models = models
        self.calls: list[dict] = []   # {task, model, n_messages} only: never store prompts

    def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task="") -> Response:
        self.calls.append({"task": task, "model": model, "n_messages": len(messages)})
        r = self.responses.get(task, self.responses.get("*", {}))
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(messages)
        text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        return Response(text, estimate_tokens(messages), max(1, len(text) // 3), model, self.name)

    def list_models(self) -> list[str]:
        return self.models if self.models is not None else ["*"]
