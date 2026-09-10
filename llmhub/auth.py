from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request


def client_host(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def is_loopback(request: Request) -> bool:
    host = client_host(request)
    if not host:
        return True
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def require_token(request: Request) -> None:
    if is_loopback(request):
        return
    settings = request.app.state.hub.settings
    if not settings.token:
        raise HTTPException(status_code=401, detail="LLMHUB_TOKEN not configured; LAN access denied")
    if bearer_token(request) != settings.token:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def require_read(request: Request) -> None:
    return None
