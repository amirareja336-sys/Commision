"""Read-only archival backup of commissions.db.

The live app always uses db/commissions.db (work DB). This module maintains
db/commissions_backup.db:

  * New rows from work are appended (INSERT OR IGNORE by primary key) —
    existing backup rows are never updated, so the archive is immutable.
  * unmatched_records is the exception: rows deleted from work (e.g. later
    rematch → matched) are also deleted from the backup.
  * Nothing in the normal pipeline opens the backup for business logic.

Call sync_backup_from_work() after a successful pipeline run or from Admin.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent
APP_ROOT = DB_DIR.parent
WORK_DB = DB_DIR / "commissions.db"
BACKUP_DB = DB_DIR / "commissions_backup.db"
SCHEMA_PATH = DB_DIR / "schema.sql"
META_PATH = APP_ROOT / "temp" / "backup_meta.json"

# Data tables mirrored into the backup (no users / auth).
BACKUP_TABLES = [
    "physicians",
    "service_prices",
    "physician_category_commision_rates",
    "abronal_mirror",
    "ipd_mirror",
    "sot_mirror",
    "matched_records",
    "unmatched_records",
    "commission_per_physicians",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_backup_schema(conn: sqlite3.Connection) -> None:
    migrations = [
        ("commission_per_physicians", "physician_name", "TEXT NOT NULL DEFAULT ''"),
        ("matched_records", "user_flagged_mismatch", "INTEGER NOT NULL DEFAULT 0"),
        ("matched_records", "user_flag_reason", "TEXT DEFAULT NULL"),
        ("matched_records", "physician_name", "TEXT NOT NULL DEFAULT ''"),
        ("unmatched_records", "physician_name", "TEXT NOT NULL DEFAULT ''"),
        ("matched_records", "source", "TEXT NOT NULL DEFAULT 'OPD'"),
        ("matched_records", "ipd_row_id", "INTEGER"),
    ]
    for table, column, decl in migrations:
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if not existing:
            continue
        if column not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')


def _ensure_backup_schema() -> None:
    BACKUP_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BACKUP_DB)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate_backup_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _write_meta(payload: dict) -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def backup_status() -> dict:
    meta = {}
    if META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    counts = {}
    exists = BACKUP_DB.exists()
    if exists:
        conn = sqlite3.connect(BACKUP_DB)
        conn.row_factory = sqlite3.Row
        try:
            for table in BACKUP_TABLES:
                try:
                    row = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()
                    counts[table] = int(row["c"]) if row else 0
                except sqlite3.Error:
                    counts[table] = None
        finally:
            conn.close()
    return {
        "path": str(BACKUP_DB),
        "exists": exists,
        "last_sync_at": meta.get("last_sync_at"),
        "last_sync": meta.get("last_sync"),
        "counts": counts,
    }


def sync_backup_from_work() -> dict:
    """Append new work rows into the backup; sync unmatched deletions."""
    if not WORK_DB.exists():
        raise FileNotFoundError(f"Work database not found: {WORK_DB}")

    _ensure_backup_schema()
    work = sqlite3.connect(WORK_DB)
    try:
        work.execute("ATTACH DATABASE ? AS bak", (str(BACKUP_DB),))
        work.execute("PRAGMA bak.foreign_keys = OFF")

        inserted: dict[str, int] = {}
        deleted_unmatched = 0

        # Ensure dependency order for IGNORE inserts with FKs off.
        for table in BACKUP_TABLES:
            if table == "unmatched_records":
                continue
            before = work.execute(f'SELECT COUNT(*) AS c FROM bak."{table}"').fetchone()[0]
            work.execute(
                f'INSERT OR IGNORE INTO bak."{table}" SELECT * FROM main."{table}"'
            )
            after = work.execute(f'SELECT COUNT(*) AS c FROM bak."{table}"').fetchone()[0]
            inserted[table] = max(0, after - before)

        # Unmatched: allow deletes when work no longer has the row (rematch).
        cur = work.execute(
            """
            DELETE FROM bak.unmatched_records
            WHERE unmatched_id NOT IN (
                SELECT unmatched_id FROM main.unmatched_records
            )
            """
        )
        deleted_unmatched = cur.rowcount if cur.rowcount is not None else 0
        before_u = work.execute('SELECT COUNT(*) AS c FROM bak.unmatched_records').fetchone()[0]
        work.execute(
            'INSERT OR IGNORE INTO bak.unmatched_records SELECT * FROM main.unmatched_records'
        )
        after_u = work.execute('SELECT COUNT(*) AS c FROM bak.unmatched_records').fetchone()[0]
        inserted["unmatched_records"] = max(0, after_u - before_u)

        work.commit()
        work.execute("DETACH DATABASE bak")
    finally:
        work.close()

    result = {
        "ok": True,
        "synced_at": _now(),
        "inserted": inserted,
        "deleted_unmatched": deleted_unmatched,
        "path": str(BACKUP_DB),
    }
    _write_meta({"last_sync_at": result["synced_at"], "last_sync": result})
    return result
