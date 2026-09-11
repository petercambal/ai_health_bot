from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import database

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user_id: int, typ: str = "weight") -> HTMLResponse:
    available_types = await database.get_distinct_types(user_id)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user_id": user_id, "selected_typ": typ, "available_types": available_types},
    )


@router.get("/api/stats", response_class=JSONResponse)
async def api_stats(
    user_id: int,
    typ: str,
    limit: int = Query(default=100, ge=1, le=1000),
) -> JSONResponse:
    records = await database.get_records_for_dashboard(user_id=user_id, typ=typ, limit=limit)
    return JSONResponse(content={"user_id": user_id, "typ": typ, "records": records})
