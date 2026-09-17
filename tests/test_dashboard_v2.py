from __future__ import annotations

import httpx


async def test_the_console_is_served_at_the_root(client: httpx.AsyncClient) -> None:
    res_root = await client.get("/")
    assert res_root.status_code == 200
    assert "text/html" in res_root.headers.get("content-type", "")
    assert "LLMHub Studio v2" in res_root.text

    # the old address keeps working, relative so it survives the /hub prefix on the LAN.
    # How far up depends on the trailing slash: the browser resolves /hub/v2 against /hub/,
    # where "../" would already be one level too high and land on the machine's home page.
    for path, target in (("/v2", "./"), ("/v2/", "../")):
        moved = await client.get(path, follow_redirects=False)
        assert moved.status_code == 308, path
        assert moved.headers["location"] == target, path

    res_v2 = await client.get("/v2", follow_redirects=True)
    assert res_v2.status_code == 200
    assert "text/html" in res_v2.headers.get("content-type", "")
    assert "LLMHub Studio v2" in res_v2.text
    assert "static/v2/v2.css" in res_v2.text
    assert "static/v2/v2.js" in res_v2.text
    assert "Playground" in res_v2.text
    assert "Routing & Matrix" in res_v2.text

    res_v2_slash = await client.get("/v2/", follow_redirects=True)
    assert res_v2_slash.status_code == 200
    assert "LLMHub Studio v2" in res_v2_slash.text


async def test_dashboard_v2_static_assets(client: httpx.AsyncClient) -> None:
    # Static CSS
    res_css = await client.get("/static/v2/v2.css")
    assert res_css.status_code == 200
    assert "text/css" in res_css.headers.get("content-type", "")
    assert "--accent-primary" in res_css.text

    # Static JS
    res_js = await client.get("/static/v2/v2.js")
    assert res_js.status_code == 200
    assert "text/javascript" in res_js.headers.get("content-type", "")
    assert "LLMHub Modern Studio" in res_js.text

    # Via /v2/static prefix
    res_v2_static = await client.get("/v2/static/v2/v2.css")
    assert res_v2_static.status_code == 200
    assert "text/css" in res_v2_static.headers.get("content-type", "")


async def test_update_alias_validation(client: httpx.AsyncClient) -> None:
    # 1. Unknown model must be refused with 422
    res_unknown = await client.put(
        "/api/aliases/auto",
        json={"prefer": ["nonexistent/fake-model"], "spread": 2},
    )
    assert res_unknown.status_code == 422
    assert "unknown model" in res_unknown.text

    # 2. Valid model from registry must be accepted and persisted
    res_valid = await client.put(
        "/api/aliases/auto",
        json={"prefer": ["alpha/m1"], "spread": 3},
    )
    assert res_valid.status_code == 200
    data = res_valid.json()
    assert data["alias"] == "auto"
    assert data["prefer"] == ["alpha/m1"]
    assert data["spread"] == 3


async def test_an_alias_cannot_be_emptied(client: httpx.AsyncClient) -> None:
    before = await client.get("/api/registry")
    assert before.status_code == 200

    for payload in ({"prefer": []}, {"prefer": ["", "   "]}):
        res = await client.put("/api/aliases/auto", json=payload)
        assert res.status_code == 400, payload
        assert "empty" in res.text

    after = await client.put("/api/aliases/auto", json={"prefer": ["alpha/m1"]})
    assert after.status_code == 200
    assert after.json()["prefer"] == ["alpha/m1"]


async def test_health_sweep_endpoint(client: httpx.AsyncClient) -> None:
    res = await client.post("/api/health/sweep")
    assert res.status_code == 200
    data = res.json()
    assert "probed_count" in data
    assert "skipped_count" in data
    assert "request_cost" in data
    assert data["request_cost"] == data["probed_count"]

    res_status = await client.get("/api/health/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["last_sweep_at"] is not None


async def test_baselines_are_served_with_their_provenance(client: httpx.AsyncClient) -> None:
    res = await client.get("/api/baselines")
    assert res.status_code == 200
    body = res.json()

    assert body["currency"] == "USD"
    assert body["as_of"]
    assert body["default"] in {item["id"] for item in body["baselines"]}
    for item in body["baselines"]:
        assert item["source"].startswith("https://")
        assert item["cached"] < item["input"] < item["output"]

    tier = body["tier_matched"]
    known = {item["id"] for item in body["baselines"]}
    assert tier["small"] in known and tier["large"] in known
    assert tier["markers"]


async def test_the_dashboard_carries_no_prices_of_its_own(client: httpx.AsyncClient) -> None:
    script = await client.get("/static/v2/v2.js")
    page = await client.get("/v2")

    assert "BASELINES" not in script.text
    assert "GPT-4o" not in page.text
