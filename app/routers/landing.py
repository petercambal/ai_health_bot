"""Public landing page at "/" - a user-facing page (what the bot does, how to use
it, where to find it) for the app's own domain
(https://longevity.curlybrackets.sk via the NAS reverse proxy), as opposed to
README.md which is developer-facing documentation."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "landing.html", {})
