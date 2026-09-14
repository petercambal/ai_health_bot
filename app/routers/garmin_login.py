import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import database
from app.garmin import web_login
from app.link_token import verify_link_token

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Nobody types this URL themselves - the bot hands it out via /garmin_link, signed
# and scoped to the requester's own telegram_id (see app/link_token.py), so
# there's no separate session/cookie auth layer here. Still re-checked against
# auth_user in case access was revoked between the link being issued and used.

_INVALID_LINK_ERROR = "This link is invalid or has expired. Request a new one via /garmin_link in Telegram."


async def _resolve_user_id(link_token: str) -> int | None:
    user_id = verify_link_token(link_token)
    if user_id is None:
        return None
    if not await database.is_authorized_user(user_id):
        return None
    return user_id


@router.get("/garmin-login", response_class=HTMLResponse)
async def garmin_login_form(request: Request, token: str) -> HTMLResponse:
    user_id = await _resolve_user_id(token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "garmin_login.html", {"stage": "invalid", "error": _INVALID_LINK_ERROR}
        )
    return templates.TemplateResponse(request, "garmin_login.html", {"stage": "password", "link_token": token})


@router.post("/garmin-login", response_class=HTMLResponse)
async def garmin_login_submit(
    request: Request,
    link_token: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    user_id = await _resolve_user_id(link_token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "garmin_login.html", {"stage": "invalid", "error": _INVALID_LINK_ERROR}
        )

    try:
        result, mfa_required = await asyncio.to_thread(web_login.start_login, email, password)
    except Exception:
        logger.exception("Garmin web login failed to start (user_id=%s)", user_id)
        return templates.TemplateResponse(
            request,
            "garmin_login.html",
            {
                "stage": "password",
                "link_token": link_token,
                "error": "Login failed. Check your email and password and try again.",
            },
        )

    if mfa_required:
        return templates.TemplateResponse(
            request,
            "garmin_login.html",
            {"stage": "mfa", "link_token": link_token, "mfa_token": result, "email": email},
        )

    await database.save_garmin_session(user_id, email, result)
    return templates.TemplateResponse(request, "garmin_login.html", {"stage": "success", "email": email})


@router.post("/garmin-login/mfa", response_class=HTMLResponse)
async def garmin_login_mfa(
    request: Request,
    link_token: str = Form(...),
    mfa_token: str = Form(...),
    email: str = Form(...),
    mfa_code: str = Form(...),
) -> HTMLResponse:
    user_id = await _resolve_user_id(link_token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "garmin_login.html", {"stage": "invalid", "error": _INVALID_LINK_ERROR}
        )

    try:
        session_json = await asyncio.to_thread(web_login.complete_login, mfa_token, mfa_code)
    except Exception:
        logger.exception("Garmin web MFA completion failed (user_id=%s)", user_id)
        return templates.TemplateResponse(
            request,
            "garmin_login.html",
            {
                "stage": "mfa",
                "link_token": link_token,
                "mfa_token": mfa_token,
                "email": email,
                "error": "Wrong code or it expired. Please try again.",
            },
        )

    await database.save_garmin_session(user_id, email, session_json)
    return templates.TemplateResponse(request, "garmin_login.html", {"stage": "success", "email": email})
