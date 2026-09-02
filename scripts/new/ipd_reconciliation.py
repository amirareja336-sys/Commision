"""Match IPD mirror rows against SoT-only unmatched records."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db_manager as dbm  # noqa: E402

SOT_UNMATCHED_REASON = "SoT row with no matching Abronal entry"
AMOUNT_TOLERANCE = 0.01


def normalize_string(s) -> str:
    if not isinstance(s, str):
        return ""
    s = s.upper()
    s = re.sub(r"[^A-Z0-9\s]", "", s)
    return " ".join(s.split())


def _norm_date(value) -> str:
    return dbm.normalize_date_to_iso(value) or ""


def run(batch_id: str, log=print) -> dict:
    """Match ipd_mirror rows to SoT-only unmatched_records; write source=IPD matches."""
    log("── IPD Reconciliation ──")

    with dbm.get_conn() as conn:
        ipd_rows = [
            dict(r) for r in conn.execute(
                """SELECT i.*, p.physician_name
                   FROM ipd_mirror i
                   JOIN physicians p ON p.physician_id = i.physician_id
                   WHERE i.batch_id = ?""",
                (batch_id,),
            ).fetchall()
        ]
        if not ipd_rows:
            ipd_rows = [
                dict(r) for r in conn.execute(
                    """SELECT i.*, p.physician_name
                       FROM ipd_mirror i
                       JOIN physicians p ON p.physician_id = i.physician_id""",
                ).fetchall()
            ]

        sot_unmatched = [
            dict(r) for r in conn.execute(
                """SELECT * FROM unmatched_records
                   WHERE sot_row_id IS NOT NULL
                     AND abronal_row_id IS NULL
                     AND reason_for_mismatch = ?""",
                (SOT_UNMATCHED_REASON,),
            ).fetchall()
        ]

        already_matched_ipd = {
            r[0] for r in conn.execute(
                "SELECT ipd_row_id FROM matched_records WHERE ipd_row_id IS NOT NULL"
            )
        }
        already_matched_sot = {
            r[0] for r in conn.execute(
                "SELECT sot_row_id FROM matched_records WHERE sot_row_id IS NOT NULL"
            )
        }

    log(f"Candidate IPD rows: {len(ipd_rows)}")
    log(f"Candidate SoT-only unmatched: {len(sot_unmatched)}")

    if not ipd_rows or not sot_unmatched:
        log("Nothing to reconcile (missing IPD data or SoT-only unmatched rows).")
        return {"matched": 0, "remaining_sot_unmatched": len(sot_unmatched)}

    # Index SoT unmatched by (name, service, date) for faster lookup
    sot_index: dict[tuple[str, str, str], list[dict]] = {}
    for s in sot_unmatched:
        key = (
            normalize_string(s.get("sot_patient_name") or ""),
            normalize_string(s.get("sot_service_type") or ""),
            _norm_date(s.get("sot_payment_date")),
        )
        sot_index.setdefault(key, []).append(s)

    matched_count = 0
    with dbm.get_conn() as conn:
        for i in ipd_rows:
            iid = i.get("row_id")
            if not iid or iid in already_matched_ipd:
                continue

            key = (
                normalize_string(i.get("patient_full_name") or ""),
                normalize_string(i.get("service_raw") or ""),
                _norm_date(i.get("payment_date")),
            )
            candidates = sot_index.get(key, [])
            if not candidates:
                continue

            ipd_net = float(i.get("net") or 0)
            found = None
            for s in candidates:
                sid = s.get("sot_row_id")
                if not sid or sid in already_matched_sot:
                    continue
                sot_amount = float(s.get("sot_amount") or 0)
                if abs(ipd_net - sot_amount) <= AMOUNT_TOLERANCE:
                    found = s
                    break
            if not found:
                continue

            service_id = i.get("service_id")
            if not service_id:
                service_id = dbm.get_or_create_service(conn, i.get("service_raw") or "Unknown")

            physician_id = i.get("physician_id")
            physician_name = i.get("physician_name") or ""
            patient_name = i.get("patient_full_name") or ""
            payment_date = i.get("payment_date") or found.get("sot_payment_date") or ""
            total_amount = float(i.get("total") or ipd_net)
            net_amount = ipd_net
            sot_row_id = found["sot_row_id"]

            match_key = {
                "patient_name": patient_name,
                "service_id": service_id,
                "net_amount": net_amount,
                "payment_date": payment_date,
                "physician_id": physician_id,
                "ipd_row_id": iid,
                "sot_row_id": sot_row_id,
            }
            if dbm.row_exists(conn, "matched_records", match_key):
                conn.execute(
                    "DELETE FROM unmatched_records WHERE unmatched_id = ?",
                    (found["unmatched_id"],),
                )
                already_matched_sot.add(sot_row_id)
                already_matched_ipd.add(iid)
                continue

            conn.execute(
                """INSERT INTO matched_records
                   (patient_name, service_id, total_amount, net_amount, payment_date,
                    physician_id, physician_name, match_type, confidence, user_flagged_mismatch,
                    user_flag_reason, abronal_row_id, sot_row_id, ipd_row_id, source, batch_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (patient_name, service_id, total_amount, net_amount, payment_date,
                 physician_id, physician_name, "ipd_exact", 1.0, 0, None,
                 None, sot_row_id, iid, "IPD", batch_id),
            )
            conn.execute(
                "DELETE FROM unmatched_records WHERE unmatched_id = ?",
                (found["unmatched_id"],),
            )
            already_matched_sot.add(sot_row_id)
            already_matched_ipd.add(iid)
            matched_count += 1

            # Remove consumed candidate from index
            sot_index[key] = [c for c in sot_index[key] if c.get("sot_row_id") != sot_row_id]

    remaining = sum(len(v) for v in sot_index.values())
    log(f"IPD reconciliation matched {matched_count} SoT row(s).")
    log(f"Remaining SoT-only unmatched (all reasons): check unmatched_records table.")
    return {"matched": matched_count, "remaining_sot_unmatched": remaining}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    print(run(args.batch))
