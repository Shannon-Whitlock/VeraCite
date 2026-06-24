#!/usr/bin/python3
"""Show the VeraCite demo usage counter: per-day check counts and the total.

Reads the "YYYY-MM-DD<TAB>count" file that check.cgi maintains (counter.txt next to
it) and prints it as plain text. It exposes ONLY aggregate counts -- no IP, no
bibliography, nothing personal. It is still a public URL unless you restrict it; if
you would rather keep the numbers private, protect this one file with an .htaccess
Basic-Auth block (see web/README.md), or just rename it to something unguessable.

Add ?json to get the data as JSON instead of text.
"""

import json
import os
import sys
from urllib.parse import parse_qs

COUNTER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "counter.txt")


def read_counts():
    counts = {}
    try:
        with open(COUNTER_FILE, encoding="utf-8") as f:
            for line in f:
                if "\t" in line:
                    d, n = line.rstrip("\n").split("\t", 1)
                    try:
                        counts[d] = int(n)
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return counts


def main():
    counts = read_counts()
    total = sum(counts.values())
    want_json = "json" in parse_qs(os.environ.get("QUERY_STRING", ""))
    if want_json:
        sys.stdout.write("Content-Type: application/json; charset=utf-8\r\n\r\n")
        sys.stdout.write(json.dumps({"total": total, "by_day": counts}, sort_keys=True))
        return
    sys.stdout.write("Content-Type: text/plain; charset=utf-8\r\n\r\n")
    sys.stdout.write("VeraCite demo usage\n===================\n\n")
    for d in sorted(counts):
        sys.stdout.write(f"{d}  {counts[d]}\n")
    sys.stdout.write(f"\nTotal checks: {total}\n")
    sys.stdout.write(f"Days active:  {len(counts)}\n")


if __name__ == "__main__":
    main()
