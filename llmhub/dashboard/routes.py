from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".woff2": "font/woff2",
}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"asset_v": _asset_version()})


@router.get("/static/{path:path}", include_in_schema=False)
async def dashboard_static(path: str) -> FileResponse:
    target = (STATIC_DIR / path).resolve()
    if not str(target).startswith(str(STATIC_DIR) + "/") or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        target,
        media_type=MEDIA_TYPES.get(target.suffix, "application/octet-stream"),
        headers={"Cache-Control": "no-cache"},
    )


def _asset_version() -> str:
    stamps = []
    for name in ("app.css", "app.js"):
        f = STATIC_DIR / name
        if f.is_file():
            stamps.append(int(f.stat().st_mtime))
    return str(max(stamps)) if stamps else "0"
