"""GET /about — user guide + Goose Solutions contact details."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.pages import templates

router = APIRouter(tags=["pages"])


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "about.html", {})
