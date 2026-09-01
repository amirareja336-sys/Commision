#!/usr/bin/env python3
"""Generate test databases for reconciliation scenarios.

Usage:
  python generate_test_data.py --scenario 1.1
  python generate_test_data.py --scenario 1.1 --from-prod
  python generate_test_data.py --scenario 1.1 --from-prod --start-date 2026-08-01 --end-date 2026-08-15

Creates folders under `data/` named `test_1_1`, etc. with a `test.db`
and matching CSVs under `data/csv_datasets/test_<scenario>/`.

Also creates a `dev` user and prints the password for Test Mode login.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import secrets
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_SCHEMA_PATH = ROOT / "db" / "schema.sql"
PROD_DB_PATH = ROOT / "db" / "commissions.db"


def ensure_schema(conn: sqlite3.Connection) -> None:
    sql = DB_SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)


def _scenario_dates(scenario: str, base: date | None = None):
    base = base or date.today()
    if scenario == "1.1":
        ab_start = base - timedelta(days=10)
        ab_dates = [ab_start + timedelta(days=i) for i in range(3)]
        sot_start = ab_start - timedelta(days=5)
        sot_dates = [sot_start + timedelta(days=i) for i in range(15)]
    elif scenario == "1.1.1":
        sot_start = base - timedelta(days=20)
        sot_dates = [sot_start + timedelta(days=i) for i in range(31)]
        ab_dates = [sot_start + timedelta(days=i) for i in range(10, 20)]
    elif scenario == "1.2":
        ab_start = base - timedelta(days=20)
        ab_dates = [ab_start + timedelta(days=i) for i in range(31)]
        sot_start = ab_start + timedelta(days=8)
        sot_dates = [sot_start + timedelta(days=i) for i in range(6)]
    elif scenario == "1.2.1":
        ab_start = base - timedelta(days=30)
        ab_dates = [ab_start + timedelta(days=i) for i in range(41)]
        sot_dates = [ab_start + timedelta(days=i) for i in range(41)]
    else:
        raise SystemExit(f"Unknown scenario: {scenario}")
    return ab_dates, sot_dates


def insert_sample_rows(conn: sqlite3.Connection, scenario: str) -> None:
    conn.execute("INSERT INTO service_prices (service_type, category, cost) VALUES ('Test Service','Other',0)")
    conn.execute("INSERT INTO physicians (physician_name) VALUES ('Dr. Test')")
    service_id = conn.execute(
        "SELECT service_id FROM service_prices WHERE service_type = 'Test Service'"
    ).fetchone()[0]
    physician_id = conn.execute(
        "SELECT physician_id FROM physicians WHERE physician_name = 'Dr. Test'"
    ).fetchone()[0]

    ab_dates, sot_dates = _scenario_dates(scenario)

    for i, d in enumerate(ab_dates, start=1):
        conn.execute(
            """INSERT INTO abronal_mirror (row_number, card_number, patient_full_name, service_id, service_raw, total, net, commission_percent, commision_amount, payment_date, visit_date, status, physician_id, source_file, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                i, f"CARD{i:03}", f"Patient A {i}", service_id, "Test Service",
                100.0, 80.0, 10.0, 8.0, d.isoformat(), d.isoformat(), "paid",
                physician_id, "abr_test.xlsx", "batch-test",
            ),
        )

    for i, d in enumerate(sot_dates, start=1):
        conn.execute(
            """INSERT INTO sot_mirror (customer, tin_number, description, item_id, base_sku, quantity, unit_price, sub_total, tax_amount, withholding, fs_number, transaction_date, reference, MRC, service_id, source_file, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"Customer {i}", f"TIN{i:05}", "desc", f"item{i}", f"sku{i}",
                1, 100.0, 100.0, 0.0, "", i, d.isoformat(), f"ref{i}", "",
                service_id, "sot_test.xlsx", "batch-test",
            ),
        )

    _write_csv_dataset(scenario, service_id, physician_id, ab_dates, sot_dates)


def _write_csv_dataset(scenario: str, service_id, physician_id, ab_dates, sot_dates) -> None:
    csv_dir = ROOT / "data" / "csv_datasets" / f"test_{scenario}"
    csv_dir.mkdir(parents=True, exist_ok=True)

    ab_columns = [
        "row_number", "card_number", "patient_full_name", "patient_type",
        "service_id", "service_raw", "total", "net", "commission_percent",
        "commision_amount", "payment_date", "visit_date", "status",
        "physician_id", "source_file", "batch_id",
    ]
    with (csv_dir / "abronal_mirror.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ab_columns)
        writer.writeheader()
        for i, d in enumerate(ab_dates, start=1):
            writer.writerow({
                "row_number": i, "card_number": f"CARD{i:03}",
                "patient_full_name": f"Patient A {i}", "patient_type": "",
                "service_id": service_id, "service_raw": "Test Service",
                "total": 100.0, "net": 80.0, "commission_percent": 10.0,
                "commision_amount": 8.0, "payment_date": d.isoformat(),
                "visit_date": d.isoformat(), "status": "paid",
                "physician_id": physician_id, "source_file": "abr_test.xlsx",
                "batch_id": "batch-test",
            })

    sot_columns = [
        "customer", "tin_number", "description", "item_id", "base_sku",
        "quantity", "unit_price", "sub_total", "tax_amount", "withholding",
        "fs_number", "transaction_date", "reference", "MRC", "service_id",
        "source_file", "batch_id",
    ]
    with (csv_dir / "sot_mirror.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=sot_columns)
        writer.writeheader()
        for i, d in enumerate(sot_dates, start=1):
            writer.writerow({
                "customer": f"Customer {i}", "tin_number": f"TIN{i:05}",
                "description": "desc", "item_id": f"item{i}", "base_sku": f"sku{i}",
                "quantity": 1, "unit_price": 100.0, "sub_total": 100.0,
                "tax_amount": 0.0, "withholding": "", "fs_number": i,
                "transaction_date": d.isoformat(), "reference": f"ref{i}",
                "MRC": "", "service_id": service_id, "source_file": "sot_test.xlsx",
                "batch_id": "batch-test",
            })

    with (csv_dir / "service_prices.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["service_id", "service_type", "category", "cost"])
        writer.writeheader()
        writer.writerow({
            "service_id": service_id, "service_type": "Test Service",
            "category": "Other", "cost": 0,
        })

    with (csv_dir / "physicians.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["physician_id", "physician_name"])
        writer.writeheader()
        writer.writerow({"physician_id": physician_id, "physician_name": "Dr. Test"})


def _copy_ref_tables(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    for table in ("service_prices", "physicians"):
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        cols = [d[0] for d in src.execute(f"PRAGMA table_info({table})").fetchall()]
        # skip autoincrement PK conflicts by inserting without PK when possible
        insert_cols = [c for c in cols if c not in ("service_id", "physician_id") or True]
        placeholders = ",".join("?" for _ in insert_cols)
        col_sql = ",".join(insert_cols)
        for r in rows:
            dest.execute(
                f"INSERT OR IGNORE INTO {table} ({col_sql}) VALUES ({placeholders})",
                [r[c] for c in insert_cols],
            )


def insert_from_prod(
    dest: sqlite3.Connection,
    scenario: str,
    start_date: str | None,
    end_date: str | None,
) -> None:
    """Copy Abronal/SoT rows from production commissions.db into the test DB.

    Date window defaults to the synthetic scenario span when not provided.
    """
    if not PROD_DB_PATH.exists():
        raise SystemExit(f"Production DB not found at {PROD_DB_PATH}")

    ab_dates, sot_dates = _scenario_dates(scenario)
    if not start_date:
        start_date = min(ab_dates + sot_dates).isoformat()
    if not end_date:
        end_date = max(ab_dates + sot_dates).isoformat()

    src = sqlite3.connect(str(PROD_DB_PATH))
    src.row_factory = sqlite3.Row
    try:
        _copy_ref_tables(src, dest)

        abr_rows = src.execute(
            """
            SELECT * FROM abronal_mirror
            WHERE payment_date IS NOT NULL
              AND date(substr(replace(replace(payment_date,'/', '-'), ' ', 'T'), 1, 10))
                  BETWEEN date(?) AND date(?)
            LIMIT 500
            """,
            (start_date, end_date),
        ).fetchall()
        # Fallback: loose LIKE filter if date() fails on Abronal formats
        if not abr_rows:
            abr_rows = src.execute(
                "SELECT * FROM abronal_mirror ORDER BY row_id DESC LIMIT 200"
            ).fetchall()

        sot_rows = src.execute(
            """
            SELECT * FROM sot_mirror
            WHERE transaction_date IS NOT NULL
              AND date(substr(replace(replace(transaction_date,'/', '-'), ' ', 'T'), 1, 10))
                  BETWEEN date(?) AND date(?)
            LIMIT 500
            """,
            (start_date, end_date),
        ).fetchall()
        if not sot_rows:
            sot_rows = src.execute(
                "SELECT * FROM sot_mirror ORDER BY row_id DESC LIMIT 200"
            ).fetchall()

        abr_cols = [d[0] for d in src.execute("PRAGMA table_info(abronal_mirror)").fetchall()]
        sot_cols = [d[0] for d in src.execute("PRAGMA table_info(sot_mirror)").fetchall()]
        abr_insert = [c for c in abr_cols if c != "row_id"]
        sot_insert = [c for c in sot_cols if c != "row_id"]

        for r in abr_rows:
            dest.execute(
                f"INSERT INTO abronal_mirror ({','.join(abr_insert)}) VALUES ({','.join('?' for _ in abr_insert)})",
                [r[c] for c in abr_insert],
            )
        for r in sot_rows:
            dest.execute(
                f"INSERT INTO sot_mirror ({','.join(sot_insert)}) VALUES ({','.join('?' for _ in sot_insert)})",
                [r[c] for c in sot_insert],
            )

        # Export CSVs for Test Mode import
        csv_dir = ROOT / "data" / "csv_datasets" / f"test_{scenario}"
        csv_dir.mkdir(parents=True, exist_ok=True)
        for table, rows, cols in (
            ("abronal_mirror", abr_rows, abr_insert),
            ("sot_mirror", sot_rows, sot_insert),
        ):
            with (csv_dir / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=cols)
                writer.writeheader()
                for r in rows:
                    writer.writerow({c: r[c] for c in cols})

        print(f"Copied {len(abr_rows)} Abronal + {len(sot_rows)} SoT rows "
              f"from prod ({start_date} … {end_date})")
    finally:
        src.close()


def create_dev_user(conn: sqlite3.Connection) -> str:
    password = secrets.token_urlsafe(8)
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    pass_hash = f"{salt.hex()}${dk.hex()}"
    conn.execute(
        "INSERT INTO users (username, pass_hash, role, created_at) VALUES (?, ?, ?, datetime('now'))",
        ("dev", pass_hash, "admin"),
    )
    return password


def create_test_db(
    scenario: str,
    dest_dir: Path,
    *,
    from_prod: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    db_path = dest_dir / "test.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        if from_prod:
            insert_from_prod(conn, scenario, start_date, end_date)
        else:
            insert_sample_rows(conn, scenario)
        password = create_dev_user(conn)
        conn.commit()
        print("Created test user:")
        print("  username: dev")
        print(f"  password: {password}")
    finally:
        conn.close()
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, help="Scenario (1.1, 1.1.1, 1.2, 1.2.1)")
    parser.add_argument(
        "--from-prod",
        action="store_true",
        help="Select Abronal/SoT rows from db/commissions.db instead of synthetic data",
    )
    parser.add_argument("--start-date", default=None, help="Inclusive start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default=None, help="Inclusive end date (YYYY-MM-DD)")
    args = parser.parse_args()
    scenario_key = args.scenario.replace(".", "_")
    dest = DATA_DIR / f"test_{scenario_key}"
    print(f"Creating test DB for scenario {args.scenario} in {dest}")
    db = create_test_db(
        args.scenario,
        dest,
        from_prod=args.from_prod,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print(f"Created {db}")
    print("")
    print("To run the app against this test DB:")
    print(f"  set COMMISSIONS_DB={db}   # Windows")
    print(f"  COMMISSIONS_DB={db} ./start.sh --  # or")
    print(f"  COMMISSIONS_DB={db} uvicorn backend.main:app --host 0.0.0.0 --port 8000")


if __name__ == "__main__":
    main()
