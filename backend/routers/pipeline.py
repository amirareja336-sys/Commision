from __future__ import annotations

import asyncio
import shutil
import sys
import traceback
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, UploadFile, File, WebSocket, WebSocketDisconnect

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

router = APIRouter()

# batch_id -> list[str] log lines. Progress/status is tracked purely
# in-memory here (streamed to the browser over the websocket below) —
# there is no persisted pipeline_runs table.
RUN_LOGS: Dict[str, List[str]] = {}
RUN_LISTENERS: Dict[str, List[asyncio.Queue]] = {}


def _emit(batch_id: str, message: str):
    RUN_LOGS.setdefault(batch_id, []).append(message)
    for q in RUN_LISTENERS.get(batch_id, []):
        q.put_nowait(message)


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
        "sot": sorted(p.name for p in UPLOAD_SOT_DIR.glob("*.xlsx")),
        "abronal": sorted(p.name for p in UPLOAD_ABR_DIR.glob("*.xlsx")),
    }


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

        _emit(batch_id, "PIPELINE_DONE::success")
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        _emit(batch_id, f"ERROR: {e}\n{tb}")
        _emit(batch_id, "PIPELINE_DONE::failed")


@router.post("/run")
async def run_pipeline(user=Depends(auth.require_admin)):
    batch_id = dbm.new_batch_id()
    RUN_LOGS[batch_id] = []
    RUN_LISTENERS[batch_id] = []
    asyncio.get_event_loop().run_in_executor(None, _run_pipeline_sync, batch_id)
    return {"batch_id": batch_id}


@router.get("/log/{batch_id}")
def get_log(batch_id: str, user=Depends(auth.require_admin)):
    return {"lines": RUN_LOGS.get(batch_id, [])}


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
