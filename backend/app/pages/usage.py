"""GET /usage — system resource dashboard (CPU, RAM, storage)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.pages import templates

router = APIRouter(tags=["pages"])


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "usage.html", {})
