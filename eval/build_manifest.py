#!/usr/bin/env python3
"""Build or refresh the evaluation manifest from all *.report.json files.

The manifest (findings.csv) is the single source of truth for what needs
adjudicating. Each row is one finding, identified by a stable composite key:
  (domain, paper_id, entry_key, finding_index, finding_category)

finding_index is the 0-based position of the finding in the entry's issues
list in the NDJSON. It exists only to break ties when the same category/line
appears twice on one entry (e.g. two metadata_mismatch findings at the same
line number). It is not meaningful on its own.

Run this once to create findings.csv, then again to add newly-run papers.
Existing rows are preserved exactly; only genuinely new findings are appended.

Usage:
    python3 eval/build_manifest.py [--examples PATH] [--out PATH] [--domain D]

    --examples  root of the examples/ tree  (default: examples/)
    --out       output CSV path             (default: eval/findings.csv)
    --domain    only process this domain    (default: all)
"""

import argparse
import csv
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _oneline(s):
    """Collapse whitespace (including embedded newlines) to a single space.
    Keeps CSV cells single-line so any CSV reader handles them correctly."""
    return re.sub(r'\s+', ' ', s).strip()


def _extract_entry(bib_text, start_line):
    """Extract one raw BibTeX entry starting at start_line (1-based) by
    scanning forward for the opening '@' then consuming until braces balance."""
    lines = bib_text.split('\n')
    start = start_line - 1
    # The recorded line is the entry's first field, not the @type{key line —
    # walk back up to 3 lines to find the opening '@'.
    for i in range(max(0, start - 3), min(start + 2, len(lines))):
        if re.match(r'\s*@\w+\s*\{', lines[i]):
            start = i
            break
    depth, result = 0, []
    for line in lines[start:]:
        result.append(line)
        depth += line.count('{') - line.count('}')
        if depth <= 0 and result:
            break
    return '\n'.join(result)


