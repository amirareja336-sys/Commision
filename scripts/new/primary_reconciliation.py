from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db_manager as dbm  # noqa: E402
import column_adapter as ca  # noqa: E402


def normalize_string(s) -> str:
    if not isinstance(s, str):
        return ""
    s = s.upper()
    s = re.sub(r"[^A-Z0-9\s]", "", s)
    return " ".join(s.split())


_MONTHS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
}


def _is_date_token(tok: str) -> bool:
    t = tok.strip().lower().rstrip(",.")
    if not t:
        return False
    if t in _MONTHS:
        return True
    if re.fullmatch(r"\d{1,2}(-\d{1,2})?", t):          # "9", "1-9"
        return True
    if re.fullmatch(r"\d{4}", t):                        # "2026"
        return True
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?", t):  # "7/1", "07-01-2026"
        return True
    return False


def physician_from_filename(filename: str) -> str:
    base = os.path.splitext(os.path.basename(filename))[0]
    tokens = re.split(r"\s+", base.strip())

    dr_idx = next((i for i, t in enumerate(tokens) if re.fullmatch(r"dr\.?", t, flags=re.I)), None)

    if dr_idx is not None:
        name_tokens = [tokens[dr_idx]]
        for t in tokens[dr_idx + 1:]:
            if _is_date_token(t):
                break
            name_tokens.append(t)
    else:
        # No explicit "Dr" token found — fall back to everything
        # before the first date-like token.
        name_tokens = []
        for t in tokens:
            if _is_date_token(t):
                break
            name_tokens.append(t)
        if not name_tokens:
            name_tokens = tokens

    first = name_tokens[0]
    rest = name_tokens[1:] if re.fullmatch(r"dr\.?", first, flags=re.I) else name_tokens
    rest = [w for w in rest if w]
    if not rest:
        return base.strip() or "Unknown Physician"
    first_name = rest[0].capitalize()
    surname = rest[-1].capitalize()
    name = first_name if first_name == surname and len(rest) == 1 else f"{first_name} {surname}"
    return re.sub(r"\s+", " ", name).strip()


SUPPORTED_EXTENSIONS = (".xlsx", ".xls", ".csv", ".tsv")


def parse_abronal_dir(abr_dir: str, batch_id: str, log):
    rows = []
    for filename in sorted(os.listdir(abr_dir)):
        if not filename.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        physician_name = physician_from_filename(filename)
        log(f"Parsing Abronal file: {filename} -> physician = {physician_name}")
        path = os.path.join(abr_dir, filename)
        try:
            df = ca.adapt_sheet(path, ca.ABRONAL_SCHEMA, log=log)
        except ca.ColumnAdapterError as e:
            log(f"  SKIPPING {filename}: {e}")
            continue

        for _, row in df.iterrows():
            name = ca.get_str(row, "patient_full_name")
            if not name:
                continue
            total = ca.get_float(row, "total")
            net = ca.get_float(row, "net", default=total)
            rows.append({
                "row_number": ca.get_int(row, "row_number", default=len(rows) + 1),
                "card_number": ca.get_str(row, "card_number") or None,
                "patient_full_name": name,
                "patient_type": ca.get_str(row, "patient_type") or None,
                "service_raw": ca.get_str(row, "service_raw"),
                "total": total,
                "net": net,
                "commission_percent": ca.get_float(row, "commission_percent", default=None),
                "commision_amount": ca.get_float(row, "commision_amount", default=None),
                "payment_date": ca.get_str(row, "payment_date") or None,
                "visit_date": ca.get_str(row, "visit_date") or None,
                "status": ca.get_str(row, "status") or None,
                "physician_name": physician_name,
                "source_file": filename,
            })
    log(f"Parsed {len(rows)} Abronal rows from {abr_dir}")
    return rows


