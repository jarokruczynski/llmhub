from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 120.0
MAX_CONSECUTIVE_429 = 2


class LLMError(RuntimeError):
    """One call failed; the pipeline records it and carries on."""


class BudgetExhausted(LLMError):
    """The hub answered 429 twice in a row: no free room left, finish with what we have."""


class ParseError(LLMError):
    """The model did not answer with a JSON object, twice."""


@dataclass
class Reply:
    text: str
    model: str
    in_tokens: int = 0
    out_tokens: int = 0
    data: dict[str, Any] | None = None


@dataclass
class Usage:
    models: list[str] = field(default_factory=list)
    in_tokens: int = 0
    out_tokens: int = 0

    def add(self, reply: Reply) -> None:
        if reply.model and reply.model not in self.models:
            self.models.append(reply.model)
        self.in_tokens += reply.in_tokens
        self.out_tokens += reply.out_tokens

    def as_tokens(self) -> dict[str, int]:
        return {"in": self.in_tokens, "out": self.out_tokens}


def parse_first_json_object(text: str) -> dict[str, Any]:
    """First balanced {...} in the answer: models fence it, prefix it with prose, or both."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : index + 1])
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    raise ParseError("no JSON object in the answer")


class HubLLM:
    """Tiny client for the hub's own OpenAI endpoint - same routing, quota and free-only
    policy as any other caller, attributed as the `scout` app."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        app: str = "scout",
        token: str | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.app = app
        self.token = token
        self.timeout = timeout
        self.consecutive_429: dict[str, int] = {}
        self.calls = 0

    def _headers(self, prefer: str | None) -> dict[str, str]:
        headers = {"content-type": "application/json", "X-Hub-App": self.app}
        if prefer:
            headers["X-Hub-Prefer"] = prefer
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str = "fast",
        prefer: str | None = None,
        json_object: bool = False,
        max_tokens: int = 1500,
        temperature: float | None = None,
    ) -> Reply:
        # no sampling params unless a caller passes one: a hardcoded default can be a value a
        # given model route rejects outright (explabs routes some models to temperature=1.0
        # only), and the hub has no way to know that ahead of a call
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if json_object:
            body["response_format"] = {"type": "json_object"}
        response = await self._post(body, prefer)
        if response.status_code == 400 and json_object and "response_format" in response.text:
            body.pop("response_format")
            response = await self._post(body, prefer)
        if response.status_code == 429:
            count = self.consecutive_429.get(model, 0) + 1
            self.consecutive_429[model] = count
            if count >= MAX_CONSECUTIVE_429:
                raise BudgetExhausted(f"hub answered 429 {count} times in a row on {model}")
            raise LLMError(f"429 from the hub on {model}")
        self.consecutive_429[model] = 0
        if response.status_code >= 400:
            raise LLMError(f"{response.status_code} from the hub on {model}: {response.text[:200]}")

        payload = response.json()
        choices = payload.get("choices") or [{}]
        content = (choices[0].get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        return Reply(
            text=str(content),
            model=response.headers.get("x-hub-model") or str(payload.get("model") or model),
            in_tokens=int(usage.get("prompt_tokens") or 0),
            out_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def _post(self, body: dict[str, Any], prefer: str | None = None) -> httpx.Response:
        self.calls += 1
        try:
            return await self.client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(prefer),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"hub call failed: {type(exc).__name__}: {exc}".rstrip(": ")) from exc

    async def json_call(
        self,
        messages: list[dict[str, str]],
        *,
        model: str = "fast",
        prefer: str | None = None,
        max_tokens: int = 1500,
    ) -> Reply:
        """One JSON answer, with a single retry when the first one is not parseable."""
        attempt_messages = list(messages)
        last: Reply | None = None
        for attempt in (1, 2):
            reply = await self.chat(
                attempt_messages,
                model=model,
                prefer=prefer,
                json_object=True,
                max_tokens=max_tokens,
            )
            last = reply
            try:
                reply.data = parse_first_json_object(reply.text)
                return reply
            except ParseError:
                if attempt == 2:
                    break
                attempt_messages = [
                    *messages,
                    {"role": "assistant", "content": reply.text[:500]},
                    {
                        "role": "user",
                        "content": "That was not valid JSON. Answer with the JSON object only, "
                        "no prose, no code fence.",
                    },
                ]
        raise ParseError(f"unparseable JSON answer from {last.model if last else model}")
