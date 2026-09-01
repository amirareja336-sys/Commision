from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
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

from routers import pipeline, tables, export, scraper, auth_router, admin, reports, runtime, testmode  # noqa: E402

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure repo DB exists (on-disk path) and also ensure a test DB path
    # exists under data/test_1_1/test.db when present. The active DB used
    # by the app is controlled by the COMMISSIONS_DB env var; we avoid
    # mutating that here — initialization only creates files if missing.
    repo_db = dbm.DEFAULT_DB_PATH
    dbm.init_db_at(repo_db)
    dbm.seed_dictionary(APP_ROOT / "dictionary.json")
    # If a test DB is present in data/test_1_1, ensure it has the schema
    test_dir = APP_ROOT / "data" / "test_1_1"
    test_db = test_dir / "test.db"
    if test_dir.exists() and not test_db.exists():
        dbm.init_db_at(test_db)
    # Always run migrations against the active DB path
    dbm._run_migrations()
    import runtime_state as rt  # noqa: E402
    rt.recover_on_startup()
    yield


app = FastAPI(title="Reconciliation Console", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(testmode.router, prefix="/api/testmode", tags=["testmode"])
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(scraper.router, prefix="/api/scraper", tags=["scraper"])
app.include_router(tables.router, prefix="/api/tables", tags=["tables"])
app.include_router(export.router, prefix="/api/export", tags=["export"])
app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
app.include_router(runtime.router, prefix="/api/runtime", tags=["runtime"])


# Lifespan handler performs startup tasks (db init/migrations and runtime recovery)


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
    # Dev users go to the Test UI
    if role == "dev":
        return "/testmode"
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


@app.get("/testmode")
def testmode_page(request: Request):
    session = _current_session(request)
    if not session:
        return RedirectResponse("/login")
    # Only show when the app is running against a test DB.
    if not testmode._test_mode_active():
        return RedirectResponse(_home_for_role(session["role"]))
    if session["role"] not in ("dev", "admin"):
        return RedirectResponse(_home_for_role(session["role"]))
    return FileResponse(FRONTEND_DIR / "testmode.html")


@app.get("/admin/db")
def admin_db_page(request: Request):
    return _admin_page(request, "admin_db.html")
