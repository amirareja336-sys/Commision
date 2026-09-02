"""Scrape IPD Physician Performance rows and mirror them into ipd_mirror."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

APP_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = APP_ROOT / "config.json"
sys.path.insert(0, str(APP_ROOT / "db"))
import db_manager as dbm  # noqa: E402
import abronal_scraper as abr  # noqa: E402

ScraperError = abr.ScraperError
DateRange = abr.DateRange
Physician = abr.Physician
ScraperConfig = abr.ScraperConfig
AbronalSession = abr.AbronalSession


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _map_row(raw: dict, physician: Physician) -> dict:
    return {
        "row_number": raw.get("number"),
        "card_number": str(raw.get("cardNumber") or ""),
        "patient_full_name": str(raw.get("patientFullName") or "").strip(),
        "patient_type": str(raw.get("patientType") or "IPD"),
        "service_raw": str(raw.get("service") or "").strip(),
        "total": float(raw.get("totalPrice") or 0),
        "net": float(raw.get("netAmount") or 0),
        "commission_percent": float(raw.get("commissionPercent") or 0),
        "commision_amount": float(raw.get("commissionAmount") or 0),
        "payment_date": dbm.normalize_date_to_iso(raw.get("collectedDate")) or "",
        "visit_date": dbm.normalize_date_to_iso(raw.get("reportedDate")) or "",
        "status": str(raw.get("paidStatus") or ""),
        "physician_name": physician.display_name,
        "source_file": f"ipd_scraper:{physician.display_name}",
    }


def persist_ipd_mirror(rows: list[dict], batch_id: str, log=print) -> list[dict]:
    """Insert new IPD rows into ipd_mirror (deduped by natural key)."""
    if not rows:
        log("No IPD rows to persist.")
        return []

    def sort_key(r):
        return dbm.normalize_date_to_iso(r.get("payment_date")) or "9999-99-99"

    rows_sorted = sorted(rows, key=sort_key)
    inserted = 0
    with dbm.get_conn() as conn:
        for r in rows_sorted:
            physician_id = dbm.get_or_create_physician(conn, r["physician_name"])
            service_id = dbm.get_or_create_service(conn, r["service_raw"])
            row_key = {
                "patient_full_name": r["patient_full_name"],
                "service_raw": r["service_raw"],
                "net": r["net"],
                "payment_date": r["payment_date"],
                "visit_date": r["visit_date"],
                "status": r["status"],
                "physician_id": physician_id,
                "source_file": r["source_file"],
            }
            if dbm.row_exists(conn, "ipd_mirror", row_key):
                existing_id = dbm.find_row_id(conn, "ipd_mirror", row_key)
                if existing_id is not None:
                    r["row_id"] = existing_id
                    r["physician_id"] = physician_id
                    r["service_id"] = service_id
                continue
            cur = conn.execute(
                """INSERT INTO ipd_mirror
                   (row_number, card_number, patient_full_name, patient_type, service_id,
                    service_raw, total, net, commission_percent, commision_amount,
                    payment_date, visit_date, status, physician_id, source_file, batch_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["row_number"], r["card_number"], r["patient_full_name"], r["patient_type"],
                 service_id, r["service_raw"], r["total"], r["net"], r["commission_percent"],
                 r["commision_amount"], r["payment_date"], r["visit_date"], r["status"],
                 physician_id, r["source_file"], batch_id),
            )
            r["row_id"] = cur.lastrowid
            r["physician_id"] = physician_id
            r["service_id"] = service_id
            inserted += 1
    log(f"Persisted {inserted} new IPD rows to ipd_mirror ({len(rows_sorted)} fetched).")
    return rows_sorted


def load_site(session: AbronalSession, log=print) -> None:
    """Open the Physician Performance report page."""
    log("Opening IPD Physician Performance report…")
    try:
        session.open_report()
    except Exception as exc:
        raise ScraperError("Site could not be loaded") from exc


def fetch_rows_for_physician(
    session: AbronalSession,
    *,
    physician: Physician,
    date_range: DateRange,
    log=print,
) -> list[dict]:
    """Fetch IPD rows for one physician and date range (same browser session)."""
    log(f"  Fetching IPD data for {physician.display_name}…")
    rows = session.fetch_performance_rows(
        physician=physician, patient_type="IPD", date_range=date_range,
    )
    if not rows:
        return []
    return [_map_row(raw, physician) for raw in rows]


# Backwards-compatible alias
load_site_data = fetch_rows_for_physician


def run(
    from_date: str,
    to_date: str,
    batch_id: str,
    physicians: list[str] | None = None,
    log=print,
) -> dict:
    """Scrape IPD Physician Performance and mirror rows into ipd_mirror."""
    cfg = _load_config()
    if not cfg.get("ipd_enabled", True):
        log("IPD scrape skipped (ipd_enabled=false in config.json).")
        return {"skipped": True, "rows": 0}

    if not ScraperConfig.has_credentials():
        log("IPD scrape skipped — Abronal credentials not configured in .env.")
        return {"skipped": True, "rows": 0}

    scraper_cfg = ScraperConfig()
    date_range = DateRange.from_iso(from_date, to_date)
    all_rows: list[dict] = []
    targets: list[Physician] = []

    log("── IPD Scraper: launching browser ──")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=scraper_cfg.headless)
        page = browser.new_page()
        session = AbronalSession(page, scraper_cfg.ipd_base_url)
        try:
            log("Logging in to Abronal (IPD host)…")
            session.login(scraper_cfg.username, scraper_cfg.password, scraper_cfg.role)
            load_site(session, log=log)

            options = session.list_physicians()
            if physicians:
                targets = [abr._resolve_one(options, p) for p in physicians]
            else:
                targets = [
                    p for p in options
                    if not abr._should_skip(p.label, scraper_cfg.skip_physicians)
                ]
            log(f"Will fetch IPD data for {len(targets)} physician(s).")

            for idx, physician in enumerate(targets, start=1):
                log(f"[{idx}/{len(targets)}] {physician.display_name}")
                try:
                    rows = fetch_rows_for_physician(session, physician=physician, date_range=date_range, log=log)
                    all_rows.extend(rows)
                except ScraperError as exc:
                    log(f"  WARNING: {physician.display_name}: {exc}")
        finally:
            browser.close()

    if not all_rows:
        log("IPD scrape finished — no rows returned from the site.")
        return {"rows": 0, "physicians": len(targets)}

    persist_ipd_mirror(all_rows, batch_id, log=log)
    return {"rows": len(all_rows), "physicians": len(targets)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scrape IPD Physician Performance into ipd_mirror")
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument("--batch", default=None)
    parser.add_argument("--physicians", nargs="*", default=None)
    args = parser.parse_args()
    batch = args.batch or dbm.new_batch_id()
    result = run(args.from_date, args.to_date, batch, physicians=args.physicians)
    print(result)
