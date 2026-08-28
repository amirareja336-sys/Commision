from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date as _date, datetime, timezone
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent
DB_PATH = DB_DIR / "commissions.db"
SCHEMA_PATH = DB_DIR / "schema.sql"

# Tables a role='user' account can see on the Evaluation page unless an
# admin has explicitly customized their access via user_table_access.
DEFAULT_USER_TABLES = ["abronal_mirror", "sot_mirror", "matched_records", "unmatched_records"]

VALID_CATEGORIES = [
    "Consultation", "Laboratory", "X-ray", "Ultrasound",
    "ECG", "Echocardiography", "Nursing & Procedures", "Supplies", "Other",
]

CATEGORY_COLUMN_MAP = {
    "Ultrasound": "ultrasound",
    "Laboratory": "laboratory",
    "X-ray": "x-ray",
    "Nursing & Procedures": "nursing_and_procedures",
    "Consultation": "consultation",
}


def new_batch_id() -> str:
    return uuid.uuid4().hex[:12]


# ── Date normalization ────────────────────────────────────────────
# Payment/visit/transaction dates come out of Excel in whatever shape
# the source file happened to use — most commonly either an
# ISO-ish string produced by pandas turning a date-typed cell into a
# Timestamp ("2026-07-02 00:00:00") or plain dd/mm/yyyy text typed
# into the sheet, sometimes with a trailing time ("02/07/2026 14:30").
# SQLite's own date() function only understands the ISO shape, so a
# dd/mm/yyyy value silently fails to parse and the row drops out of
# every date-range filter — this is what "the date filter just
# doesn't work" turned out to be. normalize_date_to_iso() below
# strips any trailing HH:MM[:SS] first, then tries ISO (yyyy-mm-dd)
# and only then dd/mm/yyyy (day-first, matching the source data —
# never month-first), returning a plain 'YYYY-MM-DD' string SQLite
# can compare lexically, or None if the value truly can't be parsed
# (which safely excludes it from range filters rather than erroring).