def parse_sot_dir(sot_dir: str, batch_id: str, log):
    rows = []
    for filename in sorted(os.listdir(sot_dir)):
        if not filename.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        log(f"Parsing SoT file: {filename}")
        path = os.path.join(sot_dir, filename)
        try:
            df = ca.adapt_sheet(path, ca.SOT_SCHEMA, log=log)
        except ca.ColumnAdapterError as e:
            log(f"  SKIPPING {filename}: {e}")
            continue

        for _, row in df.iterrows():
            customer = ca.get_str(row, "customer")
            if not customer:
                continue
            sub_total = ca.get_float(row, "sub_total")
            rows.append({
                "customer": customer,
                "tin_number": ca.get_str(row, "tin_number") or None,
                "description": ca.get_str(row, "description"),
                "item_id": ca.get_str(row, "item_id") or None,
                "base_sku": ca.get_str(row, "base_sku"),
                "quantity": ca.get_int(row, "quantity", default=1),
                "unit_price": ca.get_float(row, "unit_price", default=sub_total),
                "sub_total": sub_total,
                "tax_amount": ca.get_float(row, "tax_amount"),
                "withholding": ca.get_str(row, "withholding") or None,
                "fs_number": ca.get_int(row, "fs_number"),
                "transaction_date": ca.get_str(row, "transaction_date") or None,
                "reference": ca.get_str(row, "reference"),
                "MRC": ca.get_str(row, "mrc"),
                "source_file": filename,
            })
    log(f"Parsed {len(rows)} SoT rows from {sot_dir}")
    return rows


def persist_mirrors(abr_rows, sot_rows, batch_id, log):
    # Sort rows by payment/transaction date before inserting for chronological order
    def abr_sort_key(r):
        return dbm.normalize_date_to_iso(r.get("payment_date")) or "9999-99-99"

    def sot_sort_key(r):
        return dbm.normalize_date_to_iso(r.get("transaction_date")) or "9999-99-99"

    abr_rows_sorted = sorted(abr_rows, key=abr_sort_key)
    sot_rows_sorted = sorted(sot_rows, key=sot_sort_key)

    abr_ids, sot_ids = [], []
    with dbm.get_conn() as conn:
        for r in abr_rows_sorted:
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
            if dbm.row_exists(conn, "abronal_mirror", row_key):
                existing_id = dbm.find_row_id(conn, "abronal_mirror", row_key)
                if existing_id is not None:
                    r["row_id"] = existing_id
                    r["physician_id"] = physician_id
                    r["service_id"] = service_id
                continue
            cur = conn.execute(
                """INSERT INTO abronal_mirror
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
            abr_ids.append(cur.lastrowid)

        for r in sot_rows_sorted:
            service_id = dbm.get_or_create_service(conn, r["description"])
            row_key = {
                "customer": r["customer"],
                "description": r["description"],
                "sub_total": r["sub_total"],
                "transaction_date": r["transaction_date"],
                "source_file": r["source_file"],
            }
            if dbm.row_exists(conn, "sot_mirror", row_key):
                existing_id = dbm.find_row_id(conn, "sot_mirror", row_key)
                if existing_id is not None:
                    r["row_id"] = existing_id
                    r["service_id"] = service_id
                continue
            cur = conn.execute(
                """INSERT INTO sot_mirror
                   (customer, tin_number, description, item_id, base_sku, quantity, unit_price,
                    sub_total, tax_amount, withholding, fs_number, transaction_date, reference,
                    MRC, service_id, source_file, batch_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["customer"], r["tin_number"], r["description"], r["item_id"], r["base_sku"],
                 r["quantity"], r["unit_price"], r["sub_total"], r["tax_amount"], r["withholding"],
                 r["fs_number"], r["transaction_date"], r["reference"], r["MRC"], service_id,
                 r["source_file"], batch_id),
            )
            r["row_id"] = cur.lastrowid
            r["service_id"] = service_id
            sot_ids.append(cur.lastrowid)
    log(f"Persisted {len(abr_ids)} Abronal rows and {len(sot_ids)} SoT rows to the database.")
    return abr_rows, sot_rows


AMOUNT_TOLERANCE = 0.01


def _load_already_matched_ids(conn):
    already_matched_abr = {
        r[0] for r in conn.execute(
            "SELECT abronal_row_id FROM matched_records WHERE abronal_row_id IS NOT NULL"
        )
    }
    already_matched_sot = {
        r[0] for r in conn.execute(
            "SELECT sot_row_id FROM matched_records WHERE sot_row_id IS NOT NULL"
        )
    }
    return already_matched_abr, already_matched_sot


