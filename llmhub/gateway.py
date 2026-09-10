from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import require_token
from .config import CLI_KIND, Entry
from .quota import estimate_request_cost
from .router import (
    ABANDONED_STATUS,
    CANCEL_BY_OWNER,
    CANCEL_CLIENT_GONE,
    AllCandidatesFailed,
    NoCandidatesError,
    RunResult,
    UnknownModelError,
    UpstreamError,
    quota_room,
)
from .runtime import Hub
from .status import model_rows
from .vendor_errors import Classification, classify

JSON_RESPONSE_FORMATS = ("json_object", "json_schema")

log = logging.getLogger(__name__)

router = APIRouter()

SUPPORTED_KINDS = ("openai", "ollama")


@dataclass
class CallResult:
    entry: Entry
    status_code: int
    payload: dict[str, Any] | None
    raw: bytes
    latency_ms: int
    usage_id: int
    media_type: str = "application/json"


@dataclass
class StreamCall:
    entry: Entry
    response: httpx.Response
    usage_id: int
    started: float
    est_in: int
    media_type: str = "text/event-stream"
    latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def outbound_body(entry: Entry, body: dict[str, Any]) -> dict[str, Any]:
    payload = deep_merge(entry.model.extra_body, body)
    payload["model"] = entry.model.id
    payload.pop("hub", None)
    return payload


def upstream_headers(entry: Entry) -> dict[str, str]:
    headers = {"content-type": "application/json", **entry.provider.headers}
    key = entry.api_key
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers


def upstream_url(entry: Entry, path: str = "/chat/completions") -> str:
    return f"{entry.base_url}{path}"


def check_kind(entry: Entry) -> None:
    if entry.kind not in SUPPORTED_KINDS:
        raise HTTPException(status_code=501, detail=f"provider kind '{entry.kind}' not implemented")


def parse_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    return {
        "in_tokens": prompt,
        "out_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
    }


