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
DICTIONARY_PATH = DB_DIR.parent / "dictionary.json"

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

# Condensed buckets on commission_per_physicians (ECG / Echo / Supplies → Other).
CALCULATOR_CATEGORIES = [
    "Ultrasound",
    "Laboratory",
    "X-ray",
    "Nursing & Procedures",
    "Consultation",
    "Other",
]

PHYSICIAN_CATEGORY_RATE_TABLE = "physician_category_commision_rates"


def new_batch_id() -> str:
    return uuid.uuid4().hex[:12]


# ── Date normalization ────────────────────────────────────────────
# Abronal exports store payment dates as US month-first strings, often
# with a quirky time suffix like "08/19/2026 2:28:PM". SoT rows tend to
# be ISO ("2026-08-19 00:00:00"). SQLite's date() only understands ISO,
# so every range filter goes through parse_date() → YYYY-MM-DD.

_DATE_TIME_SUFFIX = re.compile(
    r"[T\s]+\d{1,2}:\d{2}"
    r"(?::\d{2})?"          # optional seconds
    r"(?::?\s*[AaPp][Mm])?"  # "PM", " PM", or Abronal's ":PM"
    r"$"
)

# Ambiguous slash/dash/dot dates (mm/dd/yyyy) — Abronal's native shape.
_REGEX_A_MDY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")
# ISO-ish year-first dates (yyyy-mm-dd / yyyy/mm/dd).
_REGEX_B_YMD = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")
# Day-first fallback when month-first is impossible (e.g. 20/08/2026).
_REGEX_C_DMY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")


