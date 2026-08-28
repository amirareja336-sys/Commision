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


def rematch_prior_unmatched(abr_rows, sot_rows, batch_id, log):
    """Resolve leftover unmatched_records against *new* Abronal/SoT rows.

    Before this run adds more unmatched rows:
      - prior Abronal-only unmatched + new SoT counterpart → matched
      - prior SoT-only unmatched + new Abronal counterpart → matched

    Returns sets of new-run mirror row_ids already consumed so the exact
    match phase does not reuse them.
    """
    amount_tol = 0.01
    new_abr = [a for a in abr_rows if a.get("row_id")]
    new_sot = [s for s in sot_rows if s.get("row_id")]
    consumed_abr: set[int] = set()
    consumed_sot: set[int] = set()
    if not new_abr and not new_sot:
        log("Prior-unmatched rematch: no new mirror rows to test against.")
        return consumed_abr, consumed_sot, 0

    abr_by_name: dict[str, list] = {}
    for a in new_abr:
        abr_by_name.setdefault(normalize_string(a["patient_full_name"]), []).append(a)
    sot_by_name: dict[str, list] = {}
    for s in new_sot:
        sot_by_name.setdefault(normalize_string(s["customer"]), []).append(s)

    promoted = 0
    with dbm.get_conn() as conn:
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

        prior_abr = [dict(r) for r in conn.execute(
            """SELECT * FROM unmatched_records
               WHERE abronal_row_id IS NOT NULL AND sot_row_id IS NULL"""
        ).fetchall()]
        prior_sot = [dict(r) for r in conn.execute(
            """SELECT * FROM unmatched_records
               WHERE sot_row_id IS NOT NULL AND abronal_row_id IS NULL"""
        ).fetchall()]

        def _insert_match(*, patient_name, service_id, total_amount, net_amount,
                          payment_date, physician_id, physician_name,
                          abronal_row_id, sot_row_id):
            match_key = {
                "patient_name": patient_name,
                "service_id": service_id,
                "net_amount": net_amount,
                "payment_date": payment_date,
                "physician_id": physician_id,
                "abronal_row_id": abronal_row_id,
                "sot_row_id": sot_row_id,
            }
            if not dbm.row_exists(conn, "matched_records", match_key):
                conn.execute(
                    """INSERT INTO matched_records
                       (patient_name, service_id, total_amount, net_amount, payment_date,
                        physician_id, physician_name, match_type, confidence, user_flagged_mismatch,
                        user_flag_reason, abronal_row_id, sot_row_id, batch_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (patient_name, service_id, total_amount, net_amount, payment_date,
                     physician_id, physician_name or "", "exact_rematch", 1.0, 0, None,
                     abronal_row_id, sot_row_id, batch_id),
                )

        # Abronal unmatched → new SoT counterpart
        for u in prior_abr:
            aid = u["abronal_row_id"]
            if aid in already_matched_abr:
                conn.execute(
                    "DELETE FROM unmatched_records WHERE unmatched_id = ?",
                    (u["unmatched_id"],),
                )
                continue
            name = normalize_string(u.get("abronal_patient_name") or "")
            amount = float(u.get("abronal_net_amount") or 0)
            found = None
            for s in sot_by_name.get(name, []):
                sid = s["row_id"]
                if sid in consumed_sot or sid in already_matched_sot:
                    continue
                if abs(amount - float(s["sub_total"])) < amount_tol:
                    found = s
                    break
            if not found:
                continue

            mirror = conn.execute(
                "SELECT service_id, total, net, payment_date, physician_id FROM abronal_mirror WHERE row_id = ?",
                (aid,),
            ).fetchone()
            service_id = mirror["service_id"] if mirror else dbm.get_or_create_service(
                conn, u.get("abronal_service_type") or "Unknown"
            )
            physician_id = (mirror["physician_id"] if mirror else None) or u.get("physician_id")
            physician_name = u.get("physician_name") or ""
            if not physician_name and physician_id:
                phys = conn.execute(
                    "SELECT physician_name FROM physicians WHERE physician_id = ?",
                    (physician_id,),
                ).fetchone()
                physician_name = phys["physician_name"] if phys else ""
            total_amount = float(mirror["total"]) if mirror and mirror["total"] is not None else amount
            net_amount = float(mirror["net"]) if mirror and mirror["net"] is not None else amount
            payment_date = (
                (mirror["payment_date"] if mirror else None)
                or u.get("abronal_payment_date")
                or found.get("transaction_date")
            )

            _insert_match(
                patient_name=u.get("abronal_patient_name") or "",
                service_id=service_id,
                total_amount=total_amount,
                net_amount=net_amount,
                payment_date=payment_date,
                physician_id=physician_id,
                physician_name=physician_name,
                abronal_row_id=aid,
                sot_row_id=found["row_id"],
            )
            conn.execute(
                "DELETE FROM unmatched_records WHERE unmatched_id = ?",
                (u["unmatched_id"],),
            )
            conn.execute(
                """DELETE FROM unmatched_records
                   WHERE sot_row_id = ? AND abronal_row_id IS NULL""",
                (found["row_id"],),
            )
            consumed_sot.add(found["row_id"])
            consumed_abr.add(aid)
            already_matched_abr.add(aid)
            already_matched_sot.add(found["row_id"])
            promoted += 1

        # SoT unmatched → new Abronal counterpart
        for u in prior_sot:
            sid = u["sot_row_id"]
            if sid in already_matched_sot or sid in consumed_sot:
                if sid in already_matched_sot:
                    conn.execute(
                        "DELETE FROM unmatched_records WHERE unmatched_id = ?",
                        (u["unmatched_id"],),
                    )
                continue
            name = normalize_string(u.get("sot_patient_name") or "")
            amount = float(u.get("sot_amount") or 0)
            found = None
            for a in abr_by_name.get(name, []):
                aid = a["row_id"]
                if aid in consumed_abr or aid in already_matched_abr:
                    continue
                if abs(float(a["net"]) - amount) < amount_tol:
                    found = a
                    break
            if not found:
                continue

            _insert_match(
                patient_name=found["patient_full_name"],
                service_id=found["service_id"],
                total_amount=found["total"],
                net_amount=found["net"],
                payment_date=found.get("payment_date") or u.get("sot_payment_date"),
                physician_id=found["physician_id"],
                physician_name=found.get("physician_name") or "",
                abronal_row_id=found["row_id"],
                sot_row_id=sid,
            )
            conn.execute(
                "DELETE FROM unmatched_records WHERE unmatched_id = ?",
                (u["unmatched_id"],),
            )
            conn.execute(
                """DELETE FROM unmatched_records
                   WHERE abronal_row_id = ? AND sot_row_id IS NULL""",
                (found["row_id"],),
            )
            consumed_abr.add(found["row_id"])
            consumed_sot.add(sid)
            already_matched_abr.add(found["row_id"])
            already_matched_sot.add(sid)
            promoted += 1

    log(
        f"Prior-unmatched rematch: promoted {promoted} to matched_records "
        f"(reserved {len(consumed_abr)} Abronal / {len(consumed_sot)} SoT from this run)."
    )
    return consumed_abr, consumed_sot, promoted


def match_records(abr_rows, sot_rows, batch_id, log):
    sot_by_name = {}
    for s in sot_rows:
        sot_by_name.setdefault(normalize_string(s["customer"]), []).append(s)

    matched, unmatched_abr = [], []
    consumed_sot_ids = set()

    for a in abr_rows:
        if not a.get("row_id"):
            log(f"  WARNING: skipping Abronal row without row_id: {a.get('patient_full_name')}")
            unmatched_abr.append(a)
            continue
        norm_name = normalize_string(a["patient_full_name"])
        candidates = [s for s in sot_by_name.get(norm_name, []) if s["row_id"] not in consumed_sot_ids]
        found = None
        for s in candidates:
            if abs(a["net"] - s["sub_total"]) < 0.01:
                found = s
                break
        if found:
            consumed_sot_ids.add(found["row_id"])
            matched.append({
                "patient_name": a["patient_full_name"],
                "service_id": a["service_id"],
                "total_amount": a["total"],
                "net_amount": a["net"],
                "payment_date": a["payment_date"] or found["transaction_date"],
                "physician_id": a["physician_id"],
                "physician_name": a.get("physician_name", ""),
                "match_type": "exact",
                "confidence": 1.0,
                "abronal_row_id": a["row_id"],
                "sot_row_id": found["row_id"],
            })
        else:
            unmatched_abr.append(a)

    unmatched_sot = [s for s in sot_rows if s["row_id"] not in consumed_sot_ids]
    log(f"Exact match phase: {len(matched)} matched, "
        f"{len(unmatched_abr)} unmatched Abronal, {len(unmatched_sot)} unmatched SoT.")
    return matched, unmatched_abr, unmatched_sot


def persist_results(matched, unmatched_abr, unmatched_sot, batch_id, log):
    # Sort records by payment date before inserting to maintain chronological order
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
    unmatched_abr_sorted = sorted(unmatched_abr, key=sort_key_abr)
    unmatched_sot_sorted = sorted(unmatched_sot, key=sort_key_sot)

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
            if dbm.row_exists(conn, "matched_records", match_key):
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
    log(f"Saved {len(matched)} matched_records and "
        f"{len(unmatched_abr) + len(unmatched_sot)} unmatched_records rows.")


def run(abr_dir: str, sot_dir: str, batch_id: str, log=print):
    log("── Primary Reconciliation: parsing files ──")
    abr_rows = parse_abronal_dir(abr_dir, batch_id, log)
    sot_rows = parse_sot_dir(sot_dir, batch_id, log)

    log("── Mirroring rows into the database ──")
    abr_rows, sot_rows = persist_mirrors(abr_rows, sot_rows, batch_id, log)

    log("── Rematching prior unmatched against new data ──")
    used_abr, used_sot, rematched = rematch_prior_unmatched(abr_rows, sot_rows, batch_id, log)
    if used_abr or used_sot:
        abr_rows = [a for a in abr_rows if a.get("row_id") not in used_abr]
        sot_rows = [s for s in sot_rows if s.get("row_id") not in used_sot]

    log("── Matching Abronal <-> SoT ──")
    matched, unmatched_abr, unmatched_sot = match_records(abr_rows, sot_rows, batch_id, log)

    log("── Saving matched / unmatched sets ──")
    persist_results(matched, unmatched_abr, unmatched_sot, batch_id, log)

    return {
        "matched": len(matched),
        "rematched_prior": rematched,
        "unmatched_abronal": len(unmatched_abr),
        "unmatched_sot": len(unmatched_sot),
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
