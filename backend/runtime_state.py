"""Volatile runtime state for long-running jobs and UI page switches.

Persisted as temp/runtime_state.json so Intake scrape/pipeline progress and
page UI filters survive navigating away and come back. Not a database —
cleared/overwritten as jobs finish; surviving "running" jobs are marked
failed on server restart.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
TEMP_DIR = APP_ROOT / "temp"
STATE_PATH = TEMP_DIR / "runtime_state.json"
_LOCK = threading.Lock()
_MAX_LINES = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_job() -> dict[str, Any]:
    return {
        "batch_id": None,
        "status": "idle",  # idle | running | success | failed
        "lines": [],
        "started_at": None,
        "finished_at": None,
    }


def _default_state() -> dict[str, Any]:
    return {
        "pipeline": _empty_job(),
        "scrape": {
            **_empty_job(),
            "from_date": None,
            "to_date": None,
            "physicians": None,
        },
        "ipd_reconcile": _empty_job(),
        "ui": {
            "intake": {},
            "evaluation": {},
            "report": {},
        },
    }


def load() -> dict[str, Any]:
    with _LOCK:
        return _load_unlocked()


def _load_unlocked() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    base = _default_state()
    for key in ("pipeline", "scrape", "ipd_reconcile", "ui"):
        if isinstance(data.get(key), dict):
            if key == "ui":
                for page in ("intake", "evaluation", "report"):
                    if isinstance(data["ui"].get(page), dict):
                        base["ui"][page] = data["ui"][page]
            else:
                base[key].update(data[key])
    return base


def _save_unlocked(state: dict[str, Any]) -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def save(state: dict[str, Any]) -> None:
    with _LOCK:
        _save_unlocked(state)


def recover_on_startup() -> None:
    """Any job marked running when the process starts was killed with the old process."""
    with _LOCK:
        state = _load_unlocked()
        dirty = False
        for kind, done_prefix in (
            ("pipeline", "PIPELINE_DONE"),
            ("scrape", "SCRAPE_DONE"),
            ("ipd_reconcile", "IPD_RECON_DONE"),
        ):
            job = state[kind]
            if job.get("status") == "running":
                job["status"] = "failed"
                job["finished_at"] = _now()
                lines = list(job.get("lines") or [])
                lines.append("ERROR: Server restarted while this job was running.")
                lines.append(f"{done_prefix}::failed")
                job["lines"] = lines[-_MAX_LINES:]
                dirty = True
        if dirty:
            _save_unlocked(state)


def start_job(kind: str, batch_id: str, **extra: Any) -> dict[str, Any]:
    with _LOCK:
        state = _load_unlocked()
        job = state[kind]
        job["batch_id"] = batch_id
        job["status"] = "running"
        job["lines"] = []
        job["started_at"] = _now()
        job["finished_at"] = None
        for k, v in extra.items():
            job[k] = v
        _save_unlocked(state)
        return dict(job)


def append_line(kind: str, batch_id: str, message: str) -> None:
    with _LOCK:
        state = _load_unlocked()
        job = state[kind]
        if job.get("batch_id") != batch_id:
            return
        lines = list(job.get("lines") or [])
        lines.append(message)
        job["lines"] = lines[-_MAX_LINES:]
        if message.startswith("PIPELINE_DONE::") or message.startswith("SCRAPE_DONE::") or message.startswith("IPD_RECON_DONE::"):
            job["status"] = "success" if message.endswith("success") else "failed"
            job["finished_at"] = _now()
        _save_unlocked(state)


def get_job(kind: str) -> dict[str, Any]:
    return load()[kind]


def get_ui(page: str | None = None) -> dict[str, Any]:
    ui = load()["ui"]
    if page:
        return dict(ui.get(page) or {})
    return ui


def set_ui(page: str, payload: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        state = _load_unlocked()
        state["ui"][page] = dict(payload or {})
        _save_unlocked(state)
        return state["ui"][page]
