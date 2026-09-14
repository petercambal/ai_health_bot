import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import database
from app.link_token import verify_link_token
from app.nutrition import web_login

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Same trust model as /garmin-login (see app/routers/garmin_login.py) - the link is
# signed and scoped to the requester's own telegram_id via app/link_token.py, so
# there's no separate session/cookie auth layer here.

_INVALID_LINK_ERROR = "This link is invalid or has expired. Request a new one via /nutrition_link in Telegram."


async def _resolve_user_id(link_token: str) -> int | None:
    user_id = verify_link_token(link_token)
    if user_id is None:
        return None
    if not await database.is_authorized_user(user_id):
        return None
    return user_id


@router.get("/nutrition-login", response_class=HTMLResponse)
async def nutrition_login_form(request: Request, token: str) -> HTMLResponse:
    user_id = await _resolve_user_id(token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "nutrition_login.html", {"stage": "invalid", "error": _INVALID_LINK_ERROR}
        )
    return templates.TemplateResponse(
        request, "nutrition_login.html", {"stage": "password", "link_token": token}
    )


@router.post("/nutrition-login", response_class=HTMLResponse)
async def nutrition_login_submit(
    request: Request,
    link_token: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    user_id = await _resolve_user_id(link_token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "nutrition_login.html", {"stage": "invalid", "error": _INVALID_LINK_ERROR}
        )

    try:
        cookies = await web_login.login(email, password)
    except web_login.NutritionLoginFailed:
        return templates.TemplateResponse(
            request,
            "nutrition_login.html",
            {
                "stage": "password",
                "link_token": link_token,
                "error": "Login failed. Check your email and password and try again.",
            },
        )
    except Exception:
        logger.exception("kaloricketabulky.sk login failed unexpectedly (user_id=%s)", user_id)
        return templates.TemplateResponse(
            request,
            "nutrition_login.html",
            {
                "stage": "password",
                "link_token": link_token,
                "error": "Login failed. Check your email and password and try again.",
            },
        )

    await database.save_nutrition_cookies(user_id, cookies, label=email)
    return templates.TemplateResponse(request, "nutrition_login.html", {"stage": "success", "email": email})
