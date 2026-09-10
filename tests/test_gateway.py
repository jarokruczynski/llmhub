from __future__ import annotations

import json

import httpx
import pytest
import respx

from llmhub.gateway import execute_chat, stream_body
from llmhub.runtime import Hub
from llmhub.status import model_rows

ALPHA_URL = "https://alpha.test/v1/chat/completions"
BETA_URL = "https://beta.test/v1/chat/completions"

BODY = {"model": "auto", "messages": [{"role": "user", "content": "hello there"}], "max_tokens": 64}

OK_PAYLOAD = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}

SSE_WITH_USAGE = (
    b'data: {"id":"1","choices":[{"delta":{"content":"he"},"index":0}]}\n\n'
    b'data: {"id":"1","choices":[{"delta":{"content":"llo"},"index":0}]}\n\n'
    b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    b"data: [DONE]\n\n"
)

SSE_WITHOUT_USAGE = b'data: {"id":"1","choices":[{"delta":{"content":"abcd"},"index":0}]}\n\ndata: [DONE]\n\n'


@respx.mock
async def test_nonstream_success_headers_and_usage(client: httpx.AsyncClient, hub: Hub) -> None:
    route = respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.json()["id"] == "chatcmpl-1"
    assert response.headers["x-hub-model"] == "alpha/m1"
    assert response.headers["x-hub-account"] == "alpha-1"
    assert response.headers["x-hub-attempts"] == "alpha/m1:ok"

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "m1"
    assert sent["thinking"] == {"type": "disabled"}

    rows = hub.store.query("SELECT * FROM usage")
    assert len(rows) == 1
    assert rows[0]["in_tokens"] == 11
    assert rows[0]["out_tokens"] == 5
    assert rows[0]["app"] == "my-app"
    assert rows[0]["estimated"] == 0


@respx.mock
async def test_request_body_wins_over_registry_extra_body(client: httpx.AsyncClient) -> None:
    route = respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    body = dict(BODY, thinking={"type": "enabled"})
    await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})
    sent = json.loads(route.calls[0].request.content)
    assert sent["thinking"] == {"type": "enabled"}


@respx.mock
async def test_quota_error_falls_back_to_next_provider(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            429, json={"error": {"code": "insufficient_quota", "message": "no quota"}}
        )
    )
    respx.post(BETA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))

    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.headers["x-hub-model"] == "beta/m2"
    assert response.headers["x-hub-attempts"].startswith("alpha/m1:quota")
    exhausted = hub.store.query("SELECT * FROM exhausted")
    assert {(row["account"], row["model"]) for row in exhausted} == {
        ("alpha-1", "alpha/m1"),
        ("alpha-2", "alpha/m1"),
    }
    statuses = [row["status"] for row in hub.store.query("SELECT * FROM usage ORDER BY id")]
    assert statuses == ["quota", "quota", "ok"]


@respx.mock
async def test_usage_row_written_when_parsing_fails(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(200, content=b"not json at all", headers={"content-type": "text/plain"})
    )
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.content == b"not json at all"
    rows = hub.store.query("SELECT * FROM usage")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["estimated"] == 1
    assert rows[0]["in_tokens"] > 0


@respx.mock
async def test_streaming_passthrough_and_usage_from_final_chunk(client: httpx.AsyncClient, hub: Hub) -> None:
    route = respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            200, content=SSE_WITH_USAGE, headers={"content-type": "text/event-stream"}
        )
    )
    chunks: list[bytes] = []
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=dict(BODY, stream=True),
        headers={"X-Hub-App": "my-app"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-hub-model"] == "alpha/m1"
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    assert b"".join(chunks) == SSE_WITH_USAGE
    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}

    rows = hub.store.query("SELECT * FROM usage")
    assert len(rows) == 1
    assert rows[0]["stream"] == 1
    assert rows[0]["in_tokens"] == 7
    assert rows[0]["out_tokens"] == 3
    assert rows[0]["estimated"] == 0


@respx.mock
async def test_streaming_without_usage_is_estimated(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            200, content=SSE_WITHOUT_USAGE, headers={"content-type": "text/event-stream"}
        )
    )
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=dict(BODY, stream=True),
        headers={"X-Hub-App": "my-app"},
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    rows = hub.store.query("SELECT * FROM usage")
    assert rows[0]["estimated"] == 1
    assert rows[0]["out_tokens"] == 1


