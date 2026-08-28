from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse

APP_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT / "db"))
sys.path.insert(0, str(APP_ROOT / "scripts" / "new"))
sys.path.insert(0, str(BACKEND_DIR))

import db_manager as dbm  # noqa: E402
import auth  # noqa: E402

from routers import pipeline, tables, export, scraper, auth_router, admin, reports, runtime  # noqa: E402

app = FastAPI(title="Reconciliation Console")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(scraper.router, prefix="/api/scraper", tags=["scraper"])
app.include_router(tables.router, prefix="/api/tables", tags=["tables"])
app.include_router(export.router, prefix="/api/export", tags=["export"])
app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
app.include_router(runtime.router, prefix="/api/runtime", tags=["runtime"])


@app.on_event("startup")
def on_startup():
    if not dbm.DB_PATH.exists():
        dbm.init_db()
        dbm.seed_dictionary(APP_ROOT / "dictionary.json")
    else:
        dbm._run_migrations()
    import runtime_state as rt  # noqa: E402
    rt.recover_on_startup()


FRONTEND_DIR = APP_ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def _current_session(request: Request) -> dict | None:
    return auth.session_from_cookie(request.cookies.get(auth.SESSION_COOKIE))


@app.get("/login")
def login_page(request: Request):
    session = _current_session(request)
    if session:
        return RedirectResponse(_home_for_role(session["role"]))
    return FileResponse(FRONTEND_DIR / "login.html")


def _home_for_role(role: str) -> str:
    return "/report" if role == "user" else "/"


@app.get("/")
def index(request: Request):
    session = _current_session(request)
    if not session:
        return RedirectResponse("/login")
    if session["role"] == "user":
        return RedirectResponse("/report")
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/evaluation")
def evaluation(request: Request):
    session = _current_session(request)
    if not session:
        return RedirectResponse("/login")
    if session["role"] == "user":
        return RedirectResponse("/report")
    return FileResponse(FRONTEND_DIR / "evaluation.html")


@app.get("/report")
def report_page(request: Request):
    session = _current_session(request)
    if not session:
        return RedirectResponse("/login")
    if session["role"] == "admin":
        return RedirectResponse("/admin/reports")
    return FileResponse(FRONTEND_DIR / "report.html")


def _admin_page(request: Request, filename: str):
    session = _current_session(request)
    if not session:
        return RedirectResponse("/login")
    if session["role"] != "admin":
        return RedirectResponse(_home_for_role(session["role"]))
    return FileResponse(FRONTEND_DIR / filename)


@app.get("/admin")
def admin_hub(request: Request):
    return _admin_page(request, "admin.html")


@app.get("/admin/users")
def admin_users_page(request: Request):
    return _admin_page(request, "admin_users.html")


@app.get("/admin/commissions")
def admin_commissions_page(request: Request):
    return _admin_page(request, "admin_commissions.html")


@app.get("/admin/categories")
def admin_categories_page(request: Request):
    return _admin_page(request, "admin_categories.html")


@app.get("/admin/commission-calculator")
def admin_commission_calculator_page(request: Request):
    return _admin_page(request, "admin_commission_calculator.html")


@app.get("/admin/reports")
def admin_reports_page(request: Request):
    return _admin_page(request, "admin_reports.html")
