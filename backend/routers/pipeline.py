from __future__ import annotations

import asyncio
import shutil
import sys
import traceback
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from urllib.parse import unquote

APP_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = APP_ROOT / "data"
UPLOAD_SOT_DIR = DATA_DIR / "uploads" / "sot"
UPLOAD_ABR_DIR = DATA_DIR / "uploads" / "abronal"
for d in (UPLOAD_SOT_DIR, UPLOAD_ABR_DIR):
    d.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(APP_ROOT / "db"))
sys.path.insert(0, str(APP_ROOT / "scripts" / "new"))
sys.path.insert(0, str(APP_ROOT / "backend"))
import db_manager as dbm  # noqa: E402
import primary_reconciliation  # noqa: E402
import secondary_name_matcher  # noqa: E402
import category_merger  # noqa: E402
import matched_review_adapter as review  # noqa: E402
import auth  # noqa: E402
import runtime_state as rt  # noqa: E402
import service_discovery  # noqa: E402

router = APIRouter()

UPLOAD_EXTENSIONS = {".xlsx", ".xls", ".csv", ".tsv"}

# batch_id -> list[str] log lines. Also mirrored into temp/runtime_state.json
# so progress survives page navigation (and can be restored after a refresh).
RUN_LOGS: Dict[str, List[str]] = {}
RUN_LISTENERS: Dict[str, List[asyncio.Queue]] = {}


def _emit(batch_id: str, message: str):
    RUN_LOGS.setdefault(batch_id, []).append(message)
    for q in RUN_LISTENERS.get(batch_id, []):
        q.put_nowait(message)
    rt.append_line("pipeline", batch_id, message)


@router.post("/upload/sot")
async def upload_sot(files: List[UploadFile] = File(...), user=Depends(auth.require_admin)):
    saved = []
    for f in files:
        dest = UPLOAD_SOT_DIR / f.filename
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(f.filename)
    return {"saved": saved, "count": len(saved)}


@router.post("/upload/abronal")
async def upload_abronal(files: List[UploadFile] = File(...), user=Depends(auth.require_admin)):
    saved = []
    for f in files:
        dest = UPLOAD_ABR_DIR / f.filename
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(f.filename)
    return {"saved": saved, "count": len(saved)}


@router.get("/uploads")
def list_uploads(user=Depends(auth.require_admin)):
    return {
        "sot": _list_upload_names(UPLOAD_SOT_DIR),
        "abronal": _list_upload_names(UPLOAD_ABR_DIR),
    }


def _list_upload_names(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in UPLOAD_EXTENSIONS
    )


def _upload_root(kind: str) -> Path:
    if kind == "sot":
        return UPLOAD_SOT_DIR
    if kind == "abronal":
        return UPLOAD_ABR_DIR
    raise HTTPException(status_code=404, detail="Unknown upload kind")


def _safe_upload_path(kind: str, filename: str) -> Path:
    root = _upload_root(kind).resolve()
    name = Path(unquote(filename)).name
    if not name or name in (".", "..") or Path(name).suffix.lower() not in UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = (root / name).resolve()
    if path.parent != root:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return path


def _guard_idle_pipeline() -> None:
    job = rt.get_job("pipeline")
    if job.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail="Cannot remove uploads while reconciliation is running",
        )


@router.delete("/uploads/{kind}/{filename:path}")
def delete_upload(kind: str, filename: str, user=Depends(auth.require_admin)):
    _guard_idle_pipeline()
    path = _safe_upload_path(kind, filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    path.unlink()
    return {"deleted": path.name, "kind": kind}


@router.get("/new-services")
def list_new_services(user=Depends(auth.require_admin)):
    """Services in current uploads that are missing from dictionary.json."""
    return service_discovery.discover_new_services(UPLOAD_ABR_DIR, UPLOAD_SOT_DIR)


class CategorizeServicesRequest(BaseModel):
    assignments: dict[str, str]


@router.post("/categorize-services")
def categorize_services(req: CategorizeServicesRequest, user=Depends(auth.require_admin)):
    """Save admin categories into dictionary.json and service_prices."""
    try:
        result = dbm.save_dictionary_entries(req.assignments)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    remaining = service_discovery.discover_new_services(UPLOAD_ABR_DIR, UPLOAD_SOT_DIR)
    return {**result, "remaining": remaining}


@router.post("/run")
async def run_pipeline(user=Depends(auth.require_admin)):
    pending = service_discovery.discover_new_services(UPLOAD_ABR_DIR, UPLOAD_SOT_DIR)
    if pending.get("new_services"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{len(pending['new_services'])} new service(s) need categories "
                    "before reconciliation can run."
                ),
                "new_services": pending["new_services"],
                "categories": pending["categories"],
            },
        )
    current = rt.get_job("pipeline")
    if current.get("status") == "running" and current.get("batch_id"):
        return {"batch_id": current["batch_id"], "resumed": True}
    batch_id = dbm.new_batch_id()
    RUN_LOGS[batch_id] = []
    RUN_LISTENERS[batch_id] = []
    rt.start_job("pipeline", batch_id)
    asyncio.get_event_loop().run_in_executor(None, _run_pipeline_sync, batch_id)
    return {"batch_id": batch_id, "resumed": False}


def _run_pipeline_sync(batch_id: str):
    try:
        _emit(batch_id, f"Starting pipeline run {batch_id}")

        _emit(batch_id, "STEP 1/3 — Primary reconciliation")
        r1 = primary_reconciliation.run(
            str(UPLOAD_ABR_DIR), str(UPLOAD_SOT_DIR), batch_id, log=lambda m: _emit(batch_id, m)
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

        try:
            import backup_manager as backup  # noqa: E402
            sync = backup.sync_backup_from_work()
            _emit(
                batch_id,
                "Backup DB synced "
                f"(+{sum(sync.get('inserted', {}).values())} rows, "
                f"-{sync.get('deleted_unmatched', 0)} unmatched).",
            )
        except Exception as be:  # noqa: BLE001
            _emit(batch_id, f"WARNING: backup sync failed: {be}")

        _emit(batch_id, "PIPELINE_DONE::success")
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        _emit(batch_id, f"ERROR: {e}\n{tb}")
        _emit(batch_id, "PIPELINE_DONE::failed")


@router.get("/status")
def pipeline_status(user=Depends(auth.require_admin)):
    job = rt.get_job("pipeline")
    bid = job.get("batch_id")
    if bid and bid in RUN_LOGS:
        job = {**job, "lines": list(RUN_LOGS[bid])}
    return job


@router.get("/log/{batch_id}")
def get_log(batch_id: str, user=Depends(auth.require_admin)):
    if batch_id in RUN_LOGS:
        return {"lines": RUN_LOGS[batch_id]}
    job = rt.get_job("pipeline")
    if job.get("batch_id") == batch_id:
        return {"lines": job.get("lines") or []}
    return {"lines": []}


@router.websocket("/ws/{batch_id}")
async def ws_log(websocket: WebSocket, batch_id: str):
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue()
    RUN_LISTENERS.setdefault(batch_id, []).append(q)
    try:
        for line in RUN_LOGS.get(batch_id, []):
            await websocket.send_text(line)
        while True:
            line = await q.get()
            await websocket.send_text(line)
            if line.startswith("PIPELINE_DONE::"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        RUN_LISTENERS.get(batch_id, []).remove(q)