def _prior_unmatched_to_abr_rows(conn):
    """Convert Abronal-side unmatched_records into reconciliation pool rows."""
    rows = []
    for u in conn.execute(
        """SELECT * FROM unmatched_records
           WHERE abronal_row_id IS NOT NULL AND sot_row_id IS NULL"""
    ).fetchall():
        u = dict(u)
        aid = u["abronal_row_id"]
        mirror = conn.execute(
            """SELECT service_id, total, net, payment_date, physician_id
               FROM abronal_mirror WHERE row_id = ?""",
            (aid,),
        ).fetchone()
        service_id = (mirror["service_id"] if mirror else None) or dbm.get_or_create_service(
            conn, u.get("abronal_service_type") or "Unknown"
        )
        rows.append({
            "row_id": aid,
            "patient_full_name": u["abronal_patient_name"],
            "service_raw": u["abronal_service_type"],
            "service_id": service_id,
            "total": float(mirror["total"]) if mirror and mirror["total"] is not None else float(u["abronal_net_amount"] or 0),
            "net": float(mirror["net"]) if mirror and mirror["net"] is not None else float(u["abronal_net_amount"] or 0),
            "payment_date": (mirror["payment_date"] if mirror else None) or u.get("abronal_payment_date"),
            "physician_id": (mirror["physician_id"] if mirror else None) or u.get("physician_id"),
            "physician_name": u.get("physician_name") or "",
            "_from_prior_unmatched": True,
            "_unmatched_id": u["unmatched_id"],
        })
    return rows


def _prior_unmatched_to_sot_rows(conn):
    """Convert SoT-side unmatched_records into reconciliation pool rows."""
    rows = []
    for u in conn.execute(
        """SELECT * FROM unmatched_records
           WHERE sot_row_id IS NOT NULL AND abronal_row_id IS NULL"""
    ).fetchall():
        u = dict(u)
        sid = u["sot_row_id"]
        mirror = conn.execute(
            """SELECT service_id, sub_total, transaction_date
               FROM sot_mirror WHERE row_id = ?""",
            (sid,),
        ).fetchone()
        service_id = (mirror["service_id"] if mirror else None) or dbm.get_or_create_service(
            conn, u.get("sot_service_type") or "Unknown"
        )
        rows.append({
            "row_id": sid,
            "customer": u["sot_patient_name"],
            "description": u["sot_service_type"],
            "service_id": service_id,
            "sub_total": float(mirror["sub_total"]) if mirror and mirror["sub_total"] is not None else float(u["sot_amount"] or 0),
            "transaction_date": (mirror["transaction_date"] if mirror else None) or u.get("sot_payment_date"),
            "_from_prior_unmatched": True,
            "_unmatched_id": u["unmatched_id"],
        })
    return rows


def _dedupe_pool_by_row_id(rows):
    """Keep one entry per mirror row_id; prefer freshly uploaded rows over prior unmatched."""
    by_id: dict[int, dict] = {}
    for r in rows:
        rid = r.get("row_id")
        if not rid:
            continue
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = r
        elif existing.get("_from_prior_unmatched") and not r.get("_from_prior_unmatched"):
            prior_id = existing.get("_unmatched_id")
            by_id[rid] = r
            if prior_id is not None:
                by_id[rid]["_prior_unmatched_id"] = prior_id
    return list(by_id.values())


def build_reconciliation_pools(abr_rows, sot_rows, log):
    """Merge new uploads with prior unmatched rows into unified reconciliation pools.

    Pool = current Abronal/SoT uploads + previously unmatched mirror rows that are
    not already present in matched_records.
    """
    with dbm.get_conn() as conn:
        already_matched_abr, already_matched_sot = _load_already_matched_ids(conn)
        prior_abr = _prior_unmatched_to_abr_rows(conn)
        prior_sot = _prior_unmatched_to_sot_rows(conn)

    new_abr = [a for a in abr_rows if a.get("row_id") and a["row_id"] not in already_matched_abr]
    new_sot = [s for s in sot_rows if s.get("row_id") and s["row_id"] not in already_matched_sot]
    prior_abr = [a for a in prior_abr if a["row_id"] not in already_matched_abr]
    prior_sot = [s for s in prior_sot if s["row_id"] not in already_matched_sot]

    abr_pool = _dedupe_pool_by_row_id(new_abr + prior_abr)
    sot_pool = _dedupe_pool_by_row_id(new_sot + prior_sot)

    log(
        f"Reconciliation pools: {len(abr_pool)} Abronal "
        f"({len(new_abr)} new + {len(prior_abr)} prior unmatched), "
        f"{len(sot_pool)} SoT ({len(new_sot)} new + {len(prior_sot)} prior unmatched)."
    )
    return abr_pool, sot_pool


