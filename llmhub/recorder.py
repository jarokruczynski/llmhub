"""What went to a model and what came back, while someone is watching.

Deliberately not in the database. Prompts here are the payload of other projects - transcripts,
warehouse data - and the hub already learned what happens when high-volume content lands in
SQLite: at the observed rate this would be gigabytes a day. So the log lives in memory, is armed
by hand for a bounded window, is cleared every time recording starts, and dies with the process.

Reading it is the one thing in the console that needs the token even on a read. Every other view
shows counts and status, which are dull to a passer-by on the LAN; this one shows the text.
"""

from __future__ import annotations

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
# per side, per entry. Enough to see the instruction and the shape of the answer, not enough
# for one 460 KB transcript to own the whole buffer.
MAX_CHARS = 4000
TRUNCATED = "\n[... truncated by the recorder ...]"


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS] + TRUNCATED, True


def prompt_of(body: dict[str, Any]) -> str:
    """The messages as one readable block, in the order the model sees them."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "?")
        content = message.get("content")
        if isinstance(content, list):
            # a vision request: parts, only some of which are text
            parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = "\n".join(part for part in parts if part)
            if len(parts) != len(content):
                text = f"{text}\n[non-text parts: {len(content) - len(parts)}]".strip()
        else:
            text = str(content or "")
        lines.append(f"{role}: {text}")
    return "\n\n".join(lines)


def answer_of(payload: Any) -> str:
    """The assistant's text, plus its reasoning when the vendor reports one separately."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return ""
    content = str(message.get("content") or "")
    reasoning = str(message.get("reasoning") or "")
    if reasoning:
        return f"{content}\n\n[reasoning]\n{reasoning}".strip()
    return content


@dataclass
class Entry:
    ts: str
    app: str
    model_request: str
    model: str
    account: str
    kind: str
    prompt: str
    answer: str
    status: str
    latency_ms: int
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "app": self.app,
            "model_request": self.model_request,
            "model": self.model,
            "account": self.account,
            "kind": self.kind,
            "prompt": self.prompt,
            "answer": self.answer,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "truncated": self.truncated,
        }


@dataclass
class Recorder:
    """Off until someone turns it on, and off again when the window runs out."""

    until: datetime | None = None
    started_at: datetime | None = None
    entries: deque[Entry] = field(default_factory=lambda: deque(maxlen=MAX_ENTRIES))
    dropped: int = 0

    def start(self, minutes: int = DEFAULT_MINUTES, now: datetime | None = None) -> dict[str, Any]:
        """Arm for `minutes`, and clear whatever the last run left behind."""
        window = max(1, min(int(minutes), MAX_MINUTES))
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        self.entries.clear()
        self.dropped = 0
        self.started_at = moment
        self.until = moment + timedelta(minutes=window)
        return self.status(moment)

    def stop(self, now: datetime | None = None) -> dict[str, Any]:
        """Stop recording but keep what was caught, so it can still be read."""
        self.until = None
        return self.status(now)

    def recording(self, now: datetime | None = None) -> bool:
        if self.until is None:
            return False
        return (now or datetime.now(UTC)).astimezone(UTC) < self.until

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
        """Called on the hot path: cheap when off, and it never raises into a request.

        `answer` overrides what would be read out of `payload`: a refused attempt has no
        payload to read, and what the vendor said instead is the whole point of recording it.
        """
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        if not self.recording(moment):
            return
        prompt, prompt_cut = _clip(prompt_of(body))
        answer, answer_cut = _clip(answer if answer is not None else answer_of(payload))
        if len(self.entries) == self.entries.maxlen:
            self.dropped += 1
        self.entries.append(
            Entry(
                ts=to_iso(moment),
                app=app,
                model_request=model_request,
                model=entry_key,
                account=account,
                kind=kind,
                prompt=prompt,
                answer=answer,
                status=status,
                latency_ms=latency_ms,
                truncated=prompt_cut or answer_cut,
            )
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
        }

    def dump(self, now: datetime | None = None) -> dict[str, Any]:
        return {**self.status(now), "entries": [item.as_dict() for item in reversed(self.entries)]}


def note_call(hub: Any, entry: Any, body: dict[str, Any], app: str, kind: str, result: Any) -> None:
    """Adapter for the gateway: pull what the recorder wants out of one attempt's result.

    Never raises. A debugging aid that can break a served request is worse than no aid.
    """
    recorder = getattr(hub, "recorder", None)
    if recorder is None or not recorder.recording():
        return
    try:
        payload = getattr(result, "payload", None)
        status = "ok" if getattr(result, "status_code", 0) < 400 else str(getattr(result, "status_code", ""))
        recorder.note(
            app=app,
            model_request=str(body.get("model") or ""),
            entry_key=getattr(entry, "key", ""),
            account=getattr(entry, "account_id", ""),
            kind=kind,
            body=body,
            payload=payload,
            status="streamed" if payload is None and kind == "stream" else status,
            latency_ms=int(getattr(result, "latency_ms", 0) or 0),
        )
    except Exception:  # noqa: BLE001
        return


def note_failure(hub: Any, entry: Any, body: dict[str, Any], app: str, kind: str, exc: Any) -> None:
    """A refused attempt, which is the case worth recording most.

    An attempt that fails raises out of the backend, so the success path never sees it. Without
    this, a recorder armed during a quota storm reports that nothing went through the hub - the
    exact question the owner opened it to answer.
    """
    recorder = getattr(hub, "recorder", None)
    if recorder is None or not recorder.recording():
        return
    try:
        classification = getattr(exc, "classification", None)
        label = str(getattr(classification, "kind", "") or "error")
        code = getattr(classification, "code", None) or getattr(exc, "status_code", None)
        message = str(getattr(classification, "message", "") or exc or "")
        recorder.note(
            app=app,
            model_request=str(body.get("model") or ""),
            entry_key=getattr(entry, "key", ""),
            account=getattr(entry, "account_id", ""),
            kind=kind,
            body=body,
            payload=None,
            answer=f"{label}: {code}\n{message}".strip() if code else f"{label}: {message}".strip(),
            status=f"{label} {code}".strip() if code else label,
            latency_ms=int(getattr(exc, "latency_ms", 0) or 0),
        )
    except Exception:  # noqa: BLE001
        return


__all__ = ["DEFAULT_MINUTES", "MAX_ENTRIES", "MAX_MINUTES", "Recorder", "note_call", "note_failure"]