def _find_bib(paper_dir, report_file, paper_id):
    """Return the content of the bib file for this report, or ''.

    paper_dir is always the correct source folder (arXiv ID without suffix).
    The bib stem is whatever follows the paper_id in the report filename:
      arXiv-2309.16000v2.ComagLitt.report.json  -> ComagLitt -> ComagLitt.bib
      arXiv-2207.10215v1.report.json            -> (none)   -> sole .bib
    """
    # Strip the folder_id (arXiv ID without bib suffix) from the report filename
    # to get the bib stem.  paper_id may include the stem already, so use the
    # folder name (which is always just the arXiv ID) as the prefix to strip.
    folder_name = os.path.basename(paper_dir)
    after = report_file[len(folder_name):].lstrip('.')   # "ComagLitt.report.json" or "report.json"
    stem = after.replace('.report.json', '')              # "ComagLitt" or ""

    candidate = os.path.join(paper_dir, stem + '.bib') if stem else None
    if candidate and os.path.isfile(candidate):
        bib_path = candidate
    else:
        bibs = glob.glob(os.path.join(paper_dir, '*.bib'))
        bib_path = bibs[0] if len(bibs) == 1 else None
    if not bib_path:
        return ''
    try:
        with open(bib_path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return ''

FIELDS = [
    # --- identity (composite primary key) ---
    "domain",           # arXiv category folder, e.g. atom-ph
    "paper_id",         # folder name, e.g. arXiv-2207.10215v1
    "report_file",      # basename of the .report.json, e.g. arXiv-2207.10215v1.report.json
    "entry_key",        # BibTeX citation key
    "finding_index",    # 0-based position in the entry's issues list (tie-breaker)
    # --- finding metadata (read from NDJSON, never changed) ---
    "finding_category", # e.g. metadata_mismatch
    "finding_severity", # ERROR / WARN / INFO
    "finding_group",    # syntax / semantic / context
    "finding_layer",    # static / record / retract / llm / …
    "finding_line",     # bib line number (0 = not applicable)
    "finding_message",  # the full human message
    "suggested_field",  # field the fix applies to, or ""
    "suggested_from",   # current value, or ""
    "suggested_to",     # proposed value, or ""
    # --- entry context (from the record, not the finding) ---
    "entry_type",       # @article, @book, @misc, …
    "entry_line",       # line of the entry in the bib
    "entry_status",     # VERIFIED / UNVERIFIED / MISMATCH / ""
    "entry_confidence", # 0.0–1.0, or ""
    "entry_doi",        # DOI if present
    "entry_arxiv",      # arXiv id if present
    "verify_url",       # the verify: link (DOI or arXiv URL)
    "bib_entry",        # raw BibTeX source text for this entry
    # --- adjudication (filled in by Claude or a human) ---
    "verdict",          # TP / FP / NA  (is there a real problem?)
    "diagnosis_correct",# yes / partial / no  (only for TP)
    "suggestion_verdict",# correct / incorrect / none / unverifiable
    "correct_value",    # what the right value is (when suggestion is wrong)
    "adjudicator_note", # free text
    # --- provenance ---
    "claude_verified",  # ISO date when Claude last adjudicated this row
    "human_verified",   # ISO date when a human last checked this row
    "session_id",       # e.g. 2026-06-28-1
    "source_checked",   # URL or source used (Crossref page, arXiv, etc.)
]

BLANK_ADJUDICATION = {
    "verdict": "",
    "diagnosis_correct": "",
    "suggestion_verdict": "",
    "correct_value": "",
    "adjudicator_note": "",
    "claude_verified": "",
    "human_verified": "",
    "session_id": "",
    "source_checked": "",
}


def _row_key(row):
    return (row["domain"], row["paper_id"], row["report_file"],
            row["entry_key"], row["finding_index"])


def load_existing(path):
    """Load existing CSV into {key: row_dict}. Returns {} if file absent."""
    if not os.path.isfile(path):
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = {}
        for row in reader:
            k = _row_key(row)
            rows[k] = row
    return rows


def scan_reports(examples_root, domain_filter=None):
    """Yield one dict per finding across all *.report.json files."""
    pattern = os.path.join(examples_root, "**", "*.report.json")
    for report_path in sorted(glob.glob(pattern, recursive=True)):
        parts = report_path.replace(examples_root, "").lstrip(os.sep).split(os.sep)
        if len(parts) < 2:
            continue
        domain = parts[0]
        if domain_filter and domain != domain_filter:
            continue
        report_file = parts[-1]
        # paper_id: the report filename without .report.json
        paper_id = report_file.replace(".report.json", "")

        try:
            with open(report_path, encoding="utf-8") as fh:
                records = [json.loads(l) for l in fh if l.strip()]
        except (OSError, json.JSONDecodeError) as e:
            print(f"  warning: skipping {report_path}: {e}", file=sys.stderr)
            continue

        # The source folder always matches the arXiv ID portion of paper_id —
        # strip any trailing bib-stem suffix (e.g. arXiv-2309.16000v2.ComagLitt
        # -> arXiv-2309.16000v2) by taking only the part that matches the
        # arXiv ID pattern.  Falls back to paper_id if it doesn't match.
        _arxiv_id_re = re.compile(r'^(arXiv-[\d]+\.\d+v\d+)', re.I)
        _m = _arxiv_id_re.match(paper_id)
        folder_id = _m.group(1) if _m else paper_id
        paper_dir = os.path.join(examples_root, domain, folder_id)
        bib_text = _find_bib(paper_dir, report_file, paper_id)

        for rec in records:
            key = rec.get("key", "")
            if not key or key.startswith("<"):
                continue
            ids = rec.get("identifiers") or {}
            entry_line = rec.get("line") or 0
            raw_entry = _extract_entry(bib_text, entry_line) if bib_text and entry_line else ""
            base = {
                "domain": domain,
                "paper_id": paper_id,
                "report_file": report_file,
                "entry_key": key,
                "entry_type": rec.get("entry_type") or "",
                "entry_line": str(entry_line),
                "entry_status": rec.get("status") or "",
                "entry_confidence": str(rec.get("confidence") if rec.get("confidence") is not None else ""),
                "entry_doi": ids.get("doi") or "",
                "entry_arxiv": ids.get("arxiv") or "",
                "verify_url": rec.get("verify") or "",
                "bib_entry": raw_entry,
            }
            for idx, issue in enumerate(rec.get("issues") or []):
                sug = issue.get("suggested") or {}
                # Seed adjudication from _eval if already set in the NDJSON
                eval_ = issue.get("_eval") or {}
                adj = {k: eval_.get(k) or "" for k in BLANK_ADJUDICATION}
                row = {
                    **base,
                    "finding_index": str(idx),
                    "finding_category": issue.get("category") or "",
                    "finding_severity": issue.get("severity") or "",
                    "finding_group": issue.get("group") or "",
                    "finding_layer": issue.get("layer") or "",
                    "finding_line": str(issue.get("line") or ""),
                    "finding_message": _oneline(issue.get("message") or ""),
                    "suggested_field": sug.get("field") or "",
                    "suggested_from": _oneline(str(sug.get("from") or "")),
                    "suggested_to": _oneline(str(sug.get("to") or "")),
                    **adj,
                }
                yield row


def build(examples_root, out_path, domain_filter=None):
    existing = load_existing(out_path)
    print(f"Existing rows in {out_path}: {len(existing)}")

    new_rows = []
    seen_keys = set()
    all_from_scan = list(scan_reports(examples_root, domain_filter))
    print(f"Findings in reports: {len(all_from_scan)}")

    for row in all_from_scan:
        k = _row_key(row)
        seen_keys.add(k)
        if k not in existing:
            new_rows.append(row)

    print(f"New findings to add: {len(new_rows)}")

    # Merge: existing rows first (in original order), then new ones appended.
    all_rows = list(existing.values()) + new_rows

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Written {len(all_rows)} rows to {out_path}")
    return len(new_rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--examples", default=os.path.join(ROOT, "examples"),
                    help="root of the examples/ tree")
    ap.add_argument("--out", default=os.path.join(ROOT, "eval", "findings.csv"),
                    help="output CSV path")
    ap.add_argument("--domain", default=None,
                    help="only process this domain folder (e.g. atom-ph)")
    args = ap.parse_args()
    added = build(args.examples, args.out, args.domain)
    sys.exit(0 if added >= 0 else 1)


if __name__ == "__main__":
    main()
