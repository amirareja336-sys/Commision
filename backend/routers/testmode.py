from __future__ import annotations

import asyncio
import csv
import shutil
import subprocess
import sys
import traceback
import zipfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile

import auth  # noqa: E402
import db_manager as dbm  # noqa: E402
import runtime_state as rt  # noqa: E402

APP_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_ROOT / "db"))
sys.path.insert(0, str(APP_ROOT / "scripts" / "new"))
import primary_reconciliation  # noqa: E402
import secondary_name_matcher  # noqa: E402
import category_merger  # noqa: E402
import matched_review_adapter as review  # noqa: E402

router = APIRouter(dependencies=[Depends(auth.require_dev)])

RUN_LOGS: dict[str, list[str]] = {}


def _test_mode_active() -> bool:
    # Only enable the testmode APIs when running against a non-default DB
    default = Path(dbm.DEFAULT_DB_PATH)
    try:
        return Path(dbm.DB_PATH).resolve() != default.resolve()
    except Exception:
        return True


def _require_active() -> None:
    if not _test_mode_active():
        raise HTTPException(status_code=403, detail="Test mode only")


def _data_dir() -> Path:
    return APP_ROOT / "data"


def _csv_datasets_dir() -> Path:
    return _data_dir() / "csv_datasets"


def _emit(batch_id: str, msg: str) -> None:
    RUN_LOGS.setdefault(batch_id, []).append(msg)
    rt.append_line("pipeline", batch_id, msg)


@router.get("/status")
def status():
    data_dir = _data_dir()
    dlist = [str(p.name) for p in sorted(data_dir.glob("test_*")) if p.is_dir()]
    csv_list = (
        [str(p.name) for p in sorted(_csv_datasets_dir().glob("*")) if p.is_dir()]
        if _csv_datasets_dir().exists()
        else []
    )
    return {
        "active": _test_mode_active(),
        "db_path": str(dbm.DB_PATH),
        "data_test_folders": dlist,
        "csv_datasets": csv_list,
    }


@router.get("/metrics")
def metrics():
    _require_active()
    names = [
        "abronal_mirror",
        "sot_mirror",
        "matched_records",
        "unmatched_records",
        "commission_per_physicians",
    ]
    counts = {n: dbm.count_table(n) for n in names}
    return {"counts": counts}


@router.get("/tables")
def tables():
    _require_active()
    out = []
    for t in dbm.TABLES:
        try:
            c = dbm.count_table(t)
        except Exception:
            c = None
        out.append({"name": t, "count": c, "columns": dbm.table_columns(t)})
    return {"tables": out}


@router.post("/clear_table")
def clear_table(payload: dict = Body(...)):
    _require_active()
    table = payload.get("table")
    if table not in dbm.TABLES and table not in dbm.EDITABLE_TABLES:
        raise HTTPException(status_code=400, detail="Unknown table")
    with dbm.get_conn() as conn:
        conn.execute(f'DELETE FROM "{table}"')
    return {"ok": True}


@router.post("/load_dataset")
def load_dataset(payload: dict = Body(...)):
    _require_active()
    folder = payload.get("folder")
    if not folder:
        raise HTTPException(status_code=400, detail="folder required")
    src = _data_dir() / folder / "test.db"
    if not src.exists():
        raise HTTPException(status_code=404, detail="Dataset not found")
    try:
        shutil.copyfile(str(src), str(dbm.DB_PATH))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "db_path": str(dbm.DB_PATH)}


@router.post("/reset_db")
def reset_db():
    _require_active()
    try:
        dbm.init_db()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True}


@router.post("/upload_dataset")
async def upload_dataset(name: str = Form(...), file: UploadFile = File(...)):
    """Upload a .db file or a .zip containing a test.db into data/test_<name>/test.db"""
    _require_active()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
    if not safe:
        raise HTTPException(status_code=400, detail="invalid name")
    dest_dir = _data_dir() / f"test_{safe}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / "test.db"
    try:
        content = await file.read()
        fname = (file.filename or "").lower()
        if fname.endswith(".zip"):
            tmp = dest_dir / "upload_tmp.zip"
            tmp.write_bytes(content)
            with zipfile.ZipFile(tmp, "r") as z:
                z.extractall(dest_dir)
            tmp.unlink(missing_ok=True)
            candidate = next(dest_dir.rglob("test.db"), None)
            if candidate and candidate.resolve() != dest_path.resolve():
                shutil.copyfile(str(candidate), str(dest_path))
            elif not dest_path.exists():
                raise HTTPException(status_code=400, detail="zip has no test.db")
        else:
            dest_path.write_bytes(content)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "dataset": str(dest_path)}