def parse_date(value) -> str | None:
    """Parse any common date string into comparable ISO YYYY-MM-DD.

    Standard interpretation for ambiguous numeric dates is mm/dd/yyyy
    (Abronal / US). Trailing times are stripped first. Returns None when
    the value cannot be parsed (those rows are excluded from range
    filters rather than raising).
    """
    if value is None:
        return None
    if isinstance(value, _date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()

    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None

    s = _DATE_TIME_SUFFIX.sub("", s).strip()

    # regex_b — year-first (ISO / SoT / <input type=date>)
    m = _REGEX_B_YMD.match(s)
    if m:
        y, mo, d = m.groups()
        try:
            return _date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            pass

    # regex_a — month-first mm/dd/yyyy (Abronal standard)
    m = _REGEX_A_MDY.match(s)
    if m:
        mo, d, y = m.groups()
        try:
            return _date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            # regex_c — day-first only when month-first is impossible
            try:
                return _date(int(y), int(d), int(mo)).isoformat()
            except ValueError:
                pass

    return None


# Back-compat alias used across the pipeline / review adapter.
normalize_date_to_iso = parse_date


def list_date_columns(table: str) -> list[str]:
    """Return columns on `table` whose names look like dates."""
    return [
        c for c in table_columns(table)
        if re.search(r"date|time|_at$", c, flags=re.I)
    ]


def resolve_date_column(table: str, preferred: str | None = None) -> str | None:
    """Pick the column used for date-range filters.

    If an explicit preferred column is valid, use it. When a table has
    more than one date-like column, prefer payment_date (or a mapped
    payment alias). Otherwise use the sole available date column / the
    TABLE_DATE_COLUMNS default.
    """
    if table not in TABLES and table != "reports":
        return preferred
    cols = set(table_columns(table)) if table in TABLES or table == "reports" else set()
    if preferred and preferred in cols:
        return preferred

    mapped = TABLE_DATE_COLUMNS.get(table)
    if mapped and mapped in cols:
        return mapped

    date_cols = list_date_columns(table) if cols else []
    if not date_cols:
        return mapped

    if len(date_cols) > 1:
        for name in ("payment_date", "abronal_payment_date", "transaction_date", "sot_payment_date"):
            if name in date_cols:
                return name
    return date_cols[0]


def filter_date(table: str, start: str | None, end: str | None,
                date_column: str | None = None) -> tuple[str, list]:
    """Build a WHERE fragment for an inclusive start–end date range.

    Dates are parsed to ISO via parse_date(); the resolved payment/date
    column is compared with norm_date() so mixed stored formats still match.
    """
    if not (start or end):
        return "", []
    column = resolve_date_column(table, date_column)
    if not column:
        return "", []

    clauses, params = [], []
    if start:
        norm_start = parse_date(start)
        clauses.append(f'norm_date("{column}") >= ?')
        params.append(norm_start or start)
    if end:
        norm_end = parse_date(end)
        clauses.append(f'norm_date("{column}") <= ?')
        params.append(norm_end or end)
    return " AND ".join(clauses), params


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("norm_date", 1, parse_date)
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

        _migrate_category_commission_rates(conn)

    _ensure_default_admin()


def _migrate_category_commission_rates(conn: sqlite3.Connection) -> None:
    """Copy legacy one-rate-per-physician rows onto every calculator category."""
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if PHYSICIAN_CATEGORY_RATE_TABLE not in tables:
        return
    if "physician_commission_rates" not in tables:
        return
    now = datetime.now(timezone.utc).isoformat()
    old = conn.execute(
        "SELECT physician_id, commission_percent FROM physician_commission_rates"
    ).fetchall()
    for row in old:
        for cat in CALCULATOR_CATEGORIES:
            conn.execute(
                f"""INSERT OR IGNORE INTO {PHYSICIAN_CATEGORY_RATE_TABLE}
                    (physician_id, category, commission_percent, updated_at)
                    VALUES (?, ?, ?, ?)""",
                (row["physician_id"], cat, row["commission_percent"] or 0, now),
            )


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


def load_dictionary(dictionary_path: Path | None = None) -> dict[str, str]:
    path = Path(dictionary_path) if dictionary_path else DICTIONARY_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_dictionary_entries(
    assignments: dict[str, str],
    dictionary_path: Path | None = None,
) -> dict:
    """Merge service→category into dictionary.json and upsert service_prices.

    Returns counts of written entries. Invalid categories are rejected.
    """
    path = Path(dictionary_path) if dictionary_path else DICTIONARY_PATH
    cleaned: dict[str, str] = {}
    for service, category in (assignments or {}).items():
        name = (service or "").strip()
        cat = (category or "").strip()
        if not name:
            continue
        if cat not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category for {name!r}: {cat!r}")
        cleaned[name] = cat
    if not cleaned:
        raise ValueError("No service assignments provided")

    current = load_dictionary(path)
    current.update(cleaned)
    # Stable key order for readable diffs.
    ordered = {k: current[k] for k in sorted(current.keys(), key=lambda s: s.lower())}
    path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    with get_conn() as conn:
        for service, cat in cleaned.items():
            conn.execute(
                """INSERT INTO service_prices (service_type, category, cost)
                   VALUES (?, ?, 0)
                   ON CONFLICT(service_type) DO UPDATE SET category=excluded.category""",
                (service, cat),
            )
    return {"saved": len(cleaned), "dictionary_size": len(ordered), "path": str(path)}


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
            """SELECT physician_id, physician_name
               FROM physicians
               ORDER BY physician_name"""
        ).fetchall()
    out = []
    for r in rows:
        rates = get_commission_rates_for_physician(r["physician_id"])
        vals = list(rates.values())
        uniform = vals[0] if vals and len(set(vals)) == 1 else 0.0
        out.append({
            "physician_id": r["physician_id"],
            "physician_name": r["physician_name"],
            "rates": rates,
            "commission_percent": uniform,
        })
    return out


def get_commission_rates_for_physician(physician_id: int) -> dict[str, float]:
    """One commission percent per condensed category for this physician."""
    rates = {cat: 0.0 for cat in CALCULATOR_CATEGORIES}
    with get_conn() as conn:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if PHYSICIAN_CATEGORY_RATE_TABLE not in tables:
            return rates
        rows = conn.execute(
            f"""SELECT category, commission_percent
                FROM {PHYSICIAN_CATEGORY_RATE_TABLE}
                WHERE physician_id = ?""",
            (physician_id,),
        ).fetchall()
    for row in rows:
        cat = row["category"]
        if cat in rates:
            rates[cat] = float(row["commission_percent"] or 0)
    return rates


def get_commission_rate(physician_id: int) -> float:
    """Legacy flat rate (mean of non-zero category rates). Prefer per-category lookup."""
    rates = get_commission_rates_for_physician(physician_id)
    vals = [v for v in rates.values() if v]
    if vals:
        return float(sum(vals) / len(vals))
    with get_conn() as conn:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "physician_commission_rates" not in tables:
            return 0.0
        row = conn.execute(
            "SELECT commission_percent FROM physician_commission_rates WHERE physician_id = ?",
            (physician_id,),
        ).fetchone()
    return float(row["commission_percent"]) if row else 0.0