def normalize_date_to_iso(value) -> str | None:
    """Normalize a date-like value to SQLite-safe ISO YYYY-MM-DD.

    Handles the common source formats used in the app: ISO dates, day-first
    dd/mm/yyyy values, and date strings with a trailing time like hh:mm or
    hh:mm:ss (with or without AM/PM). A value that cannot be parsed is safely
    ignored by the date filter rather than causing a hard failure.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None

    # Strip time suffixes in either space-delimited or ISO-with-T forms.
    s = re.sub(r"[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?$", "", s)

    # Common date shapes seen in scraped exports and browser inputs.
    patterns = [
        (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", lambda y, mo, d: (_date(int(y), int(mo), int(d)).isoformat())),
        (r"^(\d{4})/(\d{1,2})/(\d{1,2})$", lambda y, mo, d: (_date(int(y), int(mo), int(d)).isoformat())),
        (r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", lambda d, mo, y: (_date(int(y), int(mo), int(d)).isoformat())),
        (r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", lambda y, mo, d: (_date(int(y), int(mo), int(d)).isoformat())),
    ]

    for pattern, parser in patterns:
        m = re.match(pattern, s)
        if not m:
            continue
        try:
            return parser(*m.groups())
        except ValueError:
            continue

    return None


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("norm_date", 1, normalize_date_to_iso)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _run_migrations()
    print(f"Database initialized at {DB_PATH}")


def _run_migrations() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    migrations = [
        ("commission_per_physicians", "physician_name", "TEXT NOT NULL DEFAULT ''"),
        ("matched_records", "user_flagged_mismatch", "INTEGER NOT NULL DEFAULT 0"),
        ("matched_records", "user_flag_reason", "TEXT DEFAULT NULL"),
        ("matched_records", "physician_name", "TEXT NOT NULL DEFAULT ''"),
        ("unmatched_records", "physician_name", "TEXT NOT NULL DEFAULT ''"),
    ]
    with get_conn() as conn:
        for table, column, decl in migrations:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN "{column}" {decl}')
                print(f"Migrated: added {table}.{column}")

        # Backfill physician_name for any pre-existing rows that
        # predate the column (joins against physicians by id).
        conn.execute(
            """UPDATE commission_per_physicians
               SET physician_name = (
                   SELECT physician_name FROM physicians
                   WHERE physicians.physician_id = commission_per_physicians.physician_id
               )
               WHERE physician_name = '' OR physician_name IS NULL"""
        )
        conn.execute(
            """UPDATE matched_records
               SET physician_name = (
                   SELECT physician_name FROM physicians
                   WHERE physicians.physician_id = matched_records.physician_id
               )
               WHERE physician_name = '' OR physician_name IS NULL"""
        )
        conn.execute(
            """UPDATE unmatched_records
               SET physician_name = (
                   SELECT physician_name FROM physicians
                   WHERE physicians.physician_id = unmatched_records.physician_id
               )
               WHERE (physician_name = '' OR physician_name IS NULL)
                 AND physician_id IS NOT NULL"""
        )

    _ensure_default_admin()


def seed_dictionary(dictionary_path: Path) -> int:
    data = json.loads(Path(dictionary_path).read_text(encoding="utf-8"))
    n = 0
    with get_conn() as conn:
        for service, category in data.items():
            cat = category if category in VALID_CATEGORIES else "Other"
            conn.execute(
                """INSERT INTO service_prices (service_type, category, cost)
                   VALUES (?, ?, 0)
                   ON CONFLICT(service_type) DO UPDATE SET category=excluded.category""",
                (service, cat),
            )
            n += 1
    print(f"Seeded/updated {n} services from {dictionary_path}")
    return n


# ── Password hashing (PBKDF2-HMAC-SHA256, stdlib only) ───────────

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk, expected)


# ── Users & role-based table access ──────────────────────────────

def _ensure_default_admin() -> None:
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if count:
        return
    password = secrets.token_urlsafe(9)
    create_user("admin", password, role="admin")
    print("=" * 64)
    print("No user accounts existed — created a default admin account:")
    print("  username: admin")
    print(f"  password: {password}")
    print("Log in and change this password immediately (Admin -> Users).")
    print("=" * 64)


def create_user(username: str, password: str, role: str = "user") -> int:
    if role not in ("admin", "user"):
        raise ValueError(f"Invalid role: {role!r}")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, pass_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, datetime.now(timezone.utc).isoformat()),
        )
        user_id = cur.lastrowid
        if role == "user":
            for table in DEFAULT_USER_TABLES:
                conn.execute(
                    "INSERT OR IGNORE INTO user_table_access (user_id, table_name) VALUES (?, ?)",
                    (user_id, table),
                )
    return user_id


def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, username, role, created_at FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def update_user(user_id: int, role: str | None = None, password: str | None = None) -> None:
    sets, params = [], []
    if role is not None:
        if role not in ("admin", "user"):
            raise ValueError(f"Invalid role: {role!r}")
        sets.append("role = ?")
        params.append(role)
    if password:
        sets.append("pass_hash = ?")
        params.append(hash_password(password))
    if not sets:
        return
    params.append(user_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", params)


def delete_user(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM user_table_access WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


def get_user_allowed_tables(user_id: int, role: str) -> list[str]:
    if role == "admin":
        return list(TABLES)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT table_name FROM user_table_access WHERE user_id = ?", (user_id,)
        ).fetchall()
    tables = [r["table_name"] for r in rows]
    return tables if tables else list(DEFAULT_USER_TABLES)


def set_user_table_access(user_id: int, tables: list[str]) -> None:
    valid = [t for t in tables if t in TABLES]
    with get_conn() as conn:
        conn.execute("DELETE FROM user_table_access WHERE user_id = ?", (user_id,))
        for table in valid:
            conn.execute(
                "INSERT INTO user_table_access (user_id, table_name) VALUES (?, ?)",
                (user_id, table),
            )


# ── Admin: commission rates & service categories ─────────────────

def list_commission_rates() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.physician_id, p.physician_name,
                      COALESCE(r.commission_percent, 0) AS commission_percent
               FROM physicians p
               LEFT JOIN physician_commission_rates r ON r.physician_id = p.physician_id
               ORDER BY p.physician_name"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_commission_rate(physician_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT commission_percent FROM physician_commission_rates WHERE physician_id = ?",
            (physician_id,),
        ).fetchone()
    return row["commission_percent"] if row else 0.0


def set_commission_rate(physician_id: int, percent: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO physician_commission_rates (physician_id, commission_percent, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(physician_id) DO UPDATE SET
                   commission_percent = excluded.commission_percent,
                   updated_at = excluded.updated_at""",
            (physician_id, percent, datetime.now(timezone.utc).isoformat()),
        )


