import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
import os
import re
import json
import threading
from difflib import get_close_matches, SequenceMatcher

try:
    from service_analyzer import DEFAULT_CATEGORIES
except Exception:
    DEFAULT_CATEGORIES = {}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SAVED_CATEGORIES_PATH = os.path.join(SCRIPT_DIR, "saved_service_categories.json")
LAST_SERVICES_PATH = os.path.join(SCRIPT_DIR, "last_services_seen.json")

COLUMN_ORDER = [
    "Consultation",
    "Laboratory",
    "X-ray",
    "Ultrasound",
    "ECG",
    "Echocardiography",
    "Nursing & Procedures",
    "Supplies",
]

# ── Saved service category memory ───────────────────────────────────────────

def load_saved_categories(path=SAVED_CATEGORIES_PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_categories(mapping, path=SAVED_CATEGORIES_PATH):
    """Merge mapping into the on-disk category memory."""
    current = load_saved_categories(path)
    current.update({str(k): str(v) for k, v in mapping.items()})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(current.items(), key=lambda kv: kv[0].lower())), f, indent=2, ensure_ascii=False)
    return current


def known_category_map():
    """Defaults + previously confirmed user choices (user choices win)."""
    merged = dict(DEFAULT_CATEGORIES)
    merged.update(load_saved_categories())
    return merged