def set_commission_rate(physician_id: int, percent: float, category: str | None = None) -> None:
    """Write a per-category rate. If category is omitted, apply to every bucket."""
    cats = [category] if category else list(CALCULATOR_CATEGORIES)
    if category and category not in CALCULATOR_CATEGORIES:
        raise ValueError(f"Invalid category: {category!r}")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        for cat in cats:
            conn.execute(
                f"""INSERT INTO {PHYSICIAN_CATEGORY_RATE_TABLE}
                    (physician_id, category, commission_percent, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(physician_id, category) DO UPDATE SET
                        commission_percent = excluded.commission_percent,
                        updated_at = excluded.updated_at""",
                (physician_id, cat, percent, now),
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


def billed_by_category(row) -> dict[str, float]:
    return {
        "Ultrasound": round(float(row["ultrasound"] or 0), 2),
        "Laboratory": round(float(row["laboratory"] or 0), 2),
        "X-ray": round(float(row["xray"] or 0), 2),
        "Nursing & Procedures": round(float(row["nursing_and_procedures"] or 0), 2),
        "Consultation": round(float(row["consultation"] or 0), 2),
        "Other": round(float(row["other"] or 0), 2),
    }


def category_commission_breakdown(
    billed: dict[str, float],
    rates: dict[str, float],
) -> tuple[dict[str, dict], float]:
    """Apply each category's own rate: billed * rate / 100. Missing rate → 0."""
    categories: dict[str, dict] = {}
    total_commission = 0.0
    for cat in CALCULATOR_CATEGORIES:
        amount = round(float(billed.get(cat, 0) or 0), 2)
        rate = float(rates.get(cat, 0) or 0)
        commission = round(amount * rate / 100.0, 2)
        categories[cat] = {"billed": amount, "rate": rate, "commission": commission}
        total_commission += commission
    return categories, round(total_commission, 2)


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

        clauses = ["physician_id = ?"]
        params: list = [physician_id]
        if from_date:
            clauses.append("norm_date(payment_date) >= ?")
            params.append(normalize_date_to_iso(from_date) or from_date)
        if to_date:
            clauses.append("norm_date(payment_date) <= ?")
            params.append(normalize_date_to_iso(to_date) or to_date)

        where = " WHERE " + " AND ".join(clauses)
        cat_sums = """
                    COALESCE(SUM(ultrasound),             0) AS ultrasound,
                    COALESCE(SUM(laboratory),             0) AS laboratory,
                    COALESCE(SUM("x-ray"),                0) AS xray,
                    COALESCE(SUM(nursing_and_procedures), 0) AS nursing_and_procedures,
                    COALESCE(SUM(consultation),           0) AS consultation,
                    COALESCE(SUM(other),                  0) AS other,
                    COALESCE(SUM(total),                  0) AS total_billed
        """

        agg = conn.execute(
            f"""SELECT {cat_sums},
                    COUNT(DISTINCT patient_name) AS patient_count,
                    COUNT(*) AS row_count
               FROM commission_per_physicians{where}""",
            params,
        ).fetchone()

        batches = conn.execute(
            f"""SELECT batch_id,
                       MIN(payment_date) AS period_start,
                       MAX(payment_date) AS period_end,
                       COUNT(DISTINCT patient_name) AS patients,
                       {cat_sums}
                FROM commission_per_physicians{where}
               GROUP BY batch_id
               ORDER BY period_start DESC""",
            params,
        ).fetchall()

    rates = get_commission_rates_for_physician(physician_id)
    billed = billed_by_category(agg)
    categories, commission_amount = category_commission_breakdown(billed, rates)
    total_billed = round(float(agg["total_billed"] or 0), 2)

    batch_rows = []
    for b in batches:
        _b_cats, b_comm = category_commission_breakdown(billed_by_category(b), rates)
        batch_rows.append({
            "batch_id": b["batch_id"],
            "period_start": b["period_start"],
            "period_end": b["period_end"],
            "patients": b["patients"],
            "total_billed": round(float(b["total_billed"] or 0), 2),
            "commission_amount": b_comm,
            "categories": _b_cats,
        })

    return {
        "physician": {
            "physician_id": phys["physician_id"],
            "physician_name": phys["physician_name"],
            "rates": rates,
        },
        "date_range": {"from_date": from_date, "to_date": to_date},
        "categories": categories,
        "total_billed": total_billed,
        "commission_amount": commission_amount,
        "patient_count": agg["patient_count"],
        "row_count": agg["row_count"],
        "batches": batch_rows,
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
    cat = category if category in VALID_CATEGORIES else None
    if cat is None:
        cat = load_dictionary().get(service_type)
    if cat not in VALID_CATEGORIES:
        cat = "Other"
    cur = conn.execute(
        "INSERT INTO service_prices (service_type, category, cost) VALUES (?, ?, 0)",
        (service_type, cat),
    )
    return cur.lastrowid


TABLES = [
    "physicians", "service_prices", "abronal_mirror", "sot_mirror",
    "matched_records", "unmatched_records", "commission_per_physicians",
]

# Admin spreadsheet editor — work DB only (never the backup).
EDITABLE_TABLES = TABLES + ["physician_category_commision_rates"]

TABLE_DATE_COLUMNS = {
    "abronal_mirror": "payment_date",
    "sot_mirror": "transaction_date",
    "matched_records": "payment_date",
    "unmatched_records": "abronal_payment_date",
    "commission_per_physicians": "payment_date",
}

TABLE_PK_COLUMNS = {
    "physicians": "physician_id",
    "service_prices": "service_id",
    "physician_category_commision_rates": "physician_id",
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
    if table == "reports":
        with get_conn() as conn:
            rows = conn.execute("PRAGMA table_info(reports)").fetchall()
        return [r["name"] for r in rows]
    if table not in EDITABLE_TABLES and table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    with get_conn() as conn:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r["name"] for r in rows]


def pk_column(table: str) -> str:
    if table not in EDITABLE_TABLES:
        raise ValueError(f"Table is not editable: {table}")
    return TABLE_PK_COLUMNS.get(table) or table_columns(table)[0]


def fetch_editable_table(table: str, limit: int = 200, offset: int = 0) -> list[dict]:
    if table not in EDITABLE_TABLES:
        raise ValueError(f"Table is not editable: {table}")
    if table in TABLES:
        return fetch_table(table, limit=limit, offset=offset)
    pk = pk_column(table)
    with get_conn() as conn:
        rows = conn.execute(
            f'SELECT * FROM "{table}" ORDER BY "{pk}" ASC LIMIT ? OFFSET ?',
            (int(limit), int(offset)),
        ).fetchall()
    return [dict(r) for r in rows]


def count_editable_table(table: str) -> int:
    if table not in EDITABLE_TABLES:
        raise ValueError(f"Table is not editable: {table}")
    if table in TABLES:
        return count_table(table)
    with get_conn() as conn:
        row = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()
    return int(row["c"]) if row else 0


def update_table_row(table: str, pk_value, values: dict) -> dict:
    """Update a work-DB row. Primary key column cannot be changed."""
    if table not in EDITABLE_TABLES:
        raise ValueError(f"Table is not editable: {table}")
    pk = pk_column(table)
    cols = table_columns(table)
    payload = {k: v for k, v in values.items() if k in cols and k != pk}
    if not payload:
        raise ValueError("No updatable columns provided")
    sets = ", ".join(f'"{c}" = ?' for c in payload)
    params = list(payload.values()) + [pk_value]
    with get_conn() as conn:
        cur = conn.execute(
            f'UPDATE "{table}" SET {sets} WHERE "{pk}" = ?',
            params,
        )
        if cur.rowcount == 0:
            raise KeyError(f"No row with {pk}={pk_value}")
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE "{pk}" = ?', (pk_value,)
        ).fetchone()
    return dict(row)


def insert_table_row(table: str, values: dict) -> dict:
    if table not in EDITABLE_TABLES:
        raise ValueError(f"Table is not editable: {table}")
    pk = pk_column(table)
    cols = [c for c in table_columns(table) if c != pk or values.get(c) not in (None, "")]
    # Prefer letting AUTOINCREMENT assign PK when omitted.
    if pk in cols and (values.get(pk) is None or values.get(pk) == ""):
        cols = [c for c in cols if c != pk]
    if not cols:
        raise ValueError("No columns to insert")
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(f'"{c}"' for c in cols)
    params = [values.get(c) for c in cols]
    with get_conn() as conn:
        cur = conn.execute(
            f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})',
            params,
        )
        new_id = values.get(pk) if pk in cols else cur.lastrowid
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE "{pk}" = ?', (new_id,)
        ).fetchone()
    return dict(row)