def reconcile_pools(abr_pool, sot_pool, batch_id, log):
    """Exact-match Abronal pool against SoT pool (uploads + prior unmatched combined).

    Returns matched pairs, remaining unmatched on each side, and unmatched_ids to
    remove from unmatched_records when prior rows are promoted to matched.
    """
    sot_by_name: dict[str, list] = {}
    for s in sot_pool:
        sot_by_name.setdefault(normalize_string(s["customer"]), []).append(s)

    matched, unmatched_abr = [], []
    consumed_sot_ids: set[int] = set()
    unmatched_ids_to_delete: list[int] = []

    for a in abr_pool:
        norm_name = normalize_string(a["patient_full_name"])
        candidates = [
            s for s in sot_by_name.get(norm_name, [])
            if s["row_id"] not in consumed_sot_ids
        ]
        found = None
        for s in candidates:
            if abs(float(a["net"]) - float(s["sub_total"])) < AMOUNT_TOLERANCE:
                found = s
                break
        if found:
            consumed_sot_ids.add(found["row_id"])
            from_prior = a.get("_from_prior_unmatched") or found.get("_from_prior_unmatched")
            matched.append({
                "patient_name": a["patient_full_name"],
                "service_id": a["service_id"],
                "total_amount": a["total"],
                "net_amount": a["net"],
                "payment_date": a.get("payment_date") or found.get("transaction_date"),
                "physician_id": a["physician_id"],
                "physician_name": a.get("physician_name", ""),
                "match_type": "exact_rematch" if from_prior else "exact",
                "confidence": 1.0,
                "abronal_row_id": a["row_id"],
                "sot_row_id": found["row_id"],
            })
            for side in (a, found):
                uid = side.get("_unmatched_id") or side.get("_prior_unmatched_id")
                if uid is not None:
                    unmatched_ids_to_delete.append(uid)
        else:
            unmatched_abr.append(a)

    unmatched_sot = [s for s in sot_pool if s["row_id"] not in consumed_sot_ids]
    rematched = sum(1 for m in matched if m["match_type"] == "exact_rematch")
    log(
        f"Unified reconciliation: {len(matched)} matched ({rematched} involving prior unmatched), "
        f"{len(unmatched_abr)} unmatched Abronal, {len(unmatched_sot)} unmatched SoT."
    )
    return matched, unmatched_abr, unmatched_sot, unmatched_ids_to_delete


def _is_new_match(conn, match_key, abronal_row_id, sot_row_id) -> bool:
    """Return True only when this pair is not already recorded in matched_records."""
    if dbm.row_exists(conn, "matched_records", match_key):
        return False
    if conn.execute(
        "SELECT 1 FROM matched_records WHERE abronal_row_id = ? LIMIT 1",
        (abronal_row_id,),
    ).fetchone():
        return False
    if conn.execute(
        "SELECT 1 FROM matched_records WHERE sot_row_id = ? LIMIT 1",
        (sot_row_id,),
    ).fetchone():
        return False
    return True