@router.post("/generate")
def generate_scenario(payload: dict = Body(...)):
    """Run the generator script to create a dataset for scenario e.g. '1.1'"""
    _require_active()
    scenario = payload.get("scenario")
    if not scenario:
        raise HTTPException(status_code=400, detail="scenario required")
    gen_script = APP_ROOT / "tests" / "test file generator" / "generate_test_data.py"
    if not gen_script.exists():
        raise HTTPException(status_code=500, detail="generator script not found")
    cmd = [sys.executable, str(gen_script), "--scenario", str(scenario)]
    if payload.get("from_prod"):
        cmd.append("--from-prod")
        if payload.get("start_date"):
            cmd.extend(["--start-date", str(payload["start_date"])])
        if payload.get("end_date"):
            cmd.extend(["--end-date", str(payload["end_date"])])
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=e.stderr or e.stdout or str(e)) from e
    return {"ok": True, "stdout": completed.stdout}


@router.post("/upload_csv")
async def upload_csv(
    table: str = Form(...),
    name: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload a CSV for `table` under data/csv_datasets/<name>/<table>.csv"""
    _require_active()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
    if not safe:
        raise HTTPException(status_code=400, detail="invalid name")
    if table not in dbm.EDITABLE_TABLES and table not in dbm.TABLES:
        raise HTTPException(status_code=400, detail="unknown table")
    data_dir = _csv_datasets_dir() / safe
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = data_dir / f"{table}.csv"
    try:
        content = await file.read()
        dest.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "path": str(dest)}


@router.post("/import_csv_dataset")
def import_csv_dataset(payload: dict = Body(...)):
    """Import CSV files from data/csv_datasets/<name>/ into the active DB."""
    _require_active()
    name = payload.get("name")
    clear_first = bool(payload.get("clear_first", True))
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    base = _csv_datasets_dir() / name
    if not base.exists():
        raise HTTPException(status_code=404, detail="dataset not found")
    imported: dict[str, int | str] = {}
    try:
        with dbm.get_conn() as conn:
            for csv_path in sorted(base.glob("*.csv")):
                table = csv_path.stem
                cols = dbm.table_columns(table) if table in dbm.EDITABLE_TABLES else None
                if cols is None:
                    imported[table] = "skipped: unknown table"
                    continue
                rows = []
                with csv_path.open("r", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for r in reader:
                        filtered = {k: (v if v != "" else None) for k, v in r.items() if k in cols}
                        rows.append(filtered)
                if clear_first:
                    conn.execute(f'DELETE FROM "{table}"')
                if rows:
                    placeholders = ",".join("?" for _ in rows[0])
                    col_names = ",".join(f'"{c}"' for c in rows[0].keys())
                    sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'
                    for row in rows:
                        conn.execute(sql, list(row.values()))
                imported[table] = len(rows)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "imported": imported}


@router.post("/run_reconcile")
async def run_reconcile(payload: dict = Body(default=None)):
    """Run primary + secondary + category merger against current mirror tables."""
    _require_active()
    payload = payload or {}
    clear_results = bool(payload.get("clear_results", True))
    current = rt.get_job("pipeline")
    if current.get("status") == "running" and current.get("batch_id"):
        return {"batch_id": current["batch_id"], "resumed": True}
    batch_id = dbm.new_batch_id()
    RUN_LOGS[batch_id] = []
    rt.start_job("pipeline", batch_id)
    asyncio.get_event_loop().run_in_executor(
        None, _run_reconcile_sync, batch_id, clear_results
    )
    return {"batch_id": batch_id, "resumed": False}


def _run_reconcile_sync(batch_id: str, clear_results: bool) -> None:
    try:
        _emit(batch_id, f"Starting test-mode reconcile {batch_id}")
        _emit(batch_id, "STEP 1/3 — Primary reconciliation (from mirrors)")
        r1 = primary_reconciliation.run_from_mirrors(
            batch_id, log=lambda m: _emit(batch_id, m), clear_results=clear_results
        )
        _emit(batch_id, f"Primary reconciliation done: {r1}")

        _emit(batch_id, "STEP 2/3 — Secondary name matching")
        r2 = secondary_name_matcher.run(batch_id, log=lambda m: _emit(batch_id, m))
        _emit(batch_id, f"Secondary name matching done: {r2}")

        _emit(batch_id, "STEP 3/3 — Category merger")
        r3 = category_merger.run(batch_id, log=lambda m: _emit(batch_id, m))
        _emit(batch_id, f"Category merger done: {len(r3)} rows condensed.")

        review.invalidate_review_cache()
        _emit(batch_id, "Accountant review cache cleared.")
        _emit(batch_id, "PIPELINE_DONE::success")
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        _emit(batch_id, f"ERROR: {e}\n{tb}")
        _emit(batch_id, "PIPELINE_DONE::failed")


@router.get("/run_log/{batch_id}")
def run_log(batch_id: str):
    _require_active()
    if batch_id in RUN_LOGS:
        return {"lines": RUN_LOGS[batch_id]}
    job = rt.get_job("pipeline")
    if job.get("batch_id") == batch_id:
        return {"lines": job.get("lines") or []}
    return {"lines": []}
