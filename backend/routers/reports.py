from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

APP_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_ROOT / "db"))
sys.path.insert(0, str(APP_ROOT / "backend"))
import db_manager as dbm  # noqa: E402
import auth  # noqa: E402
import matched_review_adapter as review  # noqa: E402

router = APIRouter(dependencies=[Depends(auth.require_user)])


class SendReportRequest(BaseModel):
    physician_name: str | None = None
    start_date: str | None = None
    end_date: str | None = None


def _physician_filters(physician_name: str | None) -> dict | None:
    name = (physician_name or "").strip()
    if not name or name.lower() == "all":
        return None
    return {"physician_name": name}


@router.get("/preview")
def preview_report(
    physician_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    user=Depends(auth.require_user),
):
    """Condensed matched-records view for the user Report page."""
    if user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Use Admin → View Reports")
    filters = _physician_filters(physician_name)
    rows = review.fetch_review(
        filters=filters, limit=200000, offset=0,
        start_date=start_date, end_date=end_date,
        include_payment_date=True,
    )
    return {
        "columns": list(review.REVIEW_COLUMNS),
        "rows": rows,
        "total": len(rows),
    }


@router.get("/physicians")
def report_physicians(user=Depends(auth.require_user)):
    if user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Use Admin → View Reports")
    return {"values": review.distinct_review("physician_name")}


@router.post("/send")
def send_report(req: SendReportRequest, user=Depends(auth.require_user)):
    if user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Use Admin → View Reports")
    filters = _physician_filters(req.physician_name)
    rows = review.fetch_review(
        filters=filters, limit=200000, offset=0,
        start_date=req.start_date, end_date=req.end_date,
        include_payment_date=True,
    )
    if not rows:
        raise HTTPException(status_code=400, detail="Nothing to send — the current filters match no rows")
    result = dbm.insert_report_snapshot(
        user, rows,
        physician_filter=req.physician_name,
        start_date=req.start_date,
        end_date=req.end_date,
    )
    return {"ok": True, **result}
