from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import auth
import runtime_state as rt

router = APIRouter(dependencies=[Depends(auth.require_user)])


class UiPayload(BaseModel):
    page: str
    state: dict = {}


@router.get("/status")
def runtime_status(user=Depends(auth.require_user)):
    """Snapshot of active/recent jobs + UI page state for restore on navigation."""
    state = rt.load()
    # Non-admins only need UI (report) state; jobs are admin-only.
    if user.get("role") != "admin":
        return {"pipeline": None, "scrape": None, "ui": {"report": state["ui"].get("report", {})}}
    return state


@router.put("/ui")
def save_ui(req: UiPayload, user=Depends(auth.require_user)):
    page = (req.page or "").strip().lower()
    allowed = {"intake", "evaluation", "report"}
    if page not in allowed:
        raise HTTPException(status_code=400, detail=f"page must be one of {sorted(allowed)}")
    if user.get("role") != "admin" and page != "report":
        raise HTTPException(status_code=403, detail="Only report UI state is available for this role")
    if user.get("role") == "admin" and page == "report":
        raise HTTPException(status_code=400, detail="Admins do not use the report page state")
    return {"page": page, "state": rt.set_ui(page, req.state)}


@router.get("/ui/{page}")
def load_ui(page: str, user=Depends(auth.require_user)):
    page = page.strip().lower()
    if page not in ("intake", "evaluation", "report"):
        raise HTTPException(status_code=404, detail="Unknown page")
    if user.get("role") != "admin" and page != "report":
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"page": page, "state": rt.get_ui(page)}
