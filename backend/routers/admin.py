from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import auth  # noqa: E402
import db_manager as dbm  # noqa: E402
import backup_manager as backup  # noqa: E402

router = APIRouter(dependencies=[Depends(auth.require_admin)])


# ── Users ─────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    role: str | None = None
    password: str | None = None


@router.get("/users")
def list_users():
    return {"users": dbm.list_users()}


@router.post("/users")
def create_user(req: CreateUserRequest):
    if dbm.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="Username already exists")
    if req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user_id = dbm.create_user(req.username, req.password, role=req.role)
    return {"user_id": user_id}


@router.put("/users/{user_id}")
def update_user(user_id: int, req: UpdateUserRequest):
    if not dbm.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if req.password and len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    dbm.update_user(user_id, role=req.role, password=req.password)
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, current=Depends(auth.require_admin)):
    if user_id == current["user_id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account while logged in as it")
    if not dbm.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    dbm.delete_user(user_id)
    return {"ok": True}


# ── Per-user table access ────────────────────────────────────────

class TableAccessRequest(BaseModel):
    tables: list[str]


@router.get("/users/{user_id}/table-access")
def get_table_access(user_id: int):
    user = dbm.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "all_tables": dbm.TABLES,
        "allowed_tables": dbm.get_user_allowed_tables(user_id, user["role"]),
    }


@router.put("/users/{user_id}/table-access")
def set_table_access(user_id: int, req: TableAccessRequest):
    if not dbm.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    dbm.set_user_table_access(user_id, req.tables)
    return {"ok": True}


# ── Commission rates ──────────────────────────────────────────────

class CommissionRateRequest(BaseModel):
    commission_percent: float
    category: str | None = None


@router.get("/commission-rates")
def commission_rates():
    return {
        "rates": dbm.list_commission_rates(),
        "categories": list(dbm.CALCULATOR_CATEGORIES),
    }


@router.put("/commission-rates/{physician_id}")
def set_commission_rate(physician_id: int, req: CommissionRateRequest):
    if req.commission_percent < 0 or req.commission_percent > 100:
        raise HTTPException(status_code=400, detail="commission_percent must be between 0 and 100")
    try:
        dbm.set_commission_rate(physician_id, req.commission_percent, req.category)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


# ── Service categories ────────────────────────────────────────────

class ServiceCategoryRequest(BaseModel):
    category: str


@router.get("/service-categories")
def service_categories():
    return {"services": dbm.list_services(), "categories": dbm.VALID_CATEGORIES}


@router.put("/service-categories/{service_id}")
def set_service_category(service_id: int, req: ServiceCategoryRequest):
    if req.category not in dbm.VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of {dbm.VALID_CATEGORIES}")
    dbm.update_service_category(service_id, req.category)
    return {"ok": True}


# ── Commission calculator ─────────────────────────────────────────

@router.get("/commission-calculator/physicians")
def calculator_physicians():
    return {"physicians": dbm.list_commission_rates()}


@router.get("/commission-calculator/calculate")
def calculate_commission(
    physician_id: int,
    from_date: str | None = None,
    to_date: str | None = None,
):
    result = dbm.calculate_physician_commission(physician_id, from_date, to_date)
    if result is None:
        raise HTTPException(status_code=404, detail="Physician not found")
    return result


# ── Submitted accountant reports ─────────────────────────────────

@router.get("/reports/submissions")
def report_submissions():
    return {"submissions": dbm.list_report_submissions()}


@router.get("/reports/physicians")
def report_physicians():
    return {"values": dbm.distinct_report_physicians()}


@router.get("/reports")
def view_reports(
    physician_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    submission_id: str | None = None,
):
    rows = dbm.fetch_reports(
        physician_name=physician_name,
        start_date=start_date,
        end_date=end_date,
        submission_id=submission_id,
    )
    total = dbm.count_reports(
        physician_name=physician_name,
        start_date=start_date,
        end_date=end_date,
        submission_id=submission_id,
    )
    return {"rows": rows, "total": total}


# ── Work-DB spreadsheet editor + backup sync ─────────────────────

class DbRowPayload(BaseModel):
    values: dict = {}


@router.get("/db/tables")
def db_tables():
    return {
        "tables": [
            {"name": t, "pk": dbm.pk_column(t), "columns": dbm.table_columns(t)}
            for t in dbm.EDITABLE_TABLES
        ]
    }


@router.get("/db/tables/{table}")
def db_table_rows(table: str, limit: int = 200, offset: int = 0):
    if table not in dbm.EDITABLE_TABLES:
        raise HTTPException(status_code=404, detail="Unknown or non-editable table")
    try:
        rows = dbm.fetch_editable_table(table, limit=limit, offset=offset)
        total = dbm.count_editable_table(table)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "table": table,
        "pk": dbm.pk_column(table),
        "columns": dbm.table_columns(table),
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.put("/db/tables/{table}/{pk_value}")
def db_update_row(table: str, pk_value: str, req: DbRowPayload):
    if table not in dbm.EDITABLE_TABLES:
        raise HTTPException(status_code=404, detail="Unknown or non-editable table")
    try:
        coerced = _coerce_pk(table, pk_value)
        row = dbm.update_table_row(table, coerced, req.values)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Update failed: {e}") from e
    return {"row": row}


@router.post("/db/tables/{table}")
def db_insert_row(table: str, req: DbRowPayload):
    if table not in dbm.EDITABLE_TABLES:
        raise HTTPException(status_code=404, detail="Unknown or non-editable table")
    try:
        row = dbm.insert_table_row(table, req.values)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Insert failed: {e}") from e
    return {"row": row}


@router.delete("/db/tables/{table}/{pk_value}")
def db_delete_row(table: str, pk_value: str):
    if table not in dbm.EDITABLE_TABLES:
        raise HTTPException(status_code=404, detail="Unknown or non-editable table")
    try:
        dbm.delete_table_row(table, _coerce_pk(table, pk_value))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Delete failed: {e}") from e
    return {"ok": True}


@router.get("/db/backup")
def db_backup_status():
    return backup.backup_status()


@router.post("/db/backup/sync")
def db_backup_sync():
    try:
        return backup.sync_backup_from_work()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


def _coerce_pk(table: str, pk_value: str):
    """PKs are integers for all editable tables today."""
    try:
        return int(pk_value)
    except (TypeError, ValueError):
        return pk_value
