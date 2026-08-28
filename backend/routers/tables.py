from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

APP_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_ROOT / "db"))
sys.path.insert(0, str(APP_ROOT / "backend"))
import db_manager as dbm  # noqa: E402
import auth  # noqa: E402

router = APIRouter(dependencies=[Depends(auth.require_user)])


def _check_table_access(table: str, user: dict) -> None:
    allowed = dbm.get_user_allowed_tables(user["user_id"], user["role"])
    if table not in allowed:
        raise HTTPException(status_code=403, detail=f"You don't have access to '{table}'")


@router.get("/date-columns")
def date_columns(user=Depends(auth.require_user)):
    allowed = set(dbm.get_user_allowed_tables(user["user_id"], user["role"]))
    return {
        "date_columns": {
            table: col for table, col in dbm.TABLE_DATE_COLUMNS.items() if table in allowed
        }
    }


@router.get("/list")
def list_tables(user=Depends(auth.require_user)):
    return {"tables": dbm.get_user_allowed_tables(user["user_id"], user["role"])}


@router.get("/{table}/columns")
def columns(table: str, user=Depends(auth.require_user)):
    _check_table_access(table, user)
    return {"columns": dbm.table_columns(table)}


@router.get("/{table}")
def get_table(table: str, request: Request, limit: int = Query(1000, le=1000),
              offset: int = Query(0, ge=0), date_column: str | None = None,
              start_date: str | None = None, end_date: str | None = None,
              user=Depends(auth.require_user)):
    _check_table_access(table, user)
    # Any query param other than the reserved pagination/date-range
    # ones is treated as a column filter, e.g.
    # GET /api/tables/matched_records?physician_id=3
    # Filtering (via db_manager) always runs against the whole table in
    # SQL, then only the requested 1000-row chunk of the (filtered)
    # result set is returned — never a filter over an already-loaded
    # chunk.
    reserved = {"limit", "offset", "date_column", "start_date", "end_date"}
    filters = {k: v for k, v in request.query_params.items() if k not in reserved}
    filters = filters or None
    rows = dbm.fetch_table(table, filters=filters, limit=limit, offset=offset,
                            date_column=date_column, start_date=start_date, end_date=end_date)
    total = dbm.count_table(table, filters=filters, date_column=date_column,
                             start_date=start_date, end_date=end_date)
    return {"rows": rows, "total": total, "offset": offset, "limit": limit}


@router.get("/{table}/distinct/{column}")
def distinct_values(table: str, column: str, user=Depends(auth.require_user)):
    _check_table_access(table, user)
    return {"values": dbm.fetch_distinct(table, column)}


@router.post("/{table}/filter")
def filter_table(table: str, filters: dict, limit: int = 1000, offset: int = 0,
                  user=Depends(auth.require_user)):
    _check_table_access(table, user)
    rows = dbm.fetch_table(table, filters=filters, limit=limit, offset=offset)
    total = dbm.count_table(table, filters=filters)
    return {"rows": rows, "total": total, "offset": offset, "limit": limit}


# ── User flagging of matched records ───────────────────────────────
from pydantic import BaseModel  # noqa: E402


class FlagMismatchRequest(BaseModel):
    flagged: bool
    reason: str | None = None


@router.post("/matched_records/{match_id}/flag")
def flag_matched_record(match_id: int, req: FlagMismatchRequest, user=Depends(auth.require_user)):
    """Allow users to flag a matched record as a mismatch they found."""
    _check_table_access("matched_records", user)
    with dbm.get_conn() as conn:
        row = conn.execute(
            "SELECT match_id FROM matched_records WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Matched record not found")
        conn.execute(
            """UPDATE matched_records
               SET user_flagged_mismatch = ?, user_flag_reason = ?
               WHERE match_id = ?""",
            (1 if req.flagged else 0, req.reason if req.flagged else None, match_id),
        )
    return {"ok": True, "match_id": match_id, "flagged": req.flagged}
