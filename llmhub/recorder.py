"""What went to a model and what came back, while someone is watching.

Deliberately not in the database. Prompts here are the payload of other projects - transcripts,
warehouse data - and the hub already learned what happens when high-volume content lands in
SQLite: at the observed rate this would be gigabytes a day. So the log lives in memory, is armed
by hand for a bounded window, is cleared every time recording starts, and dies with the process.

Reading it is the one thing in the console that needs the token even on a read. Every other view
shows counts and status, which are dull to a passer-by on the LAN; this one shows the text.

One entry is one attempt. It is opened when the attempt is sent, so the console can show the
prompt while the model is still thinking, and closed when the answer, the refusal or the end of
the stream arrives. Attempts of one request share a `request_id`, so a fallback reads as one
prompt with several answers under it.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import to_iso

DEFAULT_MINUTES = 20
MAX_MINUTES = 120
# a window this size at the observed call rate is thousands of entries; the ring keeps the
# newest and the page stays readable
MAX_ENTRIES = 200
# per message, and per answer. Enough to see the instruction and the shape of the answer, not
# enough for one 460 KB transcript to own the whole buffer.
MAX_CHARS = 6000
# per entry, across all of its messages; the newest messages are the ones kept
MAX_PROMPT_CHARS = 24000
TRUNCATED = "\n[... truncated by the recorder ...]\n"

QUEUED = "queued"
PENDING = "pending"
STREAMING = "streaming"
CANCELLED = "cancelled"
NOT_SENT = "not sent"
OPEN_STATUSES = (QUEUED, PENDING, STREAMING)


def _clip(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    """Head and tail of a long text: the instruction is as often at the end as at the start."""
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + TRUNCATED + text[-tail:], True


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        # a vision request: parts, only some of which are text
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
        ]
        text = "\n".join(part for part in parts if part)
        if len(parts) != len(content):
            text = f"{text}\n[non-text parts: {len(content) - len(parts)}]".strip()
        return text
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return str(content or "")


def messages_of(body: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The messages the model sees, clipped one by one and then as a whole."""
    raw = body.get("messages")
    if not isinstance(raw, list):
        return [], False
    messages: list[dict[str, Any]] = []
    cut = False
    for message in raw:
        if not isinstance(message, dict):
            continue
        text = _text_of(message.get("content"))
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            names = [
                str((call.get("function") or {}).get("name") or "?")
                for call in calls
                if isinstance(call, dict)
            ]
            text = f"{text}\n[tool calls: {', '.join(names)}]".strip()
        text, clipped = _clip(text)
        cut = cut or clipped
        messages.append({"role": str(message.get("role") or "?"), "text": text})

    total = 0
    kept: list[dict[str, Any]] = []
    for message in reversed(messages):
        total += len(message["text"])
        if total > MAX_PROMPT_CHARS and kept:
            kept.append(
                {"role": "recorder", "text": f"[{len(messages) - len(kept)} earlier messages not kept]"}
            )
            cut = True
            break
        kept.append(message)
    return list(reversed(kept)), cut


def prompt_of(body: dict[str, Any]) -> str:
    """The messages as one readable block, in the order the model sees them."""
    messages, _ = messages_of(body)
    return "\n\n".join(f"{m['role']}: {m['text']}" for m in messages)


def answer_parts(payload: Any) -> tuple[str, str]:
    """The assistant's text and, when the vendor reports it separately, its reasoning."""
    if not isinstance(payload, dict):
        return "", ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return "", ""
    content = str(message.get("content") or "")
    calls = message.get("tool_calls")
    if isinstance(calls, list) and calls:
        names = [
            str((call.get("function") or {}).get("name") or "?") for call in calls if isinstance(call, dict)
        ]
        content = f"{content}\n[tool calls: {', '.join(names)}]".strip()
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")
    return content, reasoning


def answer_of(payload: Any) -> str:
    content, reasoning = answer_parts(payload)
    if reasoning:
        return f"{content}\n\n[reasoning]\n{reasoning}".strip()
    return content


