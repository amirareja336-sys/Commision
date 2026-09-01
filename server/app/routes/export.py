from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter

from ..schemas import ExportRequest, ExportResponse, LogResponse

router = APIRouter(prefix="/export", tags=["export"])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@router.post("/run", response_model=ExportResponse)
def run_export(payload: ExportRequest) -> ExportResponse:
    repo_root = _repo_root()
    script_path = repo_root / "scripts" / "export_physician_performance.py"
    if not script_path.exists():
        raise FileNotFoundError(f"Exporter script not found: {script_path}")

    log_dir = repo_root / "database"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "export_run.log"

    command = [
        sys.executable,
        str(script_path),
        "--from-date",
        payload.from_date,
        "--to-date",
        payload.to_date,
        "--no-prompt",
        "--no-pause",
    ]

    for physician in payload.physicians:
        command.extend(["--physician", physician])

    if payload.skip_analyzer:
        command.append("--skip-analyzer")

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== Export started: {payload.from_date} -> {payload.to_date} ===\n")
        process = subprocess.Popen(
            command,
            cwd=str(repo_root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    return ExportResponse(
        status="started",
        pid=process.pid,
        log_file=str(log_path),
    )


@router.get("/log", response_model=LogResponse)
def get_log() -> LogResponse:
    repo_root = _repo_root()
    log_path = repo_root / "database" / "export_run.log"
    if not log_path.exists():
        return LogResponse(log_file=str(log_path), content="")

    content = log_path.read_text(encoding="utf-8")
    return LogResponse(log_file=str(log_path), content=content)
