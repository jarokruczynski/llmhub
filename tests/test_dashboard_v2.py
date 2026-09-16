from __future__ import annotations

import httpx


async def test_dashboard_v1_and_v2_routes(client: httpx.AsyncClient) -> None:
    # Classic v1 dashboard
    res_v1 = await client.get("/")
    assert res_v1.status_code == 200
    assert "text/html" in res_v1.headers.get("content-type", "")
    assert "llmhub" in res_v1.text
    assert "Studio v2" in res_v1.text

    # Modern v2 dashboard
    res_v2 = await client.get("/v2")
    assert res_v2.status_code == 200
    assert "text/html" in res_v2.headers.get("content-type", "")
    assert "LLMHub Studio v2" in res_v2.text
    assert "static/v2/v2.css" in res_v2.text
    assert "static/v2/v2.js" in res_v2.text
    assert "Playground" in res_v2.text
    assert "Routing & Matrix" in res_v2.text

    # Trailing slash
    res_v2_slash = await client.get("/v2/")
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