def delete_table_row(table: str, pk_value) -> None:
    if table not in EDITABLE_TABLES:
        raise ValueError(f"Table is not editable: {table}")
    pk = pk_column(table)
    with get_conn() as conn:
        cur = conn.execute(f'DELETE FROM "{table}" WHERE "{pk}" = ?', (pk_value,))
        if cur.rowcount == 0:
            raise KeyError(f"No row with {pk}={pk_value}")


def _date_expr_for_table(table: str, date_column: str | None = None) -> str | None:
    """SQL expression that yields a comparable date for the table."""
    col = resolve_date_column(table, date_column)
    if not col:
        return None
    # unmatched rows may only carry a SoT date.
    if table == "unmatched_records":
        return (
            'norm_date(COALESCE(NULLIF("abronal_payment_date", \'\'), '
            'NULLIF("sot_payment_date", \'\')))'
        )
    return f'norm_date("{col}")'


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

    if table and (start_date or end_date):
        expr = _date_expr_for_table(table, date_column)
        if expr:
            if start_date:
                clauses.append(f"{expr} >= ?")
                params.append(parse_date(start_date) or start_date)
            if end_date:
                clauses.append(f"{expr} <= ?")
                params.append(parse_date(end_date) or end_date)

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


# ── Accountant report snapshots ───────────────────────────────────