@respx.mock
async def test_no_candidates_returns_429_with_next_window(client: httpx.AsyncClient, hub: Hub) -> None:
    for entry in hub.registry.entries():
        if entry.model.is_free:
            hub.quota.mark_exhausted(entry, "insufficient_quota")
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 429
    payload = response.json()["error"]
    assert payload["type"] == "no_candidates"
    assert payload["next_window_at"]


def spend_out(hub: Hub, account: str, out_tokens: int) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account=account,
        model="alpha/m1",
        status="ok",
        latency_ms=1,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=0, out_tokens=out_tokens)


async def test_no_room_429_reports_the_largest_remaining_across_candidates(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    spend_out(hub, "alpha-1", 900)
    spend_out(hub, "alpha-2", 500)
    response = await client.post(
        "/v1/chat/completions",
        json=dict(BODY, model="alpha/m1", max_tokens=900),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.headers["x-hub-remaining-out"] == "500"
    assert response.headers["x-hub-remaining-in"] == "10000"
    error = response.json()["error"]
    assert response.headers["x-hub-next-window"] == error["next_window_at"]
    assert error["remaining_out"] == 500
    assert error["remaining_in"] == 10000
    quota_rows = [row for row in error["rejected"] if row["reason"] == "quota"]
    assert {row["remaining_out"] for row in quota_rows} == {100, 500}
    assert {row["requested_out"] for row in quota_rows} == {900}


async def test_no_room_429_reports_request_counts_when_that_is_what_binds(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    for account in ("alpha-1", "alpha-2"):
        hub.store.record_observed_limit(account, "alpha/m1", "daily", "requests", 1)
        spend_out(hub, account, 10)
    response = await client.post(
        "/v1/chat/completions",
        json=dict(BODY, model="alpha/m1", max_tokens=16),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 429
    assert response.headers["x-hub-remaining-requests"] == "0"
    error = response.json()["error"]
    assert error["remaining_requests"] == 0
    quota_rows = [row for row in error["rejected"] if row["reason"] == "quota"]
    assert {row["remaining_requests"] for row in quota_rows} == {0}
    assert {row["detail"] for row in quota_rows} == {"daily"}


async def test_429_without_quota_rejection_omits_remaining_headers(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    hub.store.set_model_disabled("alpha/m1", True)
    response = await client.post(
        "/v1/chat/completions",
        json=dict(BODY, model="alpha/m1"),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 429
    assert "x-hub-remaining-out" not in response.headers
    assert "x-hub-remaining-in" not in response.headers
    assert "x-hub-remaining-requests" not in response.headers
    assert response.json()["error"]["remaining_out"] is None


@respx.mock
async def test_success_reports_remaining_out_after_the_call(client: httpx.AsyncClient) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    response = await client.post(
        "/v1/chat/completions",
        json=dict(BODY, model="alpha/m1"),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 200
    assert response.headers["x-hub-remaining-out"] == "995"


@respx.mock
async def test_success_without_declared_cap_has_no_remaining_header(
    client: httpx.AsyncClient,
) -> None:
    respx.post(BETA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    response = await client.post(
        "/v1/chat/completions",
        json=dict(BODY, model="beta/m2"),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 200
    assert "x-hub-remaining-out" not in response.headers


@respx.mock
async def test_stream_response_has_no_remaining_header(client: httpx.AsyncClient) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            200, content=SSE_WITH_USAGE, headers={"content-type": "text/event-stream"}
        )
    )
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=dict(BODY, model="alpha/m1", stream=True),
        headers={"X-Hub-App": "my-app"},
    ) as response:
        assert response.status_code == 200
        assert "x-hub-remaining-out" not in response.headers
        async for _ in response.aiter_bytes():
            pass


async def test_missing_app_header_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 400


async def test_missing_app_header_body_names_the_header(client: httpx.AsyncClient) -> None:
    error = (await client.post("/v1/chat/completions", json=BODY)).json()["error"]
    assert error["type"] == "missing_header"
    assert error["param"] == "X-Hub-App"
    assert error["header"] == "X-Hub-App"
    assert error["example"] == "X-Hub-App: my-app"
    assert "X-Hub-App" in error["message"]


async def test_paused_app_blocked(client: httpx.AsyncClient, hub: Hub) -> None:
    hub.store.set_app_paused("my-app", True)
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 503


async def test_unknown_model_is_400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=dict(BODY, model="ghost/model"),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 400


@respx.mock
async def test_client_error_tries_the_next_candidate_before_giving_up(
    client: httpx.AsyncClient,
) -> None:
    """One vendor's opinion of the request is not the truth for the next vendor."""
    alpha = respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "bad request", "code": "invalid_request"}}
        )
    )
    beta = respx.post(BETA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 200
    assert response.headers["x-hub-model"] == "beta/m2"
    assert alpha.called
    assert beta.called


async def test_models_endpoint_lists_free_models_and_aliases(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "alpha/m1" in ids
    assert "alpha/paid1" not in ids
    assert "auto" in ids and "vision" in ids

    paid = await client.get("/v1/models", headers={"X-Hub-Allow-Paid": "1"})
    assert "alpha/paid1" in [item["id"] for item in paid.json()["data"]]


async def test_lan_request_requires_token(client: httpx.AsyncClient, hub: Hub) -> None:
    headers = {"X-Hub-App": "my-app", "X-Forwarded-For": "192.168.1.20"}
    response = await client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 401


@respx.mock
async def test_lan_request_with_token_allowed(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    object.__setattr__(hub.settings, "token", "secret")
    headers = {
        "X-Hub-App": "my-app",
        "X-Forwarded-For": "192.168.1.20",
        "Authorization": "Bearer secret",
    }
    response = await client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize("kind", ["anthropic"])
@respx.mock
async def test_unsupported_kind_returns_501(client: httpx.AsyncClient, hub: Hub, kind: str) -> None:
    hub.registry.providers["alpha"].kind = kind
    hub.store.set_model_disabled("beta/m2", True)
    response = await client.post(
        "/v1/chat/completions",
        json=dict(BODY, model="alpha/m1"),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 501


# --- request size awareness --------------------------------------------------------------


def set_alpha_m1_max_request_tokens(hub: Hub, value: int) -> None:
    for model in hub.registry.providers["alpha"].models:
        if model.id == "m1":
            model.max_request_tokens = value


async def test_all_candidates_too_large_returns_413(client: httpx.AsyncClient, hub: Hub) -> None:
    set_alpha_m1_max_request_tokens(hub, 5)
    body = dict(BODY, model="alpha/m1", messages=[{"role": "user", "content": "x" * 400}])
    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 413
    assert response.headers["x-hub-max-request"] == "5"
    error = response.json()["error"]
    assert error["type"] == "too_large"
    assert error["max_request_tokens"] == 5
    assert len(error["rejected"]) == 2
    assert all(row["reason"] == "too_large" for row in error["rejected"])

    events = hub.store.query("SELECT * FROM events WHERE kind = 'too_large'")
    assert len(events) == 1


async def test_mixed_rejections_still_return_429(client: httpx.AsyncClient, hub: Hub) -> None:
    set_alpha_m1_max_request_tokens(hub, 5)
    hub.store.set_model_disabled("beta/m2", True)
    hub.store.set_model_disabled("gamma/fixed", True)
    hub.store.set_model_disabled("gamma/openended", True)
    body = dict(BODY, messages=[{"role": "user", "content": "x" * 400}])  # model stays "auto"
    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["type"] == "no_candidates"
    reasons = {row["reason"] for row in error["rejected"]}
    assert "too_large" in reasons
    assert reasons != {"too_large"}


LENGTH_JSON_PAYLOAD = {
    "id": "chatcmpl-2",
    "object": "chat.completion",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "{incomplete"}, "finish_reason": "length"}
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 64, "total_tokens": 75},
}

LENGTH_PROSE_PAYLOAD = {
    "id": "chatcmpl-3",
    "object": "chat.completion",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "still talking"}, "finish_reason": "length"}
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 64, "total_tokens": 75},
}


@respx.mock
async def test_truncated_json_response_advances_to_the_next_candidate(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    respx.post(ALPHA_URL).mock(
        side_effect=[
            httpx.Response(200, json=LENGTH_JSON_PAYLOAD),
            httpx.Response(200, json=OK_PAYLOAD),
        ]
    )
    body = dict(BODY, model="alpha/m1", response_format={"type": "json_object"})
    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.headers["x-hub-account"] == "alpha-2"

    events = hub.store.query("SELECT * FROM events WHERE kind = 'truncated'")
    assert len(events) == 1
    assert "alpha/m1" in events[0]["message"]
    assert "64" in events[0]["message"]

    statuses = [row["status"] for row in hub.store.query("SELECT * FROM usage ORDER BY id")]
    assert statuses == ["truncated", "ok"]


@respx.mock
async def test_truncated_json_schema_response_also_advances(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(
        side_effect=[
            httpx.Response(200, json=LENGTH_JSON_PAYLOAD),
            httpx.Response(200, json=OK_PAYLOAD),
        ]
    )
    body = dict(BODY, model="alpha/m1", response_format={"type": "json_schema", "json_schema": {}})
    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 200
    assert response.headers["x-hub-account"] == "alpha-2"


@respx.mock
async def test_truncated_prose_response_is_returned_unchanged(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=LENGTH_PROSE_PAYLOAD))
    body = dict(BODY, model="alpha/m1")  # no response_format: this is a normal prose answer
    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.headers["x-hub-account"] == "alpha-1"
    assert response.json()["choices"][0]["finish_reason"] == "length"

    events = hub.store.query("SELECT * FROM events WHERE kind = 'truncated'")
    assert events == []


UNSUPPORTED_TEMPERATURE_PAYLOAD = {
    "error": {
        "message": "The value 0.0 for 'temperature' is not supported by this model route. "
        "Supported values are between 1.0 and 1.0.",
        "param": "temperature",
    }
}


@respx.mock
async def test_unsupported_param_is_stripped_and_retried_on_the_same_candidate(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    route = respx.post(ALPHA_URL).mock(
        side_effect=[
            httpx.Response(400, json=UNSUPPORTED_TEMPERATURE_PAYLOAD),
            httpx.Response(200, json=OK_PAYLOAD),
        ]
    )
    body = dict(BODY, model="alpha/m1", temperature=0.0)
    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.headers["x-hub-account"] == "alpha-1"
    assert response.headers["x-hub-attempts"] == "alpha/m1:ok"
    assert route.call_count == 2

    first_sent = json.loads(route.calls[0].request.content)
    second_sent = json.loads(route.calls[1].request.content)
    assert first_sent["temperature"] == 0.0
    assert "temperature" not in second_sent

    assert hub.store.unsupported_params_for("alpha-1", "alpha/m1") == {"temperature"}
    events = hub.store.query("SELECT * FROM events WHERE kind = 'unsupported_param'")
    assert len(events) == 1
    assert "temperature" in events[0]["message"]


@respx.mock
async def test_learned_unsupported_param_is_stripped_before_sending(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    hub.store.record_unsupported_param("alpha-1", "alpha/m1", "temperature")
    route = respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    body = dict(BODY, model="alpha/m1", temperature=0.7)

    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert "temperature" not in sent


@respx.mock
async def test_unsupported_param_retry_failure_falls_back_to_next_candidate(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    route = respx.post(ALPHA_URL).mock(
        side_effect=[
            httpx.Response(400, json=UNSUPPORTED_TEMPERATURE_PAYLOAD),  # alpha-1, first try
            httpx.Response(400, json=UNSUPPORTED_TEMPERATURE_PAYLOAD),  # alpha-1, internal retry
            httpx.Response(200, json=OK_PAYLOAD),  # alpha-2, first try
        ]
    )
    body = dict(BODY, model="alpha/m1", temperature=0.0)
    response = await client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.headers["x-hub-account"] == "alpha-2"
    assert route.call_count == 3
    # the retry itself failed: this account never got to prove the strip actually helps
    assert hub.store.unsupported_params_for("alpha-1", "alpha/m1") == set()


@respx.mock
async def test_stream_stays_in_flight_until_the_body_is_done(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            200, content=SSE_WITH_USAGE, headers={"content-type": "text/event-stream"}
        )
    )
    result = await execute_chat(
        hub,
        body=dict(BODY, model="alpha/m1", stream=True),
        app="my-app",
        model_request="alpha/m1",
        stream=True,
    )
    # router.run has returned and released the semaphore; the call is not finished
    assert [(row["app"], row["model"], row["kind"]) for row in hub.router.in_flight()] == [
        ("my-app", "alpha/m1", "stream")
    ]
    async for _chunk in stream_body(hub, result.result, result.call_id):
        pass
    assert hub.router.in_flight() == []


@respx.mock
async def test_streaming_request_leaves_nothing_in_flight(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            200, content=SSE_WITH_USAGE, headers={"content-type": "text/event-stream"}
        )
    )
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=dict(BODY, model="alpha/m1", stream=True),
        headers={"X-Hub-App": "my-app"},
    ) as response:
        async for _chunk in response.aiter_bytes():
            pass
    assert hub.router.in_flight() == []


@respx.mock
async def test_nonstream_call_is_in_flight_only_while_it_runs(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    await client.post(
        "/v1/chat/completions", json=dict(BODY, model="alpha/m1"), headers={"X-Hub-App": "my-app"}
    )
    assert hub.router.in_flight() == []


# --- router failure handling -------------------------------------------------------------

GAMMA_FIXED_URL = "https://gamma.test/v1/chat/completions"
BAD_REQUEST = {"error": {"message": "bad request", "code": "invalid_request"}}
VENDOR_404 = {
    "error": {
        "message": "The model `m1` does not exist or you do not have access to it.",
        "type": "invalid_request_error",
        "code": "model_not_found",
    }
}


@respx.mock
async def test_a_vendor_404_never_surfaces_as_a_client_404(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(404, json=VENDOR_404))
    respx.post(BETA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    assert response.headers["x-hub-model"] == "beta/m2"
    parked = hub.store.unavailable_all()
    assert {pair[0] for pair in parked} == {"alpha-1", "alpha-2"}
    assert all(row["kind"] == "not_found" for row in parked.values())


@respx.mock
async def test_a_parked_pair_is_a_down_row_and_leaves_the_model_list(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(404, json=VENDOR_404))
    respx.post(BETA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    rows = [row for row in model_rows(hub) if row["key"] == "alpha/m1"]
    assert rows and all(row["status"] == "down" for row in rows)
    assert all(row["reason"].startswith("not_found: model_not_found until ") for row in rows)
    ids = [item["id"] for item in (await client.get("/v1/models")).json()["data"]]
    assert "alpha/m1" not in ids


@respx.mock
async def test_every_vendor_rejecting_the_request_is_a_400_with_the_vendor_body(
    client: httpx.AsyncClient,
) -> None:
    for url in (ALPHA_URL, BETA_URL, GAMMA_FIXED_URL):
        respx.post(url).mock(return_value=httpx.Response(400, json=BAD_REQUEST))
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.headers["x-hub-attempts"].startswith("alpha/m1:error")


@respx.mock
async def test_a_mixed_failure_is_a_502_with_the_attempts(client: httpx.AsyncClient) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(400, json=BAD_REQUEST))
    respx.post(BETA_URL).mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
    respx.post(GAMMA_FIXED_URL).mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 502
    assert response.headers["retry-after"] == "30"
    error = response.json()["error"]
    assert error["type"] == "upstream_failure"
    assert {item["status"] for item in error["attempts"]} == {"error", "retry"}
    assert "budget_exhausted" not in error
    assert response.headers["x-hub-attempts"].startswith("alpha/m1:error")


@respx.mock
async def test_a_502_flags_a_run_that_ran_out_of_budget(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    object.__setattr__(hub.settings, "run_budget_s", 0)
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["budget_exhausted"] is True
    # the budget stopped the walk: only the first candidate was ever asked
    assert len(error["attempts"]) == 1


async def test_models_rows_carry_the_room_a_client_sizes_max_tokens_against(
    client: httpx.AsyncClient,
) -> None:
    data = (await client.get("/v1/models")).json()["data"]
    row = next(item for item in data if item["id"] == "alpha/m1")
    assert row["hub"]["remaining_out"] == 1000
    assert row["hub"]["remaining_in"] == 10000
    assert row["hub"]["window_limit"] == 1000
    assert row["hub"]["resets_at"]
    # a model with no declared windows knows nothing, and an unknown limit is null, not zero
    unknown = next(item for item in data if item["id"] == "beta/m2")
    assert unknown["hub"]["remaining_out"] is None
    assert unknown["hub"]["window_limit"] is None
