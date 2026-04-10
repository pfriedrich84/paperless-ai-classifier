"""OCR correction routes — placeholder for future implementation."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/ocr")


@router.get("")
async def ocr_list(request: Request):
    return request.app.state.templates.TemplateResponse(
        "ocr.html",
        {"request": request},
    )