def persist_results(matched, unmatched_abr, unmatched_sot, unmatched_ids_to_delete, batch_id, log):
    """Persist reconciliation output.

    - Matched: insert only pairs not already in matched_records (by composite key
      or either mirror row_id).
    - Unmatched: insert only newly uploaded rows; prior unmatched that remain
      unmatched stay in the table unchanged.
    - Remove prior unmatched rows promoted to matched.
    """
    # Only persist unmatched rows from the current upload, not prior leftovers.
    new_unmatched_abr = [a for a in unmatched_abr if not a.get("_from_prior_unmatched")]
    new_unmatched_sot = [s for s in unmatched_sot if not s.get("_from_prior_unmatched")]

    def sort_key_matched(m):
        date_str = dbm.normalize_date_to_iso(m.get("payment_date")) or "9999-99-99"
        return date_str

    def sort_key_abr(a):
        date_str = dbm.normalize_date_to_iso(a.get("payment_date")) or "9999-99-99"
        return date_str

    def sort_key_sot(s):
        date_str = dbm.normalize_date_to_iso(s.get("transaction_date")) or "9999-99-99"
        return date_str

    matched_sorted = sorted(matched, key=sort_key_matched)
    unmatched_abr_sorted = sorted(new_unmatched_abr, key=sort_key_abr)
    unmatched_sot_sorted = sorted(new_unmatched_sot, key=sort_key_sot)

    inserted_matches = 0
    with dbm.get_conn() as conn:
        for m in matched_sorted:
            match_key = {
                "patient_name": m["patient_name"],
                "service_id": m["service_id"],
                "net_amount": m["net_amount"],
                "payment_date": m["payment_date"],
                "physician_id": m["physician_id"],
                "abronal_row_id": m["abronal_row_id"],
                "sot_row_id": m["sot_row_id"],
            }
            if not _is_new_match(conn, match_key, m["abronal_row_id"], m["sot_row_id"]):
                continue
            conn.execute(
                """INSERT INTO matched_records
                   (patient_name, service_id, total_amount, net_amount, payment_date,
                    physician_id, physician_name, match_type, confidence, user_flagged_mismatch,
                    user_flag_reason, abronal_row_id, sot_row_id, batch_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m["patient_name"], m["service_id"], m["total_amount"], m["net_amount"],
                 m["payment_date"], m["physician_id"], m.get("physician_name", ""),
                 m["match_type"], m["confidence"], 0, None, m["abronal_row_id"],
                 m["sot_row_id"], batch_id),
            )
            inserted_matches += 1

        for uid in set(unmatched_ids_to_delete):
            conn.execute("DELETE FROM unmatched_records WHERE unmatched_id = ?", (uid,))

        for a in unmatched_abr_sorted:
            unmatched_key = {
                "abronal_patient_name": a["patient_full_name"],
                "abronal_service_type": a["service_raw"],
                "abronal_net_amount": a["net"],
                "abronal_payment_date": a["payment_date"] or "",
                "physician_id": a["physician_id"],
                "abronal_row_id": a["row_id"],
            }
            if dbm.row_exists(conn, "unmatched_records", unmatched_key):
                continue
            conn.execute(
                """INSERT INTO unmatched_records
                   (abronal_patient_name, abronal_service_type, abronal_net_amount,
                    abronal_payment_date, physician_id, physician_name, reason_for_mismatch,
                    abronal_row_id, batch_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (a["patient_full_name"], a["service_raw"], a["net"], a["payment_date"] or "",
                 a["physician_id"], a.get("physician_name", ""),
                 "No matching SoT row (name/amount)", a["row_id"], batch_id),
            )
        for s in unmatched_sot_sorted:
            unmatched_key = {
                "sot_patient_name": s["customer"],
                "sot_service_type": s["description"],
                "sot_amount": s["sub_total"],
                "sot_payment_date": s["transaction_date"],
                "physician_id": None,
                "sot_row_id": s["row_id"],
            }
            if dbm.row_exists(conn, "unmatched_records", unmatched_key):
                continue
            conn.execute(
                """INSERT INTO unmatched_records
                   (abronal_patient_name, abronal_service_type, abronal_net_amount,
                    abronal_payment_date, physician_id, physician_name, sot_patient_name,
                    sot_service_type, sot_amount, sot_payment_date, reason_for_mismatch,
                    sot_row_id, batch_id)
                   VALUES ('', '', 0, '', NULL, '', ?, ?, ?, ?, ?, ?, ?)""",
                (s["customer"], s["description"], s["sub_total"], s["transaction_date"],
                 "SoT row with no matching Abronal entry", s["row_id"], batch_id),
            )

    skipped_matches = len(matched) - inserted_matches
    log(
        f"Saved {inserted_matches} new matched_records "
        f"({skipped_matches} already existed), "
        f"{len(unmatched_abr_sorted) + len(unmatched_sot_sorted)} new unmatched_records, "
        f"removed {len(set(unmatched_ids_to_delete))} prior unmatched rows."
    )


def load_mirror_rows(log=print):
    """Load abronal_mirror / sot_mirror rows already in the active DB as pool rows."""
    with dbm.get_conn() as conn:
        abr = conn.execute(
            """
            SELECT a.*, COALESCE(p.physician_name, '') AS physician_name
            FROM abronal_mirror a
            LEFT JOIN physicians p ON p.physician_id = a.physician_id
            """
        ).fetchall()
        sot = conn.execute("SELECT * FROM sot_mirror").fetchall()
    abr_rows = [dict(r) for r in abr]
    sot_rows = [dict(r) for r in sot]
    log(f"Loaded {len(abr_rows)} Abronal + {len(sot_rows)} SoT rows from mirrors")
    return abr_rows, sot_rows


def run_from_mirrors(batch_id: str, log=print, clear_results: bool = True):
    """Reconcile using rows already mirrored in the active DB (test-mode path)."""
    if clear_results:
        with dbm.get_conn() as conn:
            conn.execute("DELETE FROM matched_records")
            conn.execute("DELETE FROM unmatched_records")
            conn.execute("DELETE FROM commission_per_physicians")
        log("Cleared prior matched / unmatched / commission result tables")

    abr_rows, sot_rows = load_mirror_rows(log)
    if not abr_rows and not sot_rows:
        log("No mirror rows to reconcile")
        return {
            "matched": 0,
            "rematched_prior": 0,
            "unmatched_abronal": 0,
            "unmatched_sot": 0,
        }

    log("── Building reconciliation pools from mirrors ──")
    # Treat every mirror row as "new" for this batch; skip prior-unmatched merge
    # when we just cleared results.
    if clear_results:
        abr_pool, sot_pool = abr_rows, sot_rows
        log(f"Pools: {len(abr_pool)} Abronal, {len(sot_pool)} SoT (fresh from mirrors)")
    else:
        abr_pool, sot_pool = build_reconciliation_pools(abr_rows, sot_rows, log)

    log("── Reconciling Abronal ↔ SoT ──")
    matched, unmatched_abr, unmatched_sot, unmatched_ids_to_delete = reconcile_pools(
        abr_pool, sot_pool, batch_id, log
    )

    log("── Saving matched / unmatched sets ──")
    persist_results(
        matched, unmatched_abr, unmatched_sot, unmatched_ids_to_delete, batch_id, log
    )

    rematched = sum(1 for m in matched if m["match_type"] == "exact_rematch")
    new_unmatched_abr = [a for a in unmatched_abr if not a.get("_from_prior_unmatched")]
    new_unmatched_sot = [s for s in unmatched_sot if not s.get("_from_prior_unmatched")]
    return {
        "matched": len(matched),
        "rematched_prior": rematched,
        "unmatched_abronal": len(new_unmatched_abr),
        "unmatched_sot": len(new_unmatched_sot),
    }


def run(abr_dir: str, sot_dir: str, batch_id: str, log=print):
    log("── Primary Reconciliation: parsing files ──")
    abr_rows = parse_abronal_dir(abr_dir, batch_id, log)
    sot_rows = parse_sot_dir(sot_dir, batch_id, log)

    log("── Mirroring rows into the database ──")
    abr_rows, sot_rows = persist_mirrors(abr_rows, sot_rows, batch_id, log)

    log("── Building reconciliation pools (uploads + prior unmatched) ──")
    abr_pool, sot_pool = build_reconciliation_pools(abr_rows, sot_rows, log)

    log("── Reconciling Abronal ↔ SoT ↔ prior unmatched ──")
    matched, unmatched_abr, unmatched_sot, unmatched_ids_to_delete = reconcile_pools(
        abr_pool, sot_pool, batch_id, log
    )

    log("── Saving matched / unmatched sets ──")
    persist_results(
        matched, unmatched_abr, unmatched_sot, unmatched_ids_to_delete, batch_id, log
    )

    rematched = sum(1 for m in matched if m["match_type"] == "exact_rematch")
    new_unmatched_abr = [a for a in unmatched_abr if not a.get("_from_prior_unmatched")]
    new_unmatched_sot = [s for s in unmatched_sot if not s.get("_from_prior_unmatched")]
    return {
        "matched": len(matched),
        "rematched_prior": rematched,
        "unmatched_abronal": len(new_unmatched_abr),
        "unmatched_sot": len(new_unmatched_sot),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--abr", required=True)
    parser.add_argument("--sot", required=True)
    parser.add_argument("--batch", default=None)
    args = parser.parse_args()
    batch = args.batch or dbm.new_batch_id()
    result = run(args.abr, args.sot, batch)
    print(result)
