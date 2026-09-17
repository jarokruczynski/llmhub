from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
    return templates.TemplateResponse(request, "v2.html", {"asset_v": _asset_version()})


@router.get("/v2", include_in_schema=False)
@router.get("/v2/", include_in_schema=False)
async def dashboard_v2_redirect(request: Request) -> RedirectResponse:
    """Where the console used to live while the classic one held the root.

    Relative on purpose: behind the LAN proxy the console is mounted under /hub, and an
    absolute "/" would drop that prefix and land on the machine's home page instead.

    How far up depends on the trailing slash, because that is what the browser resolves
    against: /hub/v2/ resolves against itself, but /hub/v2 resolves against /hub/, where "../"
    is already one level too high.
    """
    target = "../" if request.url.path.endswith("/") else "./"
    return RedirectResponse(url=target, status_code=308)


@router.get("/v2/static/{path:path}", include_in_schema=False)
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
    for name in ("v2/v2.css", "v2/v2.js"):
        f = STATIC_DIR / name
        if f.is_file():
            stamps.append(int(f.stat().st_mtime))
    return str(max(stamps)) if stamps else "0"
