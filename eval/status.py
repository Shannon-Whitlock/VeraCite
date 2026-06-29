#!/usr/bin/env python3
"""Show adjudication progress across the findings.csv manifest.

Run at the start of each session to see what's left, and at the end to verify
nothing was skipped. Output is a concise table plus a per-domain breakdown.

Usage:
    python3 eval/status.py [--csv PATH] [--domain D] [--severity S] [--todo]

    --domain    filter to one domain
    --severity  filter to ERROR / WARN / INFO
    --todo      print keys of unadjudicated rows (for Claude to work through)
    --session S print rows adjudicated in session S
"""

import argparse
import csv
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(ROOT, "eval", "findings.csv")


def load(csv_path, domain=None, severity=None):
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if domain:
        rows = [r for r in rows if r["domain"] == domain]
    if severity:
        rows = [r for r in rows if r["finding_severity"].upper() == severity.upper()]
    return rows


def is_adjudicated(row):
    return bool(row.get("verdict") or row.get("human_verified"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--domain")
    ap.add_argument("--severity")
    ap.add_argument("--todo", action="store_true",
                    help="print composite keys of unadjudicated rows")
    ap.add_argument("--session", help="show rows adjudicated in this session")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        print(f"Not found: {args.csv} — run build_manifest.py first", file=sys.stderr)
        sys.exit(1)

    rows = load(args.csv, args.domain, args.severity)
    total = len(rows)
    done = sum(1 for r in rows if is_adjudicated(r))
    todo = total - done
    tp = sum(1 for r in rows if r.get("verdict") == "TP")
    fp = sum(1 for r in rows if r.get("verdict") == "FP")
    na = sum(1 for r in rows if r.get("verdict") == "NA")

    print(f"\n{'='*60}")
    print(f"  VeraCite evaluation progress")
    if args.domain:
        print(f"  Domain:   {args.domain}")
    if args.severity:
        print(f"  Severity: {args.severity}")
    print(f"{'='*60}")
    print(f"  Total findings : {total}")
    print(f"  Adjudicated    : {done}  ({100*done//total if total else 0}%)")
    print(f"  Remaining      : {todo}")
    print(f"  TP / FP / NA   : {tp} / {fp} / {na}")
    if done:
        prec = tp / (tp + fp) if (tp + fp) else None
        print(f"  Precision      : {prec:.1%}" if prec is not None else "  Precision      : n/a")
    print()

    # Per-domain breakdown
    by_domain = defaultdict(lambda: {"total": 0, "done": 0, "tp": 0, "fp": 0})
    for r in rows:
        d = r["domain"]
        by_domain[d]["total"] += 1
        if is_adjudicated(r):
            by_domain[d]["done"] += 1
        if r.get("verdict") == "TP":
            by_domain[d]["tp"] += 1
        if r.get("verdict") == "FP":
            by_domain[d]["fp"] += 1

    print(f"  {'domain':<18}  {'total':>5}  {'done':>5}  {'%':>4}  {'TP':>4}  {'FP':>4}")
    print(f"  {'-'*18}  {'-'*5}  {'-'*5}  {'-'*4}  {'-'*4}  {'-'*4}")
    for d in sorted(by_domain):
        s = by_domain[d]
        pct = 100 * s["done"] // s["total"] if s["total"] else 0
        print(f"  {d:<18}  {s['total']:>5}  {s['done']:>5}  {pct:>3}%  {s['tp']:>4}  {s['fp']:>4}")

    # Per-severity breakdown
    print()
    by_sev = defaultdict(lambda: {"total": 0, "done": 0, "tp": 0, "fp": 0})
    for r in rows:
        s = r["finding_severity"]
        by_sev[s]["total"] += 1
        if is_adjudicated(r):
            by_sev[s]["done"] += 1
        if r.get("verdict") == "TP":
            by_sev[s]["tp"] += 1
        if r.get("verdict") == "FP":
            by_sev[s]["fp"] += 1

    print(f"  {'severity':<10}  {'total':>5}  {'done':>5}  {'%':>4}  {'TP':>4}  {'FP':>4}")
    print(f"  {'-'*10}  {'-'*5}  {'-'*5}  {'-'*4}  {'-'*4}  {'-'*4}")
    for sev in ("ERROR", "WARN", "INFO"):
        if sev not in by_sev:
            continue
        s = by_sev[sev]
        pct = 100 * s["done"] // s["total"] if s["total"] else 0
        print(f"  {sev:<10}  {s['total']:>5}  {s['done']:>5}  {pct:>3}%  {s['tp']:>4}  {s['fp']:>4}")

    print()

    # Per-category breakdown (top 15 by count)
    by_cat = defaultdict(lambda: {"total": 0, "done": 0, "tp": 0, "fp": 0})
    for r in rows:
        c = r["finding_category"]
        by_cat[c]["total"] += 1
        if is_adjudicated(r):
            by_cat[c]["done"] += 1
        if r.get("verdict") == "TP":
            by_cat[c]["tp"] += 1
        if r.get("verdict") == "FP":
            by_cat[c]["fp"] += 1

    print(f"  {'category':<30}  {'total':>5}  {'done':>5}  {'%':>4}  {'TP':>4}  {'FP':>4}")
    print(f"  {'-'*30}  {'-'*5}  {'-'*5}  {'-'*4}  {'-'*4}  {'-'*4}")
    for c, s in sorted(by_cat.items(), key=lambda x: -x[1]["total"])[:20]:
        pct = 100 * s["done"] // s["total"] if s["total"] else 0
        print(f"  {c:<30}  {s['total']:>5}  {s['done']:>5}  {pct:>3}%  {s['tp']:>4}  {s['fp']:>4}")

    print(f"\n{'='*60}\n")

    if args.session:
        session_rows = [r for r in rows if r.get("session_id") == args.session]
        print(f"Session {args.session}: {len(session_rows)} rows adjudicated")
        for r in session_rows:
            print(f"  [{r['verdict']:2s}] {r['domain']}/{r['paper_id']} "
                  f"{r['entry_key']} #{r['finding_index']} {r['finding_category']}")
        print()

    if args.todo:
        unadj = [r for r in rows if not is_adjudicated(r)]
        print(f"Unadjudicated ({len(unadj)}):")
        for r in unadj[:100]:
            print(f"  {r['domain']}/{r['paper_id']}  {r['entry_key']}  "
                  f"#{r['finding_index']}  {r['finding_severity']}  {r['finding_category']}")
        if len(unadj) > 100:
            print(f"  ... and {len(unadj)-100} more")


if __name__ == "__main__":
    main()