def remember_services_seen(services, mapping, path=LAST_SERVICES_PATH):
    payload = {
        "count": len(services),
        "services": [
            {"service": svc, "category": mapping.get(svc, "Other")}
            for svc in services
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def infer_date_label(*paths):
    """
    Infer 'July 20 to July 22' from folder names like:
      Desktop/July 20 to July 22/July 20 to July 22 analysis
    """
    for path in paths:
        if not path:
            continue
        name = os.path.basename(os.path.normpath(path))
        for suffix in (" abronal", " sot", " analysis"):
            if name.lower().endswith(suffix):
                return name[: -len(suffix)].strip()
        parent = os.path.basename(os.path.dirname(os.path.normpath(path)))
        if parent and parent not in (".", "", "Desktop"):
            # Parent may already be the date-range folder
            if " to " in parent:
                return parent
    return ""


def perfect_matches_filename(date_label=None):
    label = (date_label or "").strip()
    if label:
        return f"{label} Perfect Matches.xlsx"
    return "Perfect Matches.xlsx"


def find_perfect_matches_file(output_dir, date_label=None):
    preferred = os.path.join(output_dir, perfect_matches_filename(date_label))
    if os.path.exists(preferred):
        return preferred
    # Fallbacks for older runs / manual launches
    for name in (
        "Perfect_Matches.xlsx",
        "Perfect Matches.xlsx",
    ):
        path = os.path.join(output_dir, name)
        if os.path.exists(path):
            return path
    if os.path.isdir(output_dir):
        for filename in os.listdir(output_dir):
            lower = filename.lower()
            if lower.endswith(".xlsx") and "perfect" in lower and "match" in lower:
                return os.path.join(output_dir, filename)
    return preferred

# ── Core Logic ──────────────────────────────────────────────────────────────

def normalize_string(s):
    if not isinstance(s, str): return ""
    s = s.upper()
    s = re.sub(r'[^A-Z0-9\s]', '', s)
    return ' '.join(s.split())

def parse_abronal_date(s):
    if not isinstance(s, str): return pd.NaT
    s_clean = s.replace(':AM', ' AM').replace(':PM', ' PM').replace(':am', ' AM').replace(':pm', ' PM')
    return pd.to_datetime(s_clean, errors='coerce')

def advanced_name_match(name1, name2):
    """
    Returns a similarity score between 0.0 and 1.0.
    Handles standard character similarity AND word-subset matching.
    """
    char_sim = SequenceMatcher(None, name1, name2).ratio()
    
    # Word subset matching
    w1 = name1.split()
    w2 = name2.split()
    
    if not w1 or not w2:
        return char_sim
        
    shorter, longer = (w1, w2) if len(w1) < len(w2) else (w2, w1)
    
    # Require at least 2 words to confidently do subset matching 
    # (Matching just "GIRMA" to "GIRMA ABEBE" is too risky and could cause false positives)
    if len(shorter) < 2:
        return char_sim
        
    matched_words = 0
    for sw in shorter:
        # Check if the word exactly matches or fuzzily matches a word in the longer name
        if get_close_matches(sw, longer, n=1, cutoff=0.85):
            matched_words += 1
            
    word_match_ratio = matched_words / len(shorter)
    
    # If all words in the shorter name match words in the longer name (e.g., GIRMA MEKONNEN vs GIRMA MEKONNEN WAHILA)
    if word_match_ratio == 1.0:
        return max(char_sim, 0.95)
    
    # If 2 out of 3 words match
    if word_match_ratio >= 0.66 and len(shorter) >= 3:
        return max(char_sim, 0.85)
        
    return char_sim

def date_distance_days(date1, date2):
    if pd.isna(date1) or pd.isna(date2):
        return 999999
    return abs((date1.normalize() - date2.normalize()).days)

def signed_day_difference(date1, date2):
    if pd.isna(date1) or pd.isna(date2):
        return "N/A"
    return (date1.normalize() - date2.normalize()).days

def best_date_pairs(abr_entries, sot_entries, same_service_required=True):
    """
    Pair duplicate candidates by closest dates instead of first row order.
    Returns matched (abr_index, sot_index) pairs.
    """
    candidates = []
    for ai, a in enumerate(abr_entries):
        for si, s in enumerate(sot_entries):
            if same_service_required and a['Norm_Service'] != s['Norm_Service']:
                continue
            if abs(a['Amount'] - s['Amount']) >= 0.01:
                continue
            candidates.append((date_distance_days(a['Date'], s['Date']), ai, si))

    candidates.sort()
    matched_abr = set()
    matched_sot = set()
    pairs = []
    for _, ai, si in candidates:
        if ai in matched_abr or si in matched_sot:
            continue
        matched_abr.add(ai)
        matched_sot.add(si)
        pairs.append((ai, si))
    return pairs

def load_sot(sot_dir, log_fn):
    all_sot = []; nameless_sot = []
    named_counter = 1
    nameless_counter = 1
    for filename in os.listdir(sot_dir):
        if filename.endswith(".xlsx"):
            log_fn(f"Loading SoT: {filename}")
            path = os.path.join(sot_dir, filename)
            df = pd.read_excel(path, header=None)
            h_idx = None
            for i in range(min(50, len(df))):
                row_str = [str(x).lower() for x in df.iloc[i]]
                if 'customer' in row_str and ('mrc' in row_str or 'reference' in row_str):
                    h_idx = i; break
            headers = df.iloc[h_idx].tolist() if h_idx is not None else [f"Col_{j}" for j in range(len(df.columns))]
            start_row = h_idx + 1 if h_idx is not None else 0
            for i in range(start_row, len(df)):
                row = df.iloc[i]
                try:
                    name_raw = row[0]; name_str = str(name_raw).strip(); amt = float(row[7])
                    service = str(row[2]) if pd.notna(row[2]) else "N/A"
                    date_val = pd.to_datetime(row[11], errors='coerce')
                    if amt > 0 and amt < 1000000:
                        row_raw = {str(headers[j]): row[j] for j in range(len(row))}
                        if pd.isna(name_raw) or name_str.lower() in ['', 'nan', 'none', 'row labels']:
                            row_id = f"SOT-NAMELESS-{nameless_counter:06d}"
                            nameless_counter += 1
                            nameless_sot.append({'Row_ID': row_id, 'Amt': amt, 'Date': date_val, 'Service': service, 'Source': filename.replace('.xlsx', ''), 'Raw': row_raw})
                        else:
                            row_id = f"SOT-{named_counter:06d}"
                            named_counter += 1
                            all_sot.append({'Row_ID': row_id, 'Norm_Name': normalize_string(name_str), 'Original_Name': name_str, 'Norm_Service': normalize_string(service), 'Original_Service': service, 'Amount': amt, 'Date': date_val, 'Source': filename.replace('.xlsx', ''), 'Raw': row_raw})
                except: continue
    return all_sot, nameless_sot

def load_abr(abr_dir, log_fn):
    all_abr = []
    row_counter = 1
    for filename in os.listdir(abr_dir):
        if filename.endswith(".xlsx"):
            log_fn(f"Loading Abronal: {filename}")
            path = os.path.join(abr_dir, filename)
            df = pd.read_excel(path, header=None)
            h_idx = None
            for i in range(min(20, len(df))):
                if 'customer' in str(df.iloc[i]).lower() or 'patient' in str(df.iloc[i]).lower():
                    h_idx = i; break
            headers = df.iloc[h_idx].tolist() if h_idx is not None else [f"Col_{i}" for i in range(len(df.columns))]
            start_row = h_idx + 1 if h_idx is not None else 0
            for i in range(start_row, len(df)):
                row = df.iloc[i]
                try:
                    name = str(row[3]); amt = float(row[6]); service = str(row[5]) if pd.notna(row[5]) else "N/A"
                    date_str = str(row[10]); date_val = parse_abronal_date(date_str)
                    if amt > 0 and len(name) > 3 and name.lower() not in ['nan', 'customer', 'row labels']:
                        row_dict = {str(headers[j]): row[j] for j in range(len(row))}; row_dict['Source_File'] = filename
                        row_id = f"ABR-{row_counter:06d}"
                        row_counter += 1
                        all_abr.append({'Row_ID': row_id, 'Norm_Name': normalize_string(name), 'Original_Name': name, 'Norm_Service': normalize_string(service), 'Original_Service': service, 'Amount': amt, 'Date': date_val, 'Original_Timestamp': date_str, 'File': filename, 'Raw': row_dict})
                except: continue
    return all_abr

def run_summary_logic(perfect_xlsx_path, output_dir, log_fn):
    log_fn("── Generating Service Summary Report ──")
    try:
        xl = pd.ExcelFile(perfect_xlsx_path)
        all_summaries = []
        for sheet_name in xl.sheet_names:
            df = pd.read_excel(xl, sheet_name)
            if not df.empty:
                summary = df.groupby('Service')['Amount'].agg(['sum', 'count']).reset_index()
                summary.insert(0, 'Staff Member', sheet_name)
                all_summaries.append(summary)
        if all_summaries:
            final_summary = pd.concat(all_summaries)
            final_summary.columns = ['Staff Member', 'Service', 'Total Amount', 'Transaction Count']
            summary_path = os.path.join(output_dir, "Service_Summary_Report.xlsx")
            final_summary.to_excel(summary_path, index=False)
            log_fn(f"  Summary saved: {summary_path}")
            return True
    except Exception as e:
            log_fn(f"  Summary Error: {e}")
    return False

def audit_entry(source_system, entry, status, linked_id="", linked_status=""):
    return {
        'Source System': source_system,
        'Row ID': entry.get('Row_ID', ''),
        'Final Status': status,
        'Linked Row ID': linked_id,
        'Linked Status': linked_status,
        'Patient Name': entry.get('Original_Name', ''),
        'Service': entry.get('Original_Service', entry.get('Service', '')),
        'Amount': entry.get('Amount', entry.get('Amt', '')),
        'Date': entry.get('Original_Timestamp', entry.get('Date', '')),
        'Source File': entry.get('File', entry.get('Source', '')),
    }

def raw_with_row_id(entry):
    row = {'Row ID': entry.get('Row_ID', '')}
    row.update(entry.get('Raw', {}))
    return row

def build_audit_report(all_abr, all_sot, nameless_sot_pool, perfect, mismatch, spelling_pairs, blind_matches, final_unique_abr, final_unique_sot, remaining_nameless):
    audit_rows = []

    for p in perfect:
        audit_rows.append(audit_entry('Abronal', p['abr'], 'Perfect Match', p['sot']['Row_ID'], 'Perfect Match'))
        audit_rows.append(audit_entry('SoT', p['sot'], 'Perfect Match', p['abr']['Row_ID'], 'Perfect Match'))

    for m in mismatch:
        status = m.get('Status', 'Mismatch')
        abr_entry = m.get('Abronal Entry')
        sot_entry = m.get('SoT Entry')
        if abr_entry and sot_entry:
            audit_rows.append(audit_entry('Abronal', abr_entry, status, sot_entry['Row_ID'], status))
            audit_rows.append(audit_entry('SoT', sot_entry, status, abr_entry['Row_ID'], status))

    for pair_idx, pair in enumerate(spelling_pairs, start=1):
        group = f"SPELLING-GROUP-{pair_idx:06d}"
        for entry in pair['Abronal Entries']:
            audit_rows.append(audit_entry('Abronal', entry, 'Possible Spelling Match', group, pair['SoT Name']))
        for entry in pair['SoT Entries']:
            audit_rows.append(audit_entry('SoT', entry, 'Possible Spelling Match', group, pair['Abronal Name']))

    for b in blind_matches:
        audit_rows.append(audit_entry('Abronal', b['Abronal_Entry'], 'Blind Match', b['SoT_Entry']['Row_ID'], 'Blind Match'))
        audit_rows.append(audit_entry('SoT', b['SoT_Entry'], 'Blind Match', b['Abronal_Entry']['Row_ID'], 'Blind Match'))

    for entry in final_unique_abr:
        audit_rows.append(audit_entry('Abronal', entry, 'Unique Abronal'))

    for entry in final_unique_sot:
        audit_rows.append(audit_entry('SoT', entry, 'SoT Leftover'))

    for entry in remaining_nameless:
        audit_rows.append(audit_entry('SoT', entry, 'Nameless SoT Record'))

    audit_df = pd.DataFrame(audit_rows)
    all_loaded_ids = (
        [entry['Row_ID'] for entry in all_abr]
        + [entry['Row_ID'] for entry in all_sot]
        + [entry['Row_ID'] for entry in nameless_sot_pool]
    )

    if audit_df.empty:
        assigned_ids = []
    else:
        assigned_ids = audit_df['Row ID'].dropna().astype(str).tolist()

    assigned_counts = pd.Series(assigned_ids).value_counts()
    missing_ids = sorted(set(all_loaded_ids) - set(assigned_ids))
    duplicate_ids = sorted(assigned_counts[assigned_counts > 1].index.tolist())

    summary_rows = [
        {'Check': 'Loaded Abronal Rows', 'Value': len(all_abr)},
        {'Check': 'Loaded Named SoT Rows', 'Value': len(all_sot)},
        {'Check': 'Loaded Nameless SoT Rows', 'Value': len(nameless_sot_pool)},
        {'Check': 'Total Loaded Rows', 'Value': len(all_loaded_ids)},
        {'Check': 'Rows Assigned a Final Status', 'Value': len(assigned_ids)},
        {'Check': 'Missing Row IDs', 'Value': len(missing_ids)},
        {'Check': 'Duplicate Row IDs', 'Value': len(duplicate_ids)},
        {'Check': 'Audit Result', 'Value': 'PASS' if not missing_ids and not duplicate_ids and len(assigned_ids) == len(all_loaded_ids) else 'REVIEW'},
    ]

    if not audit_df.empty:
        status_counts = audit_df.groupby(['Source System', 'Final Status']).size().reset_index(name='Count')
    else:
        status_counts = pd.DataFrame(columns=['Source System', 'Final Status', 'Count'])

    exceptions = [{'Issue': 'Missing from audit', 'Row ID': row_id} for row_id in missing_ids]
    exceptions.extend({'Issue': 'Duplicate in audit', 'Row ID': row_id} for row_id in duplicate_ids)

    return audit_df, pd.DataFrame(summary_rows), status_counts, pd.DataFrame(exceptions, columns=['Issue', 'Row ID'])

def run_reconciliation(abr_dir, sot_dir, output_dir, log_fn, done_fn, review_services_fn=None, date_label=None):
    try:
        date_label = (date_label or "").strip() or infer_date_label(output_dir, abr_dir, sot_dir)
        if date_label:
            log_fn(f"Date range label: {date_label}")
        log_fn("── Loading Files ──")
        all_sot, nameless_sot_pool = load_sot(sot_dir, log_fn)
        all_abr = load_abr(abr_dir, log_fn)
        log_fn(f"Loaded {len(all_abr)} Abronal, {len(all_sot)} SoT, {len(nameless_sot_pool)} Nameless SoT.")

        abr_by_name = {}; sot_by_name = {}
        for e in all_abr: abr_by_name.setdefault(e['Norm_Name'], []).append(e)
        for e in all_sot: sot_by_name.setdefault(e['Norm_Name'], []).append(e)

        perfect = []; mismatch = []
        remaining_abr_by_name = {}; remaining_sot_by_name = {}
        unique_names = set(list(abr_by_name.keys()) + list(sot_by_name.keys()))

        log_fn("── Phases 1-3: Exact Name Matching ──")
        for name in unique_names:
            a_list = abr_by_name.get(name, []); s_list = sot_by_name.get(name, [])
            rem_a = a_list[:]; rem_s = s_list[:]

            exact_pairs = best_date_pairs(rem_a, rem_s, same_service_required=True)
            for ai, si in exact_pairs:
                a = rem_a[ai]; s = rem_s[si]
                perfect.append({'abr': a, 'sot': s, 'day_diff': signed_day_difference(a['Date'], s['Date'])})

            matched_a = {ai for ai, _ in exact_pairs}
            matched_s = {si for _, si in exact_pairs}
            rem_a = [a for idx, a in enumerate(rem_a) if idx not in matched_a]
            rem_s = [s for idx, s in enumerate(rem_s) if idx not in matched_s]

            i = 0
            while i < len(rem_a):
                a = rem_a[i]; found = False
                for j, s in enumerate(rem_s):
                    if abs(a['Amount'] - s['Amount']) < 0.01:
                        mismatch.append({'Status': 'Service Mismatch', 'Abronal Row ID': a['Row_ID'], 'SoT Row ID': s['Row_ID'], 'Abronal Name': a['Original_Name'], 'Abronal Service': a['Original_Service'], 'Abronal Amount': a['Amount'], 'SoT Name': s['Original_Name'], 'SoT Service': s['Original_Service'], 'SoT Amount': s['Amount'], 'Source': s['Source'], 'File': a['File'], 'Difference': 0, 'Abronal Entry': a, 'SoT Entry': s})
                        rem_s.pop(j); rem_a.pop(i); found = True; break
                if not found: i += 1

            while rem_a and rem_s:
                a = rem_a.pop(0); s = rem_s.pop(0)
                mismatch.append({'Status': 'Amount Mismatch', 'Abronal Row ID': a['Row_ID'], 'SoT Row ID': s['Row_ID'], 'Abronal Name': a['Original_Name'], 'Abronal Service': a['Original_Service'], 'Abronal Amount': a['Amount'], 'SoT Name': s['Original_Name'], 'SoT Service': s['Original_Service'], 'SoT Amount': s['Amount'], 'Source': s['Source'], 'File': a['File'], 'Difference': a['Amount'] - s['Amount'], 'Abronal Entry': a, 'SoT Entry': s})

            if rem_a: remaining_abr_by_name[name] = rem_a
            if rem_s: remaining_sot_by_name[name] = rem_s

        log_fn(f"  Perfect: {len(perfect)}, Mismatches: {len(mismatch)}")

        log_fn("── Phase 4: Fuzzy Name-Level Linkage ──")
        abr_names_left = list(remaining_abr_by_name.keys())
        sot_names_left = list(remaining_sot_by_name.keys())

        # Group SoT names by first letter for speed
        sot_names_by_letter = {}
        for sn in sot_names_left:
            letter = sn[0] if sn else ''
            sot_names_by_letter.setdefault(letter, []).append(sn)

        fuzzy_pairs = []
        consumed_sot_names = set()

        for an in abr_names_left:
            letter = an[0] if an else ''
            candidates = [c for c in sot_names_by_letter.get(letter, []) if c not in consumed_sot_names]
            if not candidates: continue
            
            best_score = 0
            best_match = None
            for cand in candidates:
                score = advanced_name_match(an, cand)
                if score > best_score:
                    best_score = score
                    best_match = cand
                    
            if best_score >= 0.8:
                fuzzy_pairs.append((an, best_match, round(best_score * 100, 1)))
                consumed_sot_names.add(best_match)

        log_fn(f"  Found {len(fuzzy_pairs)} fuzzy name pairs.")

        spelling_match_reports = []
        spelling_audit_pairs = []
        consumed_abr_names = set()
        consumed_sot_names_final = set()

        for abr_norm, sot_norm, sim in fuzzy_pairs:
            abr_entries = remaining_abr_by_name.get(abr_norm, [])
            sot_entries = remaining_sot_by_name.get(sot_norm, [])

            has_date_overlap = False
            for a in abr_entries:
                for s in sot_entries:
                    if pd.notna(a['Date']) and pd.notna(s['Date']):
                        diff = abs((a['Date'].normalize() - s['Date'].normalize()).days)
                        if diff <= 10:
                            has_date_overlap = True; break
                if has_date_overlap: break

            abr_total = sum(a['Amount'] for a in abr_entries)
            sot_total = sum(s['Amount'] for s in sot_entries)
            abr_sample = abr_entries[0] if abr_entries else None
            sot_sample = sot_entries[0] if sot_entries else None

            max_rows = max(len(abr_entries), len(sot_entries))
            for idx in range(max_rows):
                row = {
                    'Abronal Name': abr_sample['Original_Name'] if abr_sample else '',
                    'SoT Name': sot_sample['Original_Name'] if sot_sample else '',
                    'Similarity (%)': sim,
                    'Dates Within 10 Days': 'Yes' if has_date_overlap else 'No',
                }
                if idx < len(abr_entries):
                    ae = abr_entries[idx]
                    row['Abronal Row ID'] = ae['Row_ID']
                    row['Abronal Service'] = ae['Original_Service']
                    row['Abronal Amount'] = ae['Amount']
                    row['Abronal Date'] = ae['Original_Timestamp']
                    row['Abronal File'] = ae['File']
                else:
                    row['Abronal Row ID'] = ''; row['Abronal Service'] = ''; row['Abronal Amount'] = ''; row['Abronal Date'] = ''; row['Abronal File'] = ''

                if idx < len(sot_entries):
                    se = sot_entries[idx]
                    row['SoT Row ID'] = se['Row_ID']
                    row['SoT Service'] = se['Original_Service']
                    row['SoT Amount'] = se['Amount']
                    row['SoT Date'] = se['Date']
                    row['SoT Source'] = se['Source']
                else:
                    row['SoT Row ID'] = ''; row['SoT Service'] = ''; row['SoT Amount'] = ''; row['SoT Date'] = ''; row['SoT Source'] = ''

                if idx == 0:
                    row['Abronal Total'] = abr_total
                    row['SoT Total'] = sot_total
                    row['Amount Difference'] = abr_total - sot_total
                else:
                    row['Abronal Total'] = ''; row['SoT Total'] = ''; row['Amount Difference'] = ''

                spelling_match_reports.append(row)

            spelling_match_reports.append({k: '' for k in spelling_match_reports[-1].keys()})
            spelling_audit_pairs.append({
                'Abronal Name': abr_sample['Original_Name'] if abr_sample else abr_norm,
                'SoT Name': sot_sample['Original_Name'] if sot_sample else sot_norm,
                'Abronal Entries': abr_entries,
                'SoT Entries': sot_entries,
            })
            consumed_abr_names.add(abr_norm)
            consumed_sot_names_final.add(sot_norm)

        final_unique_abr = []
        for name, entries in remaining_abr_by_name.items():
            if name not in consumed_abr_names: final_unique_abr.extend(entries)

        final_unique_sot = []
        for name, entries in remaining_sot_by_name.items():
            if name not in consumed_sot_names_final: final_unique_sot.extend(entries)

        log_fn("── Phase 5: Blind Match (Nameless) ──")
        blind_matches = []; remaining_nameless = []; consumed_blind = set()
        for ns in nameless_sot_pool:
            found_blind = False
            for j, al in enumerate(final_unique_abr):
                if j in consumed_blind: continue
                if pd.notna(ns['Date']) and pd.notna(al['Date']) and ns['Date'] == al['Date'].normalize() and abs(ns['Amt'] - al['Amount']) < 0.01:
                    blind_matches.append({'SoT_Row_ID': ns['Row_ID'], 'Abronal_Row_ID': al['Row_ID'], 'SoT_Amt': ns['Amt'], 'SoT_Date': ns['Date'], 'SoT_Service': ns['Service'], 'Abronal_Name': al['Original_Name'], 'Abronal_Amt': al['Amount'], 'Abronal_Date': al['Original_Timestamp'], 'Abronal_File': al['File'], 'SoT_Entry': ns, 'Abronal_Entry': al})
                    consumed_blind.add(j); found_blind = True; break
            if not found_blind: remaining_nameless.append(ns)
        final_unique_abr_cleaned = [al for j, al in enumerate(final_unique_abr) if j not in consumed_blind]

        audit_df, audit_summary, audit_status_counts, audit_exceptions = build_audit_report(
            all_abr,
            all_sot,
            nameless_sot_pool,
            perfect,
            mismatch,
            spelling_audit_pairs,
            blind_matches,
            final_unique_abr_cleaned,
            final_unique_sot,
            remaining_nameless,
        )
        audit_result = audit_summary.loc[audit_summary['Check'] == 'Audit Result', 'Value'].iloc[0]
        log_fn(f"  Audit result: {audit_result}")

        if audit_result != "PASS":
            log_fn("  Audit did not pass. Writing audit workbook only and stopping before final outputs.")
            os.makedirs(output_dir, exist_ok=True)
            mismatch_output = [{k: v for k, v in row.items() if k not in ('Abronal Entry', 'SoT Entry')} for row in mismatch]
            with pd.ExcelWriter(os.path.join(output_dir, "Unmatched_Analysis.xlsx"), engine='openpyxl') as writer:
                pd.DataFrame(mismatch_output).to_excel(writer, sheet_name='Mismatches', index=False)
                pd.DataFrame(spelling_match_reports).to_excel(writer, sheet_name='Possible_Spelling_Matches', index=False)
                pd.DataFrame([raw_with_row_id(a) for a in final_unique_abr_cleaned]).to_excel(writer, sheet_name='Unique_Abronal', index=False)
                pd.DataFrame([raw_with_row_id(s) for s in final_unique_sot]).to_excel(writer, sheet_name='SoT_Leftovers', index=False)
                audit_summary.to_excel(writer, sheet_name='Audit_Summary', index=False)
                audit_status_counts.to_excel(writer, sheet_name='Audit_Status_Counts', index=False)
                audit_df.to_excel(writer, sheet_name='Audit_All_Loaded_Rows', index=False)
                audit_exceptions.to_excel(writer, sheet_name='Audit_Exceptions', index=False)
            done_fn(False)
            return

        perfect_services = sorted({p['abr']['Original_Service'] for p in perfect if p['abr'].get('Original_Service')})
        if review_services_fn:
            log_fn(f"  Waiting for service category review: {len(perfect_services)} unique perfect-match services.")
            service_category_map = review_services_fn(perfect_services)
            if service_category_map is None:
                log_fn("  Service review cancelled. No output files were generated.")
                done_fn(False)
                return
            log_fn("  Service category review confirmed.")
        else:
            known = known_category_map()
            service_category_map = {svc: known.get(svc, "Other") for svc in perfect_services}

        # Persist choices + leave a readable snapshot for later inspection
        try:
            save_categories(service_category_map)
            remember_services_seen(perfect_services, service_category_map)
            log_fn(f"  Saved {len(service_category_map)} service categories to {SAVED_CATEGORIES_PATH}")
        except Exception as e:
            log_fn(f"  Warning: could not save service categories ({e})")

        log_fn("── Saving Files ──")
        os.makedirs(output_dir, exist_ok=True)

        pm_path = os.path.join(output_dir, perfect_matches_filename(date_label))
        log_fn(f"  Writing perfect matches: {os.path.basename(pm_path)}")
        with pd.ExcelWriter(pm_path, engine='openpyxl') as writer:
            files = sorted(set(p['abr']['File'] for p in perfect))
            used_sheet_names = set()
            for f in files:
                data = [{'Abronal Row ID': p['abr']['Row_ID'], 'SoT Row ID': p['sot']['Row_ID'], 'Patient Name': p['abr']['Original_Name'], 'Service': p['abr']['Original_Service'], 'Category': service_category_map.get(p['abr']['Original_Service'], "Other"), 'Amount': p['abr']['Amount'], 'Abronal Date': p['abr']['Original_Timestamp'], 'SoT Date': p['sot']['Date'], 'Day Difference': p['day_diff']} for p in perfect if p['abr']['File'] == f]
                data_df = pd.DataFrame(data)
                if not data_df.empty:
                    data_df = data_df.sort_values(['SoT Date', 'Patient Name', 'Service'], ascending=[False, True, True], na_position="last")
                # Prefer "Dr. Name" portion for sheet title; keep unique within Excel's 31-char limit
                base = os.path.splitext(str(f))[0]
                if " Dr. " in base:
                    base = base.split(" Dr. ", 1)[1]
                    base = f"Dr. {base}"
                base = re.sub(r'[\\/*?:\[\]]', '-', base).strip() or "Sheet"
                sheet = base[:31]
                n = 2
                while sheet in used_sheet_names:
                    suffix = f"_{n}"
                    sheet = (base[: 31 - len(suffix)] + suffix)
                    n += 1
                used_sheet_names.add(sheet)
                data_df.to_excel(writer, sheet_name=sheet, index=False)

        mismatch_output = [{k: v for k, v in row.items() if k not in ('Abronal Entry', 'SoT Entry')} for row in mismatch]
        
        with pd.ExcelWriter(os.path.join(output_dir, "Unmatched_Analysis.xlsx"), engine='openpyxl') as writer:
            pd.DataFrame(mismatch_output).to_excel(writer, sheet_name='Mismatches', index=False)
            pd.DataFrame(spelling_match_reports).to_excel(writer, sheet_name='Possible_Spelling_Matches', index=False)
            pd.DataFrame([raw_with_row_id(a) for a in final_unique_abr_cleaned]).to_excel(writer, sheet_name='Unique_Abronal', index=False)
            pd.DataFrame([raw_with_row_id(s) for s in final_unique_sot]).to_excel(writer, sheet_name='SoT_Leftovers', index=False)
            audit_summary.to_excel(writer, sheet_name='Audit_Summary', index=False)
            audit_status_counts.to_excel(writer, sheet_name='Audit_Status_Counts', index=False)
            audit_df.to_excel(writer, sheet_name='Audit_All_Loaded_Rows', index=False)
            audit_exceptions.to_excel(writer, sheet_name='Audit_Exceptions', index=False)
        
        if blind_matches:
            blind_output = [{k: v for k, v in row.items() if k not in ('Abronal_Entry', 'SoT_Entry')} for row in blind_matches]
            pd.DataFrame(blind_output).to_excel(os.path.join(output_dir, "Blind_Matches.xlsx"), index=False)
        if remaining_nameless: pd.DataFrame([raw_with_row_id(n) for n in remaining_nameless]).to_excel(os.path.join(output_dir, "Nameless_SoT_Records.xlsx"), index=False)
        
        run_summary_logic(pm_path, output_dir, log_fn)

        log_fn("\n═══════════════════════════════════════")
        log_fn("  RECONCILIATION COMPLETE")
        log_fn("═══════════════════════════════════════")
        done_fn(True)
    except Exception as e:
        log_fn(f"\nERROR: {e}"); done_fn(False)

class ReconciliationApp:
    def __init__(self, root, abr_dir=None, sot_dir=None, out_dir=None, auto_run=False, date_label=None):
        self.root = root; self.root.title("Transaction Reconciliation Tool v5"); self.root.geometry("1100x720")
        self.perfect_df = pd.DataFrame()
        self._auto_run = bool(auto_run)
        self.date_label = (date_label or "").strip() or infer_date_label(out_dir, abr_dir, sot_dir)
        notebook = ttk.Notebook(root); notebook.pack(fill="both", expand=True, padx=5, pady=5)
        self.notebook = notebook

        tab1 = ttk.Frame(notebook); notebook.add(tab1, text="Main Reconciliation")
        self.abr_path = tk.StringVar(value=abr_dir or "")
        self.sot_path = tk.StringVar(value=sot_dir or "")
        self.out_path = tk.StringVar(value=out_dir or "")
        f1 = tk.LabelFrame(tab1, text="Input", padx=10, pady=10); f1.pack(fill="x", padx=10, pady=10)
        tk.Label(f1, text="Abronal Folder:").grid(row=0, column=0, sticky="w")
        tk.Entry(f1, textvariable=self.abr_path, width=60).grid(row=0, column=1, padx=5)
        tk.Button(f1, text="Browse", command=lambda: self.abr_path.set(filedialog.askdirectory())).grid(row=0, column=2)
        tk.Label(f1, text="SoT Folder:").grid(row=1, column=0, sticky="w", pady=5)
        tk.Entry(f1, textvariable=self.sot_path, width=60).grid(row=1, column=1, padx=5)
        tk.Button(f1, text="Browse", command=lambda: self.sot_path.set(filedialog.askdirectory())).grid(row=1, column=2)
        f2 = tk.LabelFrame(tab1, text="Output", padx=10, pady=10); f2.pack(fill="x", padx=10, pady=5)
        tk.Label(f2, text="Output Folder:").grid(row=0, column=0, sticky="w")
        tk.Entry(f2, textvariable=self.out_path, width=60).grid(row=0, column=1, padx=5)
        tk.Button(f2, text="Browse", command=lambda: self.out_path.set(filedialog.askdirectory())).grid(row=0, column=2)
        self.run_btn = tk.Button(tab1, text="Run Full Reconciliation", command=self.run_main, width=30, height=2, bg="#e1f5fe"); self.run_btn.pack(pady=10)

        tab2 = ttk.Frame(notebook); notebook.add(tab2, text="Service Summary Only")
        self.pm_file_path = tk.StringVar(); self.sum_out_path = tk.StringVar()
        f3 = tk.LabelFrame(tab2, text="Analyze Existing Perfect Matches", padx=10, pady=10); f3.pack(fill="x", padx=10, pady=10)
        tk.Label(f3, text="Perfect Matches File:").grid(row=0, column=0, sticky="w")
        tk.Entry(f3, textvariable=self.pm_file_path, width=60).grid(row=0, column=1, padx=5)
        tk.Button(f3, text="Select File", command=lambda: self.pm_file_path.set(filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx")]))).grid(row=0, column=2)
        tk.Label(f3, text="Output Folder:").grid(row=1, column=0, sticky="w", pady=5)
        tk.Entry(f3, textvariable=self.sum_out_path, width=60).grid(row=1, column=1, padx=5)
        tk.Button(f3, text="Browse", command=lambda: self.sum_out_path.set(filedialog.askdirectory())).grid(row=1, column=2)
        tk.Button(tab2, text="Generate Service Summary Report", command=self.run_summary_only, width=35, height=2, bg="#f1f8e9").pack(pady=20)

        tab3 = ttk.Frame(notebook); notebook.add(tab3, text="Perfect Match Review")
        self.review_file_path = tk.StringVar()
        self.filter_source = tk.StringVar(value="All")
        self.filter_category = tk.StringVar(value="All")
        self.filter_service = tk.StringVar(value="All")
        self.filter_start_date = tk.StringVar()
        self.filter_end_date = tk.StringVar()
        self.sort_choice = tk.StringVar(value="Date newest")

        review_top = tk.LabelFrame(tab3, text="Review Perfect Matches", padx=10, pady=8)
        review_top.pack(fill="x", padx=10, pady=8)
        tk.Label(review_top, text="Perfect Matches File:").grid(row=0, column=0, sticky="w")
        tk.Entry(review_top, textvariable=self.review_file_path, width=70).grid(row=0, column=1, padx=5, sticky="ew")
        tk.Button(review_top, text="Select", command=lambda: self.review_file_path.set(filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx")]))).grid(row=0, column=2, padx=2)
        tk.Button(review_top, text="Load", command=self.load_review_file).grid(row=0, column=3, padx=2)
        review_top.grid_columnconfigure(1, weight=1)

        filters = tk.LabelFrame(tab3, text="Filters", padx=10, pady=8)
        filters.pack(fill="x", padx=10, pady=5)
        tk.Label(filters, text="Abronal Source:").grid(row=0, column=0, sticky="w")
        self.source_combo = ttk.Combobox(filters, textvariable=self.filter_source, values=["All"], width=22, state="readonly")
        self.source_combo.grid(row=0, column=1, padx=4)
        tk.Label(filters, text="Category:").grid(row=0, column=2, sticky="w")
        self.category_combo = ttk.Combobox(filters, textvariable=self.filter_category, values=["All"], width=22, state="readonly")
        self.category_combo.grid(row=0, column=3, padx=4)
        tk.Label(filters, text="Service:").grid(row=0, column=4, sticky="w")
        self.service_combo = ttk.Combobox(filters, textvariable=self.filter_service, values=["All"], width=28, state="readonly")
        self.service_combo.grid(row=0, column=5, padx=4)

        tk.Label(filters, text="Start Date:").grid(row=1, column=0, sticky="w", pady=5)
        tk.Entry(filters, textvariable=self.filter_start_date, width=14).grid(row=1, column=1, padx=4, sticky="w")
        tk.Label(filters, text="End Date:").grid(row=1, column=2, sticky="w")
        tk.Entry(filters, textvariable=self.filter_end_date, width=14).grid(row=1, column=3, padx=4, sticky="w")
        tk.Label(filters, text="Sort:").grid(row=1, column=4, sticky="w")
        self.sort_combo = ttk.Combobox(filters, textvariable=self.sort_choice, values=["Date newest", "Date oldest", "Patient A-Z", "Total high-low"], width=28, state="readonly")
        self.sort_combo.grid(row=1, column=5, padx=4)
        tk.Button(filters, text="Apply Filters", command=self.apply_review_filters).grid(row=1, column=6, padx=4)
        tk.Button(filters, text="Clear", command=self.clear_review_filters).grid(row=1, column=7, padx=4)

        table_frame = tk.Frame(tab3)
        table_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.review_tree = ttk.Treeview(table_frame, columns=(), show="headings")
        yscroll = tk.Scrollbar(table_frame, orient="vertical", command=self.review_tree.yview)
        xscroll = tk.Scrollbar(table_frame, orient="horizontal", command=self.review_tree.xview)
        self.review_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.review_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        self.review_status = tk.Label(tab3, text="No perfect match file loaded.", anchor="w")
        self.review_status.pack(fill="x", padx=10, pady=(0, 5))

        log_frame = tk.LabelFrame(root, text="System Log", padx=5, pady=5); log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_text = tk.Text(log_frame, height=12, state="disabled", font=("Consolas", 9)); self.log_text.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview); self.log_text.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y")

        if abr_dir or sot_dir or out_dir:
            self.log(f"Prefill — Abronal: {self.abr_path.get()}")
            self.log(f"Prefill — SoT: {self.sot_path.get()}")
            self.log(f"Prefill — Output: {self.out_path.get()}")
        if self._auto_run and self.abr_path.get() and self.sot_path.get() and self.out_path.get():
            self.root.after(400, self.run_main)

    def log(self, msg): self.root.after(0, self._append_log, msg)
    def _append_log(self, msg): self.log_text.configure(state="normal"); self.log_text.insert("end", msg + "\n"); self.log_text.see("end"); self.log_text.configure(state="disabled")
    def _clear_log(self): self.log_text.configure(state="normal"); self.log_text.delete("1.0", "end"); self.log_text.configure(state="disabled")

    def load_review_file(self, path=None):
        path = path or self.review_file_path.get()
        if not path:
            messagebox.showerror("Error", "Select a Perfect_Matches.xlsx file first.")
            return
        try:
            xl = pd.ExcelFile(path)
            frames = []
            for sheet_name in xl.sheet_names:
                df = pd.read_excel(xl, sheet_name)
                if df.empty:
                    continue
                df['Abronal Source'] = sheet_name
                frames.append(df)
            if not frames:
                messagebox.showwarning("No Data", "No perfect-match rows were found in this workbook.")
                return
            self.perfect_df = pd.concat(frames, ignore_index=True)
            self.perfect_df['SoT Date Parsed'] = pd.to_datetime(self.perfect_df.get('SoT Date'), errors='coerce')
            self.perfect_df['Abronal Date Parsed'] = pd.to_datetime(self.perfect_df.get('Abronal Date'), errors='coerce')
            if 'Category' not in self.perfect_df.columns:
                self.perfect_df['Category'] = self.perfect_df['Service'].map(DEFAULT_CATEGORIES).fillna('Other')
            else:
                self.perfect_df['Category'] = self.perfect_df['Category'].fillna(self.perfect_df['Service'].map(DEFAULT_CATEGORIES)).fillna('Other')
            self.review_file_path.set(path)
            self.refresh_review_filter_values()
            self.apply_review_filters()
            self.notebook.select(2)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load perfect matches:\n{e}")

    def refresh_review_filter_values(self):
        if self.perfect_df.empty:
            return
        sources = ["All"] + sorted(self.perfect_df['Abronal Source'].dropna().astype(str).unique().tolist())
        categories = ["All"] + sorted(self.perfect_df['Category'].dropna().astype(str).unique().tolist())
        services = ["All"] + sorted(self.perfect_df['Service'].dropna().astype(str).unique().tolist())
        self.source_combo.configure(values=sources)
        self.category_combo.configure(values=categories)
        self.service_combo.configure(values=services)
        self.filter_source.set("All")
        self.filter_category.set("All")
        self.filter_service.set("All")

    def clear_review_filters(self):
        self.filter_source.set("All")
        self.filter_category.set("All")
        self.filter_service.set("All")
        self.filter_start_date.set("")
        self.filter_end_date.set("")
        self.sort_choice.set("Date newest")
        self.apply_review_filters()

    def apply_review_filters(self):
        if self.perfect_df.empty:
            self.review_status.config(text="No perfect match file loaded.")
            return
        df = self.perfect_df.copy()
        if self.filter_source.get() != "All":
            df = df[df['Abronal Source'].astype(str) == self.filter_source.get()]
        if self.filter_category.get() != "All":
            df = df[df['Category'].astype(str) == self.filter_category.get()]
        if self.filter_service.get() != "All":
            df = df[df['Service'].astype(str) == self.filter_service.get()]

        if self.filter_start_date.get().strip():
            start = pd.to_datetime(self.filter_start_date.get().strip(), errors='coerce')
            if pd.notna(start):
                df = df[df['SoT Date Parsed'] >= start]
        if self.filter_end_date.get().strip():
            end = pd.to_datetime(self.filter_end_date.get().strip(), errors='coerce')
            if pd.notna(end):
                df = df[df['SoT Date Parsed'] <= end]

        summary_df = self.build_category_summary(df)
        sort_map = {
            "Date newest": (["Date", "Patient Name", "Abronal Source"], [False, True, True]),
            "Date oldest": (["Date", "Patient Name", "Abronal Source"], [True, True, True]),
            "Patient A-Z": (["Patient Name", "Date", "Abronal Source"], [True, False, True]),
            "Total high-low": (["TOTAL", "Date", "Patient Name"], [False, False, True]),
        }
        cols, ascending = sort_map.get(self.sort_choice.get(), sort_map["Date newest"])
        if not summary_df.empty:
            summary_df = summary_df.sort_values(cols, ascending=ascending, na_position="last")
        self.populate_review_table(summary_df)
        total_amount = df['Amount'].sum() if 'Amount' in df.columns else 0
        self.review_status.config(text=f"Showing {len(summary_df)} patient/date summaries from {len(df)} perfect-match rows. Total amount: {total_amount:,.2f}")

    def build_category_summary(self, df):
        if df.empty:
            return pd.DataFrame(columns=["Date", "Abronal Source", "Patient Name", "TOTAL"])
        work = df.copy()
        work['Date'] = work['SoT Date Parsed'].dt.date
        summary = work.pivot_table(
            index=["Date", "Abronal Source", "Patient Name"],
            columns="Category",
            values="Amount",
            aggfunc="sum",
            fill_value=0,
        ).reset_index()
        category_cols = [c for c in summary.columns if c not in ("Date", "Abronal Source", "Patient Name")]
        ordered_categories = [c for c in COLUMN_ORDER if c in category_cols]
        extras = sorted([c for c in category_cols if c not in COLUMN_ORDER])
        amount_cols = ordered_categories + extras
        summary["TOTAL"] = summary[amount_cols].sum(axis=1) if amount_cols else 0
        return summary[["Date", "Abronal Source", "Patient Name"] + amount_cols + ["TOTAL"]]

    def populate_review_table(self, df):
        self.review_tree.delete(*self.review_tree.get_children())
        columns = list(df.columns) if not df.empty else ["Date", "Abronal Source", "Patient Name", "TOTAL"]
        self.review_tree.configure(columns=columns)
        for col in columns:
            self.review_tree.heading(col, text=col)
            anchor = "e" if col not in ("Date", "Abronal Source", "Patient Name") else "w"
            width = 150 if col == "Patient Name" else 120
            self.review_tree.column(col, width=width, anchor=anchor, stretch=True)
        for _, row in df.iterrows():
            values = []
            for col in columns:
                value = row.get(col, "")
                if col == "Date" and pd.notna(value):
                    value = value.isoformat() if hasattr(value, "isoformat") else str(value)
                elif col not in ("Date", "Abronal Source", "Patient Name") and value != "":
                    value = f"{float(value):,.2f}"
                values.append(value)
            self.review_tree.insert("", "end", values=values)

    def review_services_before_save(self, services):
        """
        Only ask about services that are not already known.
        Known = DEFAULT_CATEGORIES + saved_service_categories.json.
        Confirmed choices (including edits) are saved for next runs.
        """
        known = known_category_map()
        new_services = [svc for svc in services if svc not in known]
        known_services = [svc for svc in services if svc in known]

        # Everything already remembered — skip the dialog entirely
        if not new_services:
            mapping = {svc: known[svc] for svc in services}
            self.log(
                f"  All {len(services)} services already have saved categories — skipping confirmation."
            )
            return mapping

        done_event = threading.Event()
        result = {'mapping': None}

        def show_dialog():
            win = tk.Toplevel(self.root)
            win.title("Review New Service Categories")
            win.geometry("720x560")
            win.transient(self.root)
            win.grab_set()

            tk.Label(
                win,
                text=(
                    f"{len(known_services)} service(s) already saved will be reused automatically.\n"
                    f"Please categorize {len(new_services)} NEW service(s) below."
                ),
                anchor="w",
                justify="left",
            ).pack(fill="x", padx=10, pady=(10, 5))

            container = tk.Frame(win)
            container.pack(fill="both", expand=True, padx=10, pady=5)
            canvas = tk.Canvas(container)
            scrollbar = tk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
            table = tk.Frame(canvas)
            table.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=table, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            tk.Label(table, text="#", width=5, relief=tk.RIDGE).grid(row=0, column=0, sticky="ew")
            tk.Label(table, text="Service", width=48, anchor="w", relief=tk.RIDGE).grid(row=0, column=1, sticky="ew")
            tk.Label(table, text="Category", width=24, relief=tk.RIDGE).grid(row=0, column=2, sticky="ew")

            category_values = sorted(set(known.values()) | set(DEFAULT_CATEGORIES.values()) | {"Other"})
            vars_by_service = {}
            for idx, service in enumerate(new_services, start=1):
                default_category = known.get(service, "Other")
                var = tk.StringVar(value=default_category)
                vars_by_service[service] = var
                values = category_values if default_category in category_values else category_values + [default_category]
                tk.Label(table, text=str(idx), width=5).grid(row=idx, column=0)
                tk.Label(table, text=service, width=48, anchor="w").grid(row=idx, column=1, sticky="w")
                ttk.Combobox(table, textvariable=var, values=values, width=22).grid(row=idx, column=2, padx=4, pady=1)

            button_bar = tk.Frame(win)
            button_bar.pack(fill="x", padx=10, pady=10)

            def confirm():
                mapping = {svc: known[svc] for svc in known_services}
                mapping.update({service: var.get() for service, var in vars_by_service.items()})
                result['mapping'] = mapping
                win.destroy()
                done_event.set()

            def cancel():
                result['mapping'] = None
                win.destroy()
                done_event.set()

            tk.Button(button_bar, text="Confirm and Remember", command=confirm, width=28, bg="#e1f5fe").pack(side=tk.LEFT)
            tk.Button(button_bar, text="Cancel", command=cancel, width=12).pack(side=tk.RIGHT)
            win.protocol("WM_DELETE_WINDOW", cancel)

        self.root.after(0, show_dialog)
        done_event.wait()
        return result['mapping']

    def run_main(self):
        abr, sot, out = self.abr_path.get(), self.sot_path.get(), self.out_path.get()
        if not abr or not sot or not out: messagebox.showerror("Error", "Fill all fields"); return
        self._clear_log(); self.run_btn.configure(state="disabled")
        label = self.date_label or infer_date_label(out, abr, sot)
        self.date_label = label
        threading.Thread(
            target=run_reconciliation,
            args=(abr, sot, out, self.log, self._on_done, self.review_services_before_save, label),
            daemon=True,
        ).start()

    def run_summary_only(self):
        pm, out = self.pm_file_path.get(), self.sum_out_path.get()
        if not pm or not out: messagebox.showerror("Error", "Select file and folder"); return
        self._clear_log(); run_summary_logic(pm, out, self.log); messagebox.showinfo("Done", "Summary generated.")

    def _on_done(self, success):
        self.root.after(0, lambda: self.run_btn.configure(state="normal"))
        if success:
            self.root.after(0, self.after_reconciliation_success)

    def after_reconciliation_success(self):
        out = self.out_path.get()
        pm_path = find_perfect_matches_file(out, self.date_label)
        audit_path = os.path.join(out, "Unmatched_Analysis.xlsx")
        audit_result = "UNKNOWN"
        try:
            audit_summary = pd.read_excel(audit_path, sheet_name="Audit_Summary")
            matches = audit_summary.loc[audit_summary['Check'] == 'Audit Result', 'Value']
            if not matches.empty:
                audit_result = str(matches.iloc[0])
        except Exception:
            pass

        if os.path.exists(pm_path):
            self.load_review_file(pm_path)

        if audit_result == "PASS":
            messagebox.showinfo("Complete", "Reconciliation complete. Audit passed. Perfect matches loaded for review.")
        else:
            messagebox.showwarning("Review Needed", f"Reconciliation complete, but audit result is {audit_result}. Check Audit_Exceptions before relying on the review tab.")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Transaction Reconciliation Tool v5")
    parser.add_argument("--abr", help="Abronal Excel folder")
    parser.add_argument("--sot", help="SoT Excel folder")
    parser.add_argument("--out", help="Analysis / output folder")
    parser.add_argument(
        "--date-label",
        help='Date range label for output names, e.g. "July 20 to July 22"',
    )
    parser.add_argument(
        "--auto-run",
        action="store_true",
        help="Start reconciliation automatically after the window opens",
    )
    args = parser.parse_args()

    root = tk.Tk()
    app = ReconciliationApp(
        root,
        abr_dir=args.abr,
        sot_dir=args.sot,
        out_dir=args.out,
        auto_run=args.auto_run,
        date_label=args.date_label,
    )
    root.mainloop()