def insert_report_snapshot(
    user: dict,
    rows: list[dict],
    *,
    physician_filter: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Insert the currently filtered condensed table, ordered by payment date."""
    submission_id = new_batch_id()
    submitted_at = datetime.now(timezone.utc).isoformat()
    submitted_by = user.get("user_id")
    submitted_by_name = user.get("username") or ""

    def sort_key(row: dict) -> str:
        return normalize_date_to_iso(row.get("payment_date")) or "9999-99-99"

    ordered = sorted(rows, key=sort_key)
    with get_conn() as conn:
        for row in ordered:
            conn.execute(
                """INSERT INTO reports
                   (submission_id, submitted_by, submitted_by_name, submitted_at,
                    match_id, physician_id, physician_name, patient_name, service_id,
                    total_amount, net_amount, payment_date, match_type, confidence,
                    user_flagged_mismatch, user_flag_reason,
                    filter_physician, filter_start_date, filter_end_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    submission_id, submitted_by, submitted_by_name, submitted_at,
                    row.get("match_id"), row.get("physician_id"),
                    row.get("physician_name") or "", row.get("patient_name") or "",
                    row.get("service_id"),
                    float(row.get("total_amount") or 0), float(row.get("net_amount") or 0),
                    row.get("payment_date"), row.get("match_type"), row.get("confidence"),
                    1 if row.get("user_flagged_mismatch") in (1, True, "1") else 0,
                    row.get("user_flag_reason"),
                    physician_filter or None, start_date or None, end_date or None,
                ),
            )
    return {
        "submission_id": submission_id,
        "row_count": len(ordered),
        "submitted_at": submitted_at,
    }


def fetch_reports(
    physician_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    submission_id: str | None = None,
    limit: int = 5000,
    offset: int = 0,
) -> list[dict]:
    filters: dict = {}
    if physician_name:
        filters["physician_name"] = physician_name
    if submission_id:
        filters["submission_id"] = submission_id
    where, params = _build_where("reports", filters or None, "payment_date", start_date, end_date)
    order_sql = (
        'ORDER BY (norm_date("payment_date") IS NULL) ASC, '
        'norm_date("payment_date") ASC, '
        '"physician_name" COLLATE NOCASE ASC, '
        '"patient_name" COLLATE NOCASE ASC, "report_row_id" ASC'
    )
    query = f"SELECT * FROM reports{where} {order_sql} LIMIT ? OFFSET ?"
    with get_conn() as conn:
        rows = conn.execute(query, params + [int(limit), int(offset)]).fetchall()
    return [dict(r) for r in rows]


def count_reports(
    physician_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    submission_id: str | None = None,
) -> int:
    filters: dict = {}
    if physician_name:
        filters["physician_name"] = physician_name
    if submission_id:
        filters["submission_id"] = submission_id
    where, params = _build_where("reports", filters or None, "payment_date", start_date, end_date)
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM reports{where}", params).fetchone()
    return row["c"] if row else 0


def list_report_submissions(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT submission_id, submitted_by_name, MIN(submitted_at) AS submitted_at,
                      COUNT(*) AS row_count,
                      MIN(filter_physician) AS filter_physician,
                      MIN(filter_start_date) AS filter_start_date,
                      MIN(filter_end_date) AS filter_end_date
               FROM reports
               GROUP BY submission_id
               ORDER BY submitted_at DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def distinct_report_physicians(limit: int = 1000) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT physician_name AS v FROM reports
               WHERE physician_name IS NOT NULL AND physician_name != ''
               ORDER BY physician_name COLLATE NOCASE LIMIT ?""",
            (int(limit),),
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