def list_services() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT service_id, service_type, category, cost FROM service_prices "
            "ORDER BY category ASC, service_type ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_service_category(service_id: int, category: str) -> None:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category: {category!r}")
    with get_conn() as conn:
        conn.execute(
            "UPDATE service_prices SET category = ? WHERE service_id = ?", (category, service_id)
        )


def update_service_cost(service_id: int, cost: float) -> None:
    if cost < 0:
        raise ValueError("cost must be >= 0")
    with get_conn() as conn:
        conn.execute(
            "UPDATE service_prices SET cost = ? WHERE service_id = ?", (cost, service_id)
        )


def calculate_physician_commission(
    physician_id: int,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict | None:
    with get_conn() as conn:
        phys = conn.execute(
            "SELECT physician_id, physician_name FROM physicians WHERE physician_id = ?",
            (physician_id,),
        ).fetchone()
        if not phys:
            return None
        rate_row = conn.execute(
            "SELECT commission_percent FROM physician_commission_rates WHERE physician_id = ?",
            (physician_id,),
        ).fetchone()
        commission_percent = rate_row["commission_percent"] if rate_row else 0.0

        clauses = ["physician_id = ?"]
        params: list = [physician_id]
        if from_date:
            clauses.append("norm_date(payment_date) >= ?")
            params.append(normalize_date_to_iso(from_date) or from_date)
        if to_date:
            clauses.append("norm_date(payment_date) <= ?")
            params.append(normalize_date_to_iso(to_date) or to_date)

        where = " WHERE " + " AND ".join(clauses)

        agg = conn.execute(
            f"""SELECT
                    COALESCE(SUM(ultrasound),             0) AS ultrasound,
                    COALESCE(SUM(laboratory),             0) AS laboratory,
                    COALESCE(SUM("x-ray"),                0) AS xray,
                    COALESCE(SUM(nursing_and_procedures), 0) AS nursing_and_procedures,
                    COALESCE(SUM(consultation),           0) AS consultation,
                    COALESCE(SUM(other),                  0) AS other,
                    COALESCE(SUM(total),                  0) AS total_billed,
                    COALESCE(SUM(commision_amount),       0) AS commission_amount,
                    COUNT(DISTINCT patient_name)              AS patient_count,
                    COUNT(*)                                  AS row_count
               FROM commission_per_physicians{where}""",
            params,
        ).fetchone()

        # Batch breakdown: list of distinct batch_ids with their subtotals
        batches = conn.execute(
            f"""SELECT batch_id,
                       MIN(payment_date) AS period_start,
                       MAX(payment_date) AS period_end,
                       COUNT(DISTINCT patient_name) AS patients,
                       COALESCE(SUM(total), 0)             AS total_billed,
                       COALESCE(SUM(commision_amount), 0)  AS commission_amount
                FROM commission_per_physicians{where}
               GROUP BY batch_id
               ORDER BY period_start DESC""",
            params,
        ).fetchall()

    return {
        "physician": {
            "physician_id": phys["physician_id"],
            "physician_name": phys["physician_name"],
            "commission_percent": commission_percent,
        },
        "date_range": {"from_date": from_date, "to_date": to_date},
        "categories": {
            "Ultrasound":              round(agg["ultrasound"], 2),
            "Laboratory":              round(agg["laboratory"], 2),
            "X-ray":                   round(agg["xray"], 2),
            "Nursing & Procedures":    round(agg["nursing_and_procedures"], 2),
            "Consultation":            round(agg["consultation"], 2),
            "Other":                   round(agg["other"], 2),
        },
        "total_billed":      round(agg["total_billed"], 2),
        "commission_percent": commission_percent,
        "commission_amount": round(agg["commission_amount"], 2),
        "patient_count":     agg["patient_count"],
        "row_count":         agg["row_count"],
        "batches": [dict(b) for b in batches],
    }


# ── Reference helpers used across the pipeline & API ────────────

def get_or_create_physician(conn: sqlite3.Connection, name: str) -> int:
    name = (name or "Unknown").strip()
    row = conn.execute(
        "SELECT physician_id FROM physicians WHERE physician_name = ?", (name,)
    ).fetchone()
    if row:
        return row["physician_id"]
    cur = conn.execute("INSERT INTO physicians (physician_name) VALUES (?)", (name,))
    return cur.lastrowid


def get_or_create_service(conn: sqlite3.Connection, service_type: str, category: str | None = None) -> int:
    service_type = (service_type or "Unknown").strip()
    row = conn.execute(
        "SELECT service_id FROM service_prices WHERE service_type = ?", (service_type,)
    ).fetchone()
    if row:
        return row["service_id"]
    cat = category if category in VALID_CATEGORIES else "Other"
    cur = conn.execute(
        "INSERT INTO service_prices (service_type, category, cost) VALUES (?, ?, 0)",
        (service_type, cat),
    )
    return cur.lastrowid


TABLES = [
    "physicians", "service_prices", "abronal_mirror", "sot_mirror",
    "matched_records", "unmatched_records", "commission_per_physicians",
]

TABLE_DATE_COLUMNS = {
    "abronal_mirror": "payment_date",
    "sot_mirror": "transaction_date",
    "matched_records": "payment_date",
    "unmatched_records": "abronal_payment_date",
    "commission_per_physicians": "payment_date",
}

TABLE_PK_COLUMNS = {
    "abronal_mirror": "row_id",
    "sot_mirror": "row_id",
    "matched_records": "match_id",
    "unmatched_records": "unmatched_id",
    "commission_per_physicians": "id",
}


def _predicate_sql(predicate: dict[str, object]) -> tuple[list[str], list]:
    clauses, params = [], []
    for col, value in predicate.items():
        if value is None:
            clauses.append(f'"{col}" IS NULL')
        else:
            clauses.append(f'"{col}" = ?')
            params.append(value)
    return clauses, params


def row_exists(conn: sqlite3.Connection, table: str, predicate: dict[str, object]) -> bool:
    """Return True if a row already exists using the given equality predicates."""
    if not predicate:
        return False
    clauses, params = _predicate_sql(predicate)
    query = f'SELECT 1 FROM "{table}" WHERE {" AND ".join(clauses)} LIMIT 1'
    return conn.execute(query, params).fetchone() is not None


def find_row_id(conn: sqlite3.Connection, table: str, predicate: dict[str, object],
                id_column: str | None = None) -> int | None:
    """Return the primary key of an existing row matching predicate, or None."""
    if not predicate:
        return None
    pk = id_column or TABLE_PK_COLUMNS.get(table) or table_columns(table)[0]
    clauses, params = _predicate_sql(predicate)
    query = f'SELECT "{pk}" FROM "{table}" WHERE {" AND ".join(clauses)} LIMIT 1'
    row = conn.execute(query, params).fetchone()
    return row[pk] if row else None


def table_columns(table: str) -> list[str]:
    if table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    with get_conn() as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _build_where(table: str | None = None, filters: dict | None = None,
                  date_column: str | None = None,
                  start_date: str | None = None, end_date: str | None = None) -> tuple[str, list]:
    clauses, params = [], []
    if filters:
        for col, val in filters.items():
            if val in (None, "", "All"):
                continue
            # LIKE (not '=') so the same filter box works whether the
            # person picked an exact value from the dropdown or typed
            # a partial search term.
            clauses.append(f'"{col}" LIKE ? ESCAPE \'\\\'')
            escaped = str(val).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")

    resolved_date_column = date_column or (TABLE_DATE_COLUMNS.get(table) if table else None)
    if resolved_date_column and (start_date or end_date):
        # Use the table's actual payment date column rather than whichever
        # date-like column happens to be first in a result set. Compare
        # against normalized ISO dates so the filter works even when
        # cells are stored as dd/mm/yyyy, dd/mm/yyyy hh:mm, or ISO
        # strings; inclusive >=/<= continues to match every record in the
        # requested window, including gaps between dates.
        if start_date:
            norm_start = normalize_date_to_iso(start_date)
            clauses.append(f'norm_date("{resolved_date_column}") >= ?')
            params.append(norm_start or start_date)
        if end_date:
            norm_end = normalize_date_to_iso(end_date)
            clauses.append(f'norm_date("{resolved_date_column}") <= ?')
            params.append(norm_end or end_date)
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def table_default_order(table: str) -> str:
    """Default ordering for a table, preferring the natural date column
    when one exists. For commission_per_physicians, physician name is the
    leading grouping key, then the payment date within each physician.
    For matched/unmatched records, order by date to keep records chronological."""
    if table == "commission_per_physicians":
        return (
            'ORDER BY (norm_date("payment_date") IS NULL) ASC, '
            '"physician_name" COLLATE NOCASE ASC, '
            'norm_date("payment_date") ASC, '
            '"patient_name" COLLATE NOCASE ASC, "id" ASC'
        )
    if table == "matched_records":
        return (
            'ORDER BY (norm_date("payment_date") IS NULL) ASC, '
            'norm_date("payment_date") ASC, '
            '"physician_name" COLLATE NOCASE ASC, '
            '"patient_name" COLLATE NOCASE ASC, "match_id" ASC'
        )
    if table == "unmatched_records":
        return (
            'ORDER BY (norm_date("abronal_payment_date") IS NULL) ASC, '
            'norm_date("abronal_payment_date") ASC, '
            '"physician_name" COLLATE NOCASE ASC, '
            '"abronal_patient_name" COLLATE NOCASE ASC, "unmatched_id" ASC'
        )
    date_column = TABLE_DATE_COLUMNS.get(table)
    if date_column:
        pk = table_columns(table)[0]
        return (
            f'ORDER BY (norm_date("{date_column}") IS NULL) ASC, '
            f'norm_date("{date_column}") ASC, "{pk}" ASC'
        )
    pk = table_columns(table)[0]
    return f'ORDER BY "{pk}" ASC'


def fetch_table(table: str, filters: dict | None = None, limit: int = 1000, offset: int = 0,
                 date_column: str | None = None, start_date: str | None = None,
                 end_date: str | None = None) -> list[dict]:
    if table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    where, params = _build_where(table, filters, date_column, start_date, end_date)
    order_sql = table_default_order(table)
    query = f'SELECT * FROM {table}{where} {order_sql} LIMIT ? OFFSET ?'
    with get_conn() as conn:
        rows = conn.execute(query, params + [int(limit), int(offset)]).fetchall()
    return [dict(r) for r in rows]


def count_table(table: str, filters: dict | None = None, date_column: str | None = None,
                 start_date: str | None = None, end_date: str | None = None) -> int:
    if table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    where, params = _build_where(table, filters, date_column, start_date, end_date)
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}{where}", params).fetchone()
    return row["c"] if row else 0


def fetch_distinct(table: str, column: str, limit: int = 1000) -> list:
    if table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    if column not in table_columns(table):
        raise ValueError(f"Unknown column: {table}.{column}")
    with get_conn() as conn:
        rows = conn.execute(
            f'SELECT DISTINCT "{column}" AS v FROM {table} '
            f'WHERE "{column}" IS NOT NULL AND "{column}" != "" '
            f'ORDER BY "{column}" LIMIT ?',
            (limit,),
        ).fetchall()
    return [r["v"] for r in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage commissions.db")
    parser.add_argument("--init", action="store_true", help="(Re)create schema")
    parser.add_argument("--seed-dictionary", type=str, help="Path to dictionary.json")
    args = parser.parse_args()

    if args.init:
        init_db()
    if args.seed_dictionary:
        seed_dictionary(Path(args.seed_dictionary))
    if not args.init and not args.seed_dictionary:
        parser.print_help()