def response_status(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "ok"
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and choice.get("finish_reason") == "content_filter":
                return "refusal"
    return "ok"


def wants_json_response(body: dict[str, Any]) -> bool:
    response_format = body.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") in JSON_RESPONSE_FORMATS


def finish_reason_of(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                return str(choice["finish_reason"])
    return None


def strip_learned_unsupported_params(hub: Hub, entry: Entry, payload: dict[str, Any]) -> None:
    """Params this (account, model) has already told us it rejects: never send them again."""
    for param in hub.store.unsupported_params_for(entry.account_id, entry.key):
        payload.pop(param, None)


def _unsupported_param_to_drop(classification: Classification, payload: dict[str, Any]) -> str | None:
    if classification.kind != "unsupported_param":
        return None
    param = classification.detail.get("param")
    return param if isinstance(param, str) and param in payload else None


async def call_nonstream(
    hub: Hub, entry: Entry, body: dict[str, Any], app: str, attempt_no: int
) -> CallResult:
    check_kind(entry)
    payload = outbound_body(entry, body)
    strip_learned_unsupported_params(hub, entry, payload)
    try:
        return await _call_nonstream_once(hub, entry, payload, body, app, attempt_no)
    except UpstreamError as exc:
        param = _unsupported_param_to_drop(exc.classification, payload)
        if param is None:
            raise
        payload.pop(param, None)
        result = await _call_nonstream_once(hub, entry, payload, body, app, attempt_no)
        hub.store.record_unsupported_param(entry.account_id, entry.key, param)
        hub.store.add_event(
            kind="unsupported_param",
            message=f"{entry.key}: dropped '{param}' and retried, learned for next time",
            app=app,
            model=entry.key,
            account=entry.account_id,
        )
        return result


async def _call_nonstream_once(
    hub: Hub, entry: Entry, payload: dict[str, Any], body: dict[str, Any], app: str, attempt_no: int
) -> CallResult:
    started = time.perf_counter()
    try:
        response = await hub.client.post(upstream_url(entry), json=payload, headers=upstream_headers(entry))
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        classification = classify(None, str(exc), entry.template_id)
        hub.store.start_usage(
            app=app,
            provider=entry.provider_name,
            account=entry.account_id,
            model=entry.key,
            status="error",
            latency_ms=latency_ms,
            attempt=attempt_no,
            error_code=classification.code,
        )
        raise UpstreamError(classification, None, str(exc), latency_ms) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    raw = response.content
    if response.status_code >= 400:
        classification = classify(response.status_code, raw, entry.template_id, response.headers)
        status = "quota" if classification.is_quota else "error"
        hub.store.start_usage(
            app=app,
            provider=entry.provider_name,
            account=entry.account_id,
            model=entry.key,
            status=status,
            latency_ms=latency_ms,
            attempt=attempt_no,
            error_code=classification.code,
        )
        hub.store.add_event(
            kind=status,
            message=f"{entry.key} {response.status_code} {classification.code or ''} "
            f"{classification.message}"[:500],
            app=app,
            model=entry.key,
            account=entry.account_id,
        )
        raise UpstreamError(classification, response.status_code, raw, latency_ms)

    payload_json: dict[str, Any] | None
    try:
        parsed = json.loads(raw)
        payload_json = parsed if isinstance(parsed, dict) else None
    except ValueError:
        payload_json = None

    if payload_json and finish_reason_of(payload_json) == "length" and wants_json_response(body):
        # the JSON the caller asked for never closes - a normal outcome for prose is a broken
        # one here, so this attempt counts as failed and the router tries the next candidate
        max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
        hub.store.start_usage(
            app=app,
            provider=entry.provider_name,
            account=entry.account_id,
            model=entry.key,
            status="truncated",
            latency_ms=latency_ms,
            attempt=attempt_no,
        )
        hub.store.add_event(
            kind="truncated",
            message=f"{entry.key}: finish_reason=length with json response_format, max_tokens={max_tokens}",
            app=app,
            model=entry.key,
            account=entry.account_id,
        )
        classification = Classification(
            "truncated", None, "truncated json response", detail={"max_tokens": max_tokens}
        )
        raise UpstreamError(classification, response.status_code, raw, latency_ms)

    usage_id = hub.store.start_usage(
        app=app,
        provider=entry.provider_name,
        account=entry.account_id,
        model=entry.key,
        status="ok",
        latency_ms=latency_ms,
        attempt=attempt_no,
    )
    tokens = parse_usage(payload_json or {})
    if tokens:
        hub.store.update_usage_tokens(usage_id, **tokens, status=response_status(payload_json))
    else:
        est_in, _ = estimate_request_cost(body)
        est_out = len(raw) // 4
        hub.store.update_usage_tokens(usage_id, in_tokens=est_in, out_tokens=est_out, estimated=True)
        hub.store.add_event(
            kind="parse",
            message=f"{entry.key}: response without usage, estimated",
            app=app,
            model=entry.key,
            account=entry.account_id,
        )
    return CallResult(
        entry=entry,
        status_code=response.status_code,
        payload=payload_json,
        raw=raw,
        latency_ms=latency_ms,
        usage_id=usage_id,
        media_type=response.headers.get("content-type", "application/json").split(";")[0],
    )


async def call_stream(hub: Hub, entry: Entry, body: dict[str, Any], app: str, attempt_no: int) -> StreamCall:
    check_kind(entry)
    payload = outbound_body(entry, body)
    strip_learned_unsupported_params(hub, entry, payload)
    payload["stream"] = True
    stream_options = payload.get("stream_options")
    if not isinstance(stream_options, dict):
        payload["stream_options"] = {"include_usage": True}
    started = time.perf_counter()
    request = hub.client.build_request(
        "POST", upstream_url(entry), json=payload, headers=upstream_headers(entry)
    )
    try:
        response = await hub.client.send(request, stream=True)
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        classification = classify(None, str(exc), entry.template_id)
        hub.store.start_usage(
            app=app,
            provider=entry.provider_name,
            account=entry.account_id,
            model=entry.key,
            status="error",
            latency_ms=latency_ms,
            attempt=attempt_no,
            error_code=classification.code,
            stream=True,
        )
        raise UpstreamError(classification, None, str(exc), latency_ms) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        raw = await response.aread()
        await response.aclose()
        classification = classify(response.status_code, raw, entry.template_id, response.headers)
        status = "quota" if classification.is_quota else "error"
        hub.store.start_usage(
            app=app,
            provider=entry.provider_name,
            account=entry.account_id,
            model=entry.key,
            status=status,
            latency_ms=latency_ms,
            attempt=attempt_no,
            error_code=classification.code,
            stream=True,
        )
        raise UpstreamError(classification, response.status_code, raw, latency_ms)

    est_in, _ = estimate_request_cost(body)
    usage_id = hub.store.start_usage(
        app=app,
        provider=entry.provider_name,
        account=entry.account_id,
        model=entry.key,
        status="ok",
        latency_ms=latency_ms,
        attempt=attempt_no,
        stream=True,
    )
    return StreamCall(
        entry=entry,
        response=response,
        usage_id=usage_id,
        started=started,
        est_in=est_in,
        latency_ms=latency_ms,
        media_type=response.headers.get("content-type", "text/event-stream").split(";")[0],
    )


def _scan_sse_line(line: bytes, state: dict[str, Any]) -> None:
    text = line.strip()
    if not text.startswith(b"data:"):
        return
    data = text[5:].strip()
    if not data or data == b"[DONE]":
        return
    try:
        chunk = json.loads(data)
    except ValueError:
        return
    if not isinstance(chunk, dict):
        return
    usage = chunk.get("usage")
    if isinstance(usage, dict) and usage:
        state["usage"] = chunk
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            state["chars"] += len(content)


# the per (account, model) semaphore is released when router.run returns, so a stream still being
# consumed no longer holds it. The in-flight registry entry does outlive router.run - the call is
# still running as far as the caller is concerned - and is released here, where the usage row of
# the stream is closed.
async def stream_body(hub: Hub, call: StreamCall, call_id: int = 0) -> AsyncIterator[bytes]:
    state: dict[str, Any] = {"usage": None, "chars": 0}
    buffer = b""
    try:
        async for chunk in call.response.aiter_bytes():
            yield chunk
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                _scan_sse_line(line, state)
        if buffer:
            _scan_sse_line(buffer, state)
    finally:
        await call.response.aclose()
        latency_ms = int((time.perf_counter() - call.started) * 1000)
        tokens = parse_usage(state["usage"] or {})
        if tokens:
            hub.store.update_usage_tokens(call.usage_id, **tokens, latency_ms=latency_ms)
        else:
            hub.store.update_usage_tokens(
                call.usage_id,
                in_tokens=call.est_in,
                out_tokens=state["chars"] // 4,
                estimated=True,
                latency_ms=latency_ms,
            )
        if call_id:
            hub.router.release(call_id)


def hub_of(request: Request) -> Hub:
    return request.app.state.hub


def parse_list_header(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_int_header(value: str | None) -> int | None:
    """A positive int, or None when the header is absent or unreadable.

    Not a 400: these headers narrow the pool, and the selection echoes back what it applied,
    so a caller who mistyped one sees it in `error.constraints` rather than in a rejection.
    """
    try:
        parsed = int((value or "").strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def attempts_header(attempts: list[dict[str, Any]]) -> str:
    return ",".join(f"{item['model']}:{item['status']}" for item in attempts) or "none"


def hub_response_headers(result: RunResult) -> dict[str, str]:
    return {
        "X-Hub-Model": result.entry.key,
        "X-Hub-Account": result.entry.account_id,
        "X-Hub-Attempts": attempts_header(result.attempts),
        "X-Hub-Attempt-Count": str(len(result.attempts)),
    }


# best effort: the usage row of this call is already written, so the number is post-call.
# streams are skipped here, their usage lands after the headers are on the wire
def remaining_out_header(hub: Hub, entry: Entry) -> dict[str, str]:
    try:
        remaining = hub.quota.room(entry, datetime.now(UTC))["remaining_out"]
    except Exception as exc:  # noqa: BLE001
        log.warning("remaining_out unavailable for %s: %s", entry.key, exc)
        return {}
    return {"X-Hub-Remaining-Out": str(remaining)} if remaining is not None else {}


def vendor_detail(exc: UpstreamError | None) -> Any:
    """The last vendor body, parsed when it is JSON, so it nests instead of being escaped."""
    if exc is None:
        return None
    body = exc.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            return body[:1000]
    return body


def all_candidates_response(exc: AllCandidatesFailed) -> Response:
    """Whose fault the failure was decides the status code.

    Every candidate answering `error` is every vendor saying the same thing about the request:
    that is the client's to fix, so the last vendor body goes back as a 400 unchanged. Any
    other mix - quota, retry, a dead route - is the hub's side of the wire, and 502 says so.

    The last attempt has to carry a real HTTP status for the 400: a local backend that crashed
    before it reached a vendor also lands as `error`, and nobody looked at the request there.
    """
    headers = {"X-Hub-Attempts": attempts_header(exc.attempts)}
    statuses = {item["status"] for item in exc.attempts}
    rejected_by_vendor = exc.last is not None and exc.last.status_code is not None
    if exc.attempts and statuses == {"error"} and rejected_by_vendor:
        body = exc.last.body if exc.last is not None else None
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        if not isinstance(body, str):
            body = json.dumps({"error": {"message": "every candidate rejected the request"}})
        return Response(content=body, status_code=400, media_type="application/json", headers=headers)
    error: dict[str, Any] = {
        "message": "all candidates failed",
        "type": "upstream_failure",
        "attempts": exc.attempts,
        # the last vendor's own words: "all candidates failed" alone gives nobody anything to
        # act on, and the attempt list carries codes but not messages
        "last": vendor_detail(exc.last),
    }
    if exc.budget_exhausted:
        error["budget_exhausted"] = True
    return JSONResponse(
        status_code=502,
        content={"error": error},
        headers={**headers, "Retry-After": "30"},
    )


def quota_room_headers(room: dict[str, int | None], next_window: str | None) -> dict[str, str]:
    headers = {"Retry-After": "60"}
    if room["remaining_out"] is not None:
        headers["X-Hub-Remaining-Out"] = str(room["remaining_out"])
    if room["remaining_in"] is not None:
        headers["X-Hub-Remaining-In"] = str(room["remaining_in"])
    if room["remaining_requests"] is not None:
        headers["X-Hub-Remaining-Requests"] = str(room["remaining_requests"])
    if next_window:
        headers["X-Hub-Next-Window"] = next_window
    return headers


async def call_model(
    hub: Hub, entry: Entry, body: dict[str, Any], app: str, attempt_no: int, *, stream: bool = False
) -> Any:
    """One attempt against one candidate, whichever kind of backend it is.

    `kind: cli` runs a local agent CLI instead of an HTTP request; it has no real streaming, so
    a streaming request still runs once and the answer is shipped as a single chunk.
    """
    if entry.kind == CLI_KIND:
        from .cli_backend import CliRequestError, call_cli

        try:
            return await call_cli(hub, entry, body, app, attempt_no, stream=stream)
        except CliRequestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if stream:
        return await call_stream(hub, entry, body, app, attempt_no)
    return await call_nonstream(hub, entry, body, app, attempt_no)


async def cli_stream_body(payload: dict[str, Any] | None) -> AsyncIterator[bytes]:
    from .cli_backend import sse_chunks

    for chunk in sse_chunks(payload or {}):
        yield chunk


async def execute_chat(
    hub: Hub,
    *,
    body: dict[str, Any],
    app: str,
    model_request: str,
    require: list[str] | None = None,
    prefer: list[str] | None = None,
    allow_paid: bool = False,
    stream: bool = False,
    kind: str | None = None,
    job_id: str | None = None,
    min_context: int | None = None,
    avoid: list[str] | None = None,
    max_latency_ms: int | None = None,
    on_attempt: Callable[[dict[str, Any]], None] | None = None,
) -> RunResult:
    est_in, est_out = estimate_request_cost(body)
    selection = hub.router.select(
        model_request=model_request,
        require=require or [],
        prefer=prefer or [],
        allow_paid=allow_paid,
        est_in=est_in,
        est_out=est_out,
        # the app is part of the selection: its own bans keep models it has judged unusable
        # out of the pool, whichever way the call arrived
        app=app,
        min_context=min_context,
        avoid=avoid or [],
        max_latency_ms=max_latency_ms,
    )
    if not selection.candidates:
        raise NoCandidatesError("no eligible model with quota", selection.rejected, selection.constraints)

    async def call(entry: Entry, attempt_no: int) -> Any:
        return await call_model(hub, entry, body, app, attempt_no, stream=stream)

    call_kind = kind or ("stream" if stream else "sync")
    # a job has nobody waiting on the socket, so it walks the whole pool; a sync or stream
    # call has to answer before the client's read timeout, budget included
    budget_s = None if call_kind == "job" else float(hub.settings.run_budget_s)
    return await hub.router.run(
        selection.candidates,
        call,
        app=app,
        model_request=model_request,
        kind=call_kind,
        job_id=job_id,
        est_in=est_in,
        budget_s=budget_s,
        on_attempt=on_attempt,
    )


# How often the handler asks whether the socket is still there. uvicorn does not cancel a
# handler when the client goes away, so without this poll an abandoned request keeps a slot and
# a vendor call to the end - and behind it queues every other caller of the same pair.
DISCONNECT_POLL_S = 1.0


async def watch_client(request: Request, task: asyncio.Task[Any]) -> None:
    """Cancel the run as soon as the socket behind it is gone."""
    while not task.done():
        if await request.is_disconnected():
            task.cancel()
            return
        await asyncio.sleep(DISCONNECT_POLL_S)


async def execute_watched(hub: Hub, request: Request, **kwargs: Any) -> RunResult:
    """`execute_chat` as a task, with the disconnect watcher alongside it."""
    task = asyncio.create_task(execute_chat(hub, **kwargs))
    watcher = asyncio.create_task(watch_client(request, task))
    try:
        return await task
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        if not task.done():
            # the handler itself was cancelled: the run must not outlive the request
            task.cancel()


def cancel_reason(attempts: list[dict[str, Any]]) -> str | None:
    """Why the run was cancelled, from the row the router booked before it re-raised.

    Nothing there means the cancellation was not the router's to explain - the handler is
    being torn down for some other reason - and the CancelledError belongs upstream.
    """
    if attempts and attempts[-1]["status"] == ABANDONED_STATUS:
        return str(attempts[-1]["error_code"] or CANCEL_CLIENT_GONE)
    return None


def cancelled_response(reason: str, attempts: list[dict[str, Any]]) -> Response:
    """What a cancelled run answers with - to a client that may not be there any more."""
    headers = {"X-Hub-Attempts": attempts_header(attempts)}
    if reason == CANCEL_CLIENT_GONE:
        # the socket is already gone, so this body reaches nobody; it exists so the handler
        # returns a response instead of letting a CancelledError look like a crash
        return JSONResponse(
            status_code=499,
            content={"error": {"message": "client disconnected", "type": CANCEL_CLIENT_GONE}},
            headers=headers,
        )
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "message": "the hub owner cancelled this call from the dashboard",
                "type": CANCEL_BY_OWNER,
            }
        },
        headers=headers,
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    require_token(request)
    hub = hub_of(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    app = request.headers.get("x-hub-app")
    if not app:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "missing required request header X-Hub-App; add "
                    "'X-Hub-App: <your app name>' (for example 'X-Hub-App: my-app') to "
                    "identify the calling app",
                    "type": "missing_header",
                    "param": "X-Hub-App",
                    "code": "missing_header",
                    "header": "X-Hub-App",
                    "example": "X-Hub-App: my-app",
                }
            },
        )
    hub.store.ensure_app(app)
    if hub.store.app_paused(app):
        raise HTTPException(status_code=503, detail=f"app '{app}' is paused")

    model_request = str(body.get("model") or "auto")
    require = parse_list_header(request.headers.get("x-hub-require"))
    prefer = parse_list_header(request.headers.get("x-hub-prefer"))
    avoid = parse_list_header(request.headers.get("x-hub-avoid"))
    min_context = parse_int_header(request.headers.get("x-hub-min-context"))
    max_latency_ms = parse_int_header(request.headers.get("x-hub-max-latency-ms"))
    allow_paid = request.headers.get("x-hub-allow-paid") == "1"
    stream = bool(body.get("stream"))

    attempts: list[dict[str, Any]] = []
    try:
        result = await execute_watched(
            hub,
            request,
            body=body,
            app=app,
            model_request=model_request,
            require=require,
            prefer=prefer,
            allow_paid=allow_paid,
            stream=stream,
            min_context=min_context,
            avoid=avoid,
            max_latency_ms=max_latency_ms,
            on_attempt=attempts.append,
        )
    except asyncio.CancelledError:
        # the run task was cancelled, not this handler: the router has already released the
        # slot, killed the vendor call and written the abandoned row
        reason = cancel_reason(attempts)
        if reason is None:
            raise
        return cancelled_response(reason, attempts)
    except UnknownModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NoCandidatesError as exc:
        reasons = {row["reason"] for row in exc.rejected}
        if exc.rejected and reasons == {"too_large"}:
            # two axes, two kinds of detail: an input ceiling is the plain int the entry's own
            # ceiling produced, an output ceiling arrives as {"max_out_tokens": N}
            in_caps = [row["detail"] for row in exc.rejected if isinstance(row["detail"], int)]
            out_caps = [
                row["detail"]["max_out_tokens"]
                for row in exc.rejected
                if isinstance(row["detail"], dict) and row["detail"].get("max_out_tokens") is not None
            ]
            max_request_tokens = max(in_caps) if in_caps else None
            max_out_tokens = max(out_caps) if out_caps else None
            requested_in = next(
                (row.get("requested_in") for row in exc.rejected if row.get("requested_in") is not None),
                None,
            )
            requested_out = next(
                (row.get("requested_out") for row in exc.rejected if row.get("requested_out") is not None),
                None,
            )
            if in_caps and requested_in is not None:
                message = f"no candidate accepts a request of {requested_in} input tokens"
            elif out_caps and requested_out is not None:
                message = f"no candidate accepts a request for {requested_out} output tokens"
            else:
                message = str(exc)
            hub.store.add_event(kind="too_large", message=message, app=app, model=model_request)
            headers = {}
            if max_request_tokens is not None:
                headers["X-Hub-Max-Request"] = str(max_request_tokens)
            if max_out_tokens is not None:
                headers["X-Hub-Max-Output"] = str(max_out_tokens)
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "message": message,
                        "type": "too_large",
                        "rejected": exc.rejected,
                        "max_request_tokens": max_request_tokens,
                        "max_out_tokens": max_out_tokens,
                    }
                },
                headers=headers,
            )
        next_window = hub.router.next_window_at(model_request, allow_paid)
        room = quota_room(exc.rejected)
        hub.store.add_event(kind="no_candidates", message=str(exc), app=app, model=model_request)
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": str(exc),
                    "type": "no_candidates",
                    "rejected": exc.rejected,
                    "next_window_at": next_window,
                    "remaining_out": room["remaining_out"],
                    "remaining_in": room["remaining_in"],
                    "remaining_requests": room["remaining_requests"],
                    # what the alias and the headers together asked the selection for
                    "constraints": exc.constraints,
                }
            },
            headers=quota_room_headers(room, next_window),
        )
    except UpstreamError as exc:
        # safety net only: Router.run turns every vendor failure into a fallback or into
        # AllCandidatesFailed, so nothing should land here
        log.warning("upstream error escaped the router: %s %s", exc.status_code, exc.classification.kind)
        detail = exc.body if isinstance(exc.body, (str, bytes)) else str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        return Response(
            content=detail,
            status_code=exc.status_code or 502,
            media_type="application/json",
        )
    except AllCandidatesFailed as exc:
        return all_candidates_response(exc)

    headers = hub_response_headers(result)
    if stream and result.entry.kind == CLI_KIND:
        cli_result = result.result
        # the CLI answer is already complete, the SSE frames are only packaging: nothing is
        # still in flight once the run returns
        hub.router.release(result.call_id)
        return StreamingResponse(
            cli_stream_body(cli_result.payload),
            status_code=200,
            media_type="text/event-stream",
            headers=headers,
        )
    if stream:
        call: StreamCall = result.result
        return StreamingResponse(
            stream_body(hub, call, result.call_id),
            status_code=200,
            media_type="text/event-stream",
            headers=headers,
        )
    call_result: CallResult = result.result
    headers.update(remaining_out_header(hub, result.entry))
    return Response(
        content=call_result.raw,
        status_code=call_result.status_code,
        media_type=call_result.media_type,
        headers=headers,
    )


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    allow_paid = request.headers.get("x-hub-allow-paid") == "1"
    now = datetime.now(UTC)
    data: list[dict[str, Any]] = []
    for row in model_rows(hub, now):
        if not row["free"] and not allow_paid:
            continue
        if row["disabled"] or row["status"] in ("down", "expired"):
            continue
        data.append(
            {
                "id": row["key"],
                "object": "model",
                "created": 0,
                "owned_by": row["provider"],
                "hub": {
                    "account": row["account"],
                    "caps": row["caps"],
                    "context": row["context"],
                    "free": row["free"],
                    "status": row["status"],
                    "windows": row["windows"],
                    # size max_tokens against remaining_out; window_limit is the most a single
                    # request could ever get on this pair, whatever the window state
                    "remaining_out": row["remaining_out"],
                    "remaining_in": row["remaining_in"],
                    "remaining_requests": row["remaining_requests"],
                    "window_limit": row["window_limit"],
                    "resets_at": row["resets_at"],
                    "last_error": row["last_error"],
                    "avg_latency_ms": row["avg_latency_ms"],
                },
            }
        )
    for alias_name, alias in hub.registry.aliases.items():
        data.append(
            {
                "id": alias_name,
                "object": "model",
                "created": 0,
                "owned_by": "llmhub",
                "hub": {"alias": True, "require": list(alias.require), "prefer": list(alias.prefer)},
            }
        )
    return {"object": "list", "data": data}
