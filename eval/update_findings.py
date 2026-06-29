#!/usr/bin/env python3
"""Upsert adjudication results into findings.csv without creating duplicates.

Claude Code and human reviewers call this script to write adjudication results
back to the CSV. It identifies each finding by its composite key and updates
ONLY the adjudication columns, leaving all metadata columns untouched.

Usage (from Python, by Claude or a script):

    python3 eval/update_findings.py --updates updates.json

Where updates.json is a list of dicts, each with the key fields plus any
adjudication columns to set:

    [
      {
        "domain": "atom-ph",
        "paper_id": "arXiv-2207.10215v1",
        "report_file": "arXiv-2207.10215v1.report.json",
        "entry_key": "Gregory2021",
        "finding_index": "0",
        "verdict": "TP",
        "diagnosis_correct": "yes",
        "suggestion_verdict": "correct",
        "adjudicator_note": "Hutson is a co-author, confirmed on Crossref.",
        "claude_verified": "2026-06-28",
        "session_id": "2026-06-28-1",
        "source_checked": "https://doi.org/10.1038/s41567-021-01328-7"
      },
      ...
    ]

Or inline for a single finding:

    python3 eval/update_findings.py \\
        --domain atom-ph \\
        --paper arXiv-2207.10215v1 \\
        --report arXiv-2207.10215v1.report.json \\
        --key Gregory2021 \\
        --index 0 \\
        --verdict TP \\
        --diagnosis yes \\
        --suggestion correct \\
        --note "Hutson confirmed on Crossref." \\
        --claude 2026-06-28 \\
        --session 2026-06-28-1 \\
        --source "https://doi.org/10.1038/s41567-021-01328-7"
"""

import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(ROOT, "eval", "findings.csv")

ADJUDICATION_COLS = {
    "verdict", "diagnosis_correct", "suggestion_verdict",
    "correct_value", "adjudicator_note",
    "claude_verified", "human_verified", "session_id", "source_checked",
}

KEY_COLS = ["domain", "paper_id", "report_file", "entry_key", "finding_index"]


def _row_key(row):
    return tuple(row.get(c, "") for c in KEY_COLS)


def upsert(csv_path, updates, force_blank=False):
    """Apply a list of update dicts to the CSV. Returns (matched, unmatched).

    By default only non-empty values are written, so a partial update cannot
    accidentally erase an existing adjudication. Pass force_blank=True to
    allow writing empty strings (e.g. to reset a row).
    """
    if not os.path.isfile(csv_path):
        print(f"error: {csv_path} not found — run build_manifest.py first", file=sys.stderr)
        sys.exit(1)

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        rows = list(reader)

    index = {_row_key(r): i for i, r in enumerate(rows)}

    matched = 0
    unmatched = []
    for upd in updates:
        k = _row_key(upd)
        if k not in index:
            unmatched.append(upd)
            continue
        row = rows[index[k]]
        for col in ADJUDICATION_COLS:
            if col in upd and (upd[col] != "" or force_blank):
                row[col] = upd[col]
        matched += 1

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Updated {matched} rows. Unmatched: {len(unmatched)}")
    for u in unmatched:
        print(f"  NOT FOUND: {_row_key(u)}", file=sys.stderr)
    return matched, unmatched


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--force-blank", action="store_true",
                    help="allow writing empty strings (to reset adjudication fields)")
    # Batch mode
    ap.add_argument("--updates", help="JSON file with list of update dicts")
    # Single-finding inline mode
    ap.add_argument("--domain")
    ap.add_argument("--paper", dest="paper_id")
    ap.add_argument("--report", dest="report_file")
    ap.add_argument("--key", dest="entry_key")
    ap.add_argument("--index", dest="finding_index", default="0")
    ap.add_argument("--verdict")
    ap.add_argument("--diagnosis", dest="diagnosis_correct")
    ap.add_argument("--suggestion", dest="suggestion_verdict")
    ap.add_argument("--correct-value", dest="correct_value")
    ap.add_argument("--note", dest="adjudicator_note")
    ap.add_argument("--claude", dest="claude_verified")
    ap.add_argument("--human", dest="human_verified")
    ap.add_argument("--session", dest="session_id")
    ap.add_argument("--source", dest="source_checked")
    args = ap.parse_args()

    if args.updates:
        with open(args.updates, encoding="utf-8") as fh:
            updates = json.load(fh)
    elif args.domain and args.paper_id and args.entry_key:
        upd = {c: getattr(args, c.replace("-", "_"), "") or ""
               for c in KEY_COLS + list(ADJUDICATION_COLS)}
        upd["paper_id"] = args.paper_id
        upd["report_file"] = args.report_file or ""
        updates = [upd]
    else:
        ap.print_help()
        sys.exit(1)

    upsert(args.csv, updates, force_blank=args.force_blank)


if __name__ == "__main__":
    main()
