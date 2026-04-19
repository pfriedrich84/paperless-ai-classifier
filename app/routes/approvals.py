"""Approvals entry route (parent nav for entity approvals)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/approvals")


@router.get("")
async def approvals_home():
    """Redirect to the default approvals sub-page."""
    return RedirectResponse(url="/tags", status_code=302)