def tokens_of(payload: Any) -> dict[str, int] | None:
    """What the vendor billed, in the shape the console shows; None when it did not say."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict) or not usage:
        return None
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    out_details = usage.get("completion_tokens_details")
    reasoning = int(out_details.get("reasoning_tokens") or 0) if isinstance(out_details, dict) else 0
    return {
        "in": prompt,
        "out": completion,
        "total": int(usage.get("total_tokens") or (prompt + completion)),
        "cached": cached,
        "reasoning": reasoning,
    }


def estimated_tokens(messages: list[dict[str, Any]], answer: str) -> dict[str, Any]:
    chars_in = sum(len(m["text"]) for m in messages)
    return {
        "in": chars_in // 4,
        "out": len(answer) // 4,
        "total": (chars_in + len(answer)) // 4,
        "estimated": True,
    }


@dataclass
class Entry:
    id: int
    request_id: int
    attempt: int
    rev: int
    sent_at: str
    app: str
    model_request: str
    model: str
    account: str
    kind: str
    messages: list[dict[str, Any]]
    status: str = PENDING
    started_at: str | None = None
    queued_ms: int = 0
    ts: str | None = None
    answer: str = ""
    reasoning: str = ""
    latency_ms: int = 0
    first_ms: int | None = None
    tokens: dict[str, Any] | None = None
    truncated: bool = False
    started: float = field(default=0.0, repr=False)

    @property
    def prompt(self) -> str:
        return "\n\n".join(f"{m['role']}: {m['text']}" for m in self.messages)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "rev": self.rev,
            "sent_at": self.sent_at,
            "started_at": self.started_at or self.sent_at,
            "queued_ms": self.queued_ms,
            "ts": self.ts,
            "app": self.app,
            "model_request": self.model_request,
            "model": self.model,
            "account": self.account,
            "kind": self.kind,
            "messages": self.messages,
            "prompt": self.prompt,
            "answer": self.answer,
            "reasoning": self.reasoning,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "first_ms": self.first_ms,
            "tokens": self.tokens,
            "truncated": self.truncated,
        }


@dataclass
class Recorder:
    """Off until someone turns it on, and off again when the window runs out."""

    until: datetime | None = None
    started_at: datetime | None = None
    entries: deque[Entry] = field(default_factory=lambda: deque(maxlen=MAX_ENTRIES))
    dropped: int = 0
    rev: int = 0
    _ids: Any = field(default_factory=lambda: itertools.count(1), repr=False)
    _requests: Any = field(default_factory=lambda: itertools.count(1), repr=False)

    def start(self, minutes: int = DEFAULT_MINUTES, now: datetime | None = None) -> dict[str, Any]:
        """Arm for `minutes`, and clear whatever the last run left behind."""
        window = max(1, min(int(minutes), MAX_MINUTES))
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        self.entries.clear()
        self.dropped = 0
        self.started_at = moment
        self.until = moment + timedelta(minutes=window)
        self.rev += 1
        return self.status(moment)

    def stop(self, now: datetime | None = None) -> dict[str, Any]:
        """Stop recording but keep what was caught, so it can still be read."""
        self.until = None
        self.rev += 1
        return self.status(now)

    def recording(self, now: datetime | None = None) -> bool:
        if self.until is None:
            return False
        return (now or datetime.now(UTC)).astimezone(UTC) < self.until

    def next_request(self) -> int:
        return next(self._requests)

    def begin(
        self,
        *,
        app: str,
        model_request: str,
        entry_key: str,
        account: str,
        kind: str,
        body: dict[str, Any],
        request_id: int | None = None,
        attempt: int = 1,
        status: str = PENDING,
        now: datetime | None = None,
    ) -> Entry | None:
        """Open an entry as the attempt is sent, or as the request arrives (`queued`).

        None while the recorder is off.
        """
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        if not self.recording(moment):
            return None
        messages, cut = messages_of(body)
        if len(self.entries) == self.entries.maxlen:
            self.dropped += 1
        self.rev += 1
        entry = Entry(
            id=next(self._ids),
            request_id=request_id if request_id is not None else self.next_request(),
            attempt=attempt,
            rev=self.rev,
            sent_at=to_iso(moment),
            app=app,
            model_request=model_request,
            model=entry_key,
            account=account,
            kind=kind,
            messages=messages,
            status=status,
            truncated=cut,
            started=time.monotonic(),
        )
        self.entries.append(entry)
        return entry

    def finish(
        self,
        entry: Entry,
        *,
        status: str,
        answer: str = "",
        reasoning: str = "",
        latency_ms: int = 0,
        tokens: dict[str, Any] | None = None,
        first_ms: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Close an entry with what came back. A closed entry is not reopened."""
        if entry.status not in OPEN_STATUSES:
            return
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        answer, answer_cut = _clip(answer)
        reasoning, reasoning_cut = _clip(reasoning)
        waited = int((time.monotonic() - entry.started) * 1000) if entry.started else 0
        self.rev += 1
        entry.rev = self.rev
        entry.status = status
        entry.ts = to_iso(moment)
        entry.answer = answer
        entry.reasoning = reasoning
        entry.latency_ms = latency_ms or waited
        if first_ms is not None:
            entry.first_ms = first_ms
        entry.tokens = tokens or (estimated_tokens(entry.messages, answer) if status == "ok" else None)
        entry.truncated = entry.truncated or answer_cut or reasoning_cut

    def claim(self, entry: Entry, *, entry_key: str, account: str, now: datetime | None = None) -> None:
        """A queued request got its first candidate: the wait so far is the queue, not the model."""
        if entry.status != QUEUED:
            return
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        self.rev += 1
        entry.rev = self.rev
        entry.status = PENDING
        entry.model = entry_key
        entry.account = account
        entry.queued_ms = int((time.monotonic() - entry.started) * 1000) if entry.started else 0
        entry.started_at = to_iso(moment)
        entry.started = time.monotonic()

    def streaming(self, entry: Entry, first_ms: int) -> None:
        """The headers are back and the body is on its way to the caller."""
        if entry.status != PENDING:
            return
        self.rev += 1
        entry.rev = self.rev
        entry.status = STREAMING
        entry.first_ms = first_ms

    def note(
        self,
        *,
        app: str,
        model_request: str,
        entry_key: str,
        account: str,
        kind: str,
        body: dict[str, Any],
        payload: Any,
        status: str,
        latency_ms: int,
        answer: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """One finished attempt in one call: `begin` and `finish` together.

        `answer` overrides what would be read out of `payload`: a refused attempt has no
        payload to read, and what the vendor said instead is the whole point of recording it.
        """
        entry = self.begin(
            app=app,
            model_request=model_request,
            entry_key=entry_key,
            account=account,
            kind=kind,
            body=body,
            now=now,
        )
        if entry is None:
            return
        content, reasoning = answer_parts(payload)
        self.finish(
            entry,
            status=status,
            answer=answer if answer is not None else content,
            reasoning="" if answer is not None else reasoning,
            latency_ms=latency_ms,
            tokens=tokens_of(payload),
            now=now,
        )

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        recording = self.recording(moment)
        left = int((self.until - moment).total_seconds()) if recording and self.until else 0
        return {
            "recording": recording,
            "started_at": to_iso(self.started_at) if self.started_at else None,
            "until": to_iso(self.until) if self.until else None,
            "seconds_left": max(0, left),
            "count": len(self.entries),
            "dropped": self.dropped,
            "max_entries": MAX_ENTRIES,
            "default_minutes": DEFAULT_MINUTES,
            "max_minutes": MAX_MINUTES,
            "rev": self.rev,
            "first_id": self.entries[0].id if self.entries else None,
        }

    def dump(self, now: datetime | None = None, since: int | None = None) -> dict[str, Any]:
        """Newest first. With `since`, only the entries opened or changed after that rev.

        The console polls with the rev it last saw, so an answer arriving in one pane does not
        make the whole page redraw - which is what reset the scroll of every other pane.
        """
        rows = self.entries if since is None else [item for item in self.entries if item.rev > since]
        return {
            **self.status(now),
            "since": since,
            "entries": [item.as_dict() for item in reversed(rows)],
        }


def recorder_of(hub: Any) -> Recorder | None:
    recorder = getattr(hub, "recorder", None)
    if recorder is None or not recorder.recording():
        return None
    return recorder


def begin_request(
    hub: Any, body: dict[str, Any], app: str, kind: str, request_id: int | None
) -> Entry | None:
    """Adapter for the gateway: open an entry as the request arrives, before any slot is free.

    A call can stand in line for minutes behind a busy pair; without this the console shows
    nothing until the vendor is asked, and the prompt turns up long after the app sent it.
    """
    recorder = recorder_of(hub)
    if recorder is None:
        return None
    try:
        return recorder.begin(
            app=app,
            model_request=str(body.get("model") or ""),
            entry_key="",
            account="",
            kind=kind,
            body=body,
            request_id=request_id,
            attempt=1,
            status=QUEUED,
        )
    except Exception:  # noqa: BLE001
        return None


def close_request(hub: Any, queued: Entry | None, exc: BaseException) -> None:
    """The request ended before its first attempt was sent: say why, rather than wait forever."""
    recorder = getattr(hub, "recorder", None)
    if recorder is None or queued is None or queued.status != QUEUED:
        return
    try:
        if isinstance(exc, asyncio.CancelledError):
            recorder.finish(queued, status=CANCELLED, answer="cancelled while waiting for a slot")
            return
        attempts = getattr(exc, "attempts", None) or []
        skipped = ", ".join(
            f"{row.get('model')}: {row.get('error_code') or row.get('status')}" for row in attempts
        )
        answer = f"{type(exc).__name__}: {exc}" + (f"\n{skipped}" if skipped else "")
        recorder.finish(queued, status=NOT_SENT, answer=answer)
    except Exception:  # noqa: BLE001
        return


def begin_call(
    hub: Any,
    entry: Any,
    body: dict[str, Any],
    app: str,
    kind: str,
    *,
    request_id: int | None = None,
    attempt: int = 1,
    queued: Entry | None = None,
) -> Entry | None:
    """Adapter for the gateway: open an entry for one attempt. Never raises.

    The first attempt takes over the entry `begin_request` opened, so a request reads as one
    prompt whose wait splits into the queue and the model.
    """
    recorder = recorder_of(hub)
    if recorder is None:
        return None
    try:
        if queued is not None and queued.status == QUEUED:
            recorder.claim(
                queued, entry_key=getattr(entry, "key", ""), account=getattr(entry, "account_id", "")
            )
            return queued
        return recorder.begin(
            app=app,
            model_request=str(body.get("model") or ""),
            entry_key=getattr(entry, "key", ""),
            account=getattr(entry, "account_id", ""),
            kind=kind,
            body=body,
            request_id=request_id,
            attempt=attempt,
        )
    except Exception:  # noqa: BLE001
        return None


def note_call(
    hub: Any,
    entry: Any,
    body: dict[str, Any],
    app: str,
    kind: str,
    result: Any,
    *,
    opened: Entry | None = None,
) -> None:
    """Adapter for the gateway: pull what the recorder wants out of one attempt's result.

    A stream is left open: its answer is still on the wire, and `note_stream_end` closes it
    once the last chunk has passed. Never raises. A debugging aid that can break a served
    request is worse than no aid.
    """
    recorder = getattr(hub, "recorder", None)
    if recorder is None:
        return
    try:
        if opened is None:
            opened = begin_call(hub, entry, body, app, kind)
        if opened is None:
            return
        payload = getattr(result, "payload", None)
        latency_ms = int(getattr(result, "latency_ms", 0) or 0)
        extra = getattr(result, "extra", None)
        if payload is None and getattr(result, "response", None) is not None:
            recorder.streaming(opened, latency_ms)
            if isinstance(extra, dict):
                extra["recorder_entry"] = opened
            return
        code = int(getattr(result, "status_code", 0) or 0)
        content, reasoning = answer_parts(payload)
        recorder.finish(
            opened,
            status="ok" if code < 400 else str(code),
            answer=content,
            reasoning=reasoning,
            latency_ms=latency_ms,
            tokens=tokens_of(payload),
        )
    except Exception:  # noqa: BLE001
        return


def note_stream_end(
    hub: Any, opened: Entry | None, text: str, reasoning: str, usage: Any, latency_ms: int
) -> None:
    """The last chunk of a stream has gone to the caller: close its entry with what it said."""
    recorder = getattr(hub, "recorder", None)
    if recorder is None or opened is None:
        return
    try:
        recorder.finish(
            opened,
            status="ok",
            answer=text,
            reasoning=reasoning,
            latency_ms=latency_ms,
            tokens=tokens_of(usage),
        )
    except Exception:  # noqa: BLE001
        return


def note_failure(
    hub: Any, entry: Any, body: dict[str, Any], app: str, kind: str, exc: Any, *, opened: Entry | None = None
) -> None:
    """A refused attempt, which is the case worth recording most.

    An attempt that fails raises out of the backend, so the success path never sees it. Without
    this, a recorder armed during a quota storm reports that nothing went through the hub - the
    exact question the owner opened it to answer.
    """
    recorder = getattr(hub, "recorder", None)
    if recorder is None:
        return
    try:
        if opened is None:
            opened = begin_call(hub, entry, body, app, kind)
        if opened is None:
            return
        if isinstance(exc, asyncio.CancelledError):
            recorder.finish(opened, status=CANCELLED, answer="cancelled before the model answered")
            return
        classification = getattr(exc, "classification", None)
        label = str(getattr(classification, "kind", "") or "error")
        code = getattr(classification, "code", None) or getattr(exc, "status_code", None)
        message = str(getattr(classification, "message", "") or getattr(exc, "detail", "") or exc or "")
        recorder.finish(
            opened,
            status=f"{label} {code}".strip() if code else label,
            answer=f"{label}: {code}\n{message}".strip() if code else f"{label}: {message}".strip(),
            latency_ms=int(getattr(exc, "latency_ms", 0) or 0),
        )
    except Exception:  # noqa: BLE001
        return


__all__ = [
    "DEFAULT_MINUTES",
    "MAX_CHARS",
    "MAX_ENTRIES",
    "MAX_MINUTES",
    "Recorder",
    "begin_call",
    "begin_request",
    "close_request",
    "note_call",
    "note_failure",
    "note_stream_end",
]
