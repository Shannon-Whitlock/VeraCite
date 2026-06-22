#!/usr/bin/env python3
"""Regenerate biblatex_datamodel.json from the installed biblatex datamodel.

The field-validity check derives legal fields per entry type from biblatex's
formal datamodel (blx-dm.def) rather than a hand-kept list, so it stays complete
and standard. Run this when biblatex updates:

    ./tools/gen_datamodel.py [path/to/blx-dm.def]

With no argument it searches the common TeX Live location. The result is written
next to check_bib.py as biblatex_datamodel.json.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "veracite", "data",
                   "biblatex_datamodel.json")

# Legacy BibTeX entry types mapped to their biblatex datamodel equivalents.
ALIASES = {
    "phdthesis": "thesis", "mastersthesis": "thesis", "mathesis": "thesis",
    "conference": "inproceedings", "techreport": "report",
    "www": "online", "electronic": "online",
}


def find_datamodel(argv):
    if len(argv) > 1:
        return argv[1]
    try:
        root = subprocess.check_output(["kpsewhich", "blx-dm.def"], text=True).strip()
        if root:
            return root
    except (OSError, subprocess.CalledProcessError):
        pass
    for base in ("/usr/share/texlive", "/opt/texlive", "/usr/local/texlive"):
        for dirpath, _dirs, names in os.walk(base):
            if "blx-dm.def" in names:
                return os.path.join(dirpath, "blx-dm.def")
    sys.exit("blx-dm.def not found; pass its path as an argument")


def _balanced(text, open_idx):
    """Return the substring inside the brace group starting at `open_idx` (which
    must point at '{'), tracking nesting. Used to carve constraint blocks."""
    depth, i = 0, open_idx
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i
        i += 1
    return text[open_idx + 1:], len(text)


def parse_mandatory(text):
    """Per-entry-type mandatory-field constraints from biblatex's datamodel
    (\\DeclareDatamodelConstraints[...]{ \\constraint[type=mandatory]{...} }). Each
    type maps to a list of *slots*; a slot is a list of acceptable field names,
    satisfied if the entry supplies ANY one (a \\constraintfieldsor groups
    alternatives, e.g. author OR editor). This is biber's own rule, so reading it
    here keeps required-field validation aligned with biber rather than hand-kept."""
    out = {}
    for m in re.finditer(r"\\DeclareDatamodelConstraints(\[[^\]]*\])?\s*\{", text):
        opt = m.group(1)
        if not opt:
            continue   # the global data-type constraints (isbn/issn/...), not per-type
        types = [t.strip().lower() for t in re.split(r"[,\s]+", opt.strip("[]")) if t.strip()]
        body, _end = _balanced(text, m.end() - 1)
        cm = re.search(r"\\constraint\[type=mandatory\]\s*\{", body)
        if not cm:
            continue
        cbody, _ = _balanced(body, cm.end() - 1)
        slots = []
        i = 0
        while i < len(cbody):
            orm = re.compile(r"\\constraintfieldsor\s*\{").search(cbody, i)
            fm = re.compile(r"\\constraintfield\s*\{").search(cbody, i)
            if orm and (not fm or orm.start() < fm.start()):
                inner, end = _balanced(cbody, orm.end() - 1)
                alts = re.findall(r"\\constraintfield\s*\{([^}]*)\}", inner)
                slots.append([a.strip().lower() for a in alts])
                i = end + 1
            elif fm:
                inner, end = _balanced(cbody, fm.end() - 1)
                slots.append([inner.strip().lower()])
                i = end + 1
            else:
                break
        for t in types:
            out[t] = slots
    return out


def main(argv):
    path = find_datamodel(argv)
    text = "\n".join(l for l in open(path).read().splitlines()
                     if not l.lstrip().startswith("%"))
    entrytypes = set()
    for opt, body in re.findall(
            r"\\DeclareDatamodelEntrytypes(\[[^\]]*\])?\{([^}]*)\}", text, re.S):
        entrytypes |= {t.strip().lower() for t in re.split(r"[,\s]+", body) if t.strip()}
    entrytypes |= set(ALIASES)   # legacy BibTeX types are valid too

    calls = re.findall(r"\\DeclareDatamodelEntryfields(\[[^\]]*\])?\{([^}]*)\}",
                       text, re.S)
    universal, bytype = set(), {}
    for opt, fields in calls:
        flds = {f.strip().lower() for f in re.split(r"[,\s]+", fields) if f.strip()}
        if not opt:
            universal |= flds
        else:
            for t in re.split(r"[,\s]+", opt.strip("[]")):
                if t.strip():
                    bytype.setdefault(t.strip().lower(), set()).update(flds)

    # The date-type fields (date, urldate, eventdate, origdate) are declared with
    # \DeclareDatamodelFields[...datatype=date...], not \DeclareDatamodelEntryfields,
    # and biblatex auto-associates them with every entry type (see the "auto-create
    # for all date fields" logic in blx-dm.def). Parsing only Entryfields would miss
    # them and wrongly flag a standard 'date'/'urldate' as invalid, so add them to
    # the universal set explicitly.
    for opt, fields in re.findall(
            r"\\DeclareDatamodelFields(\[[^\]]*\])?\{([^}]*)\}", text, re.S):
        if opt and "datatype=date" in opt.replace(" ", ""):
            universal |= {f.strip().lower()
                          for f in re.split(r"[,\s]+", fields) if f.strip()}
    specific = {t: sorted(f - universal) for t, f in sorted(bytype.items())
                if f - universal}
    mandatory = parse_mandatory(text)
    data = {
        "_source": f"biblatex datamodel ({os.path.basename(path)}); "
                   f"regenerate with tools/gen_datamodel.py",
        "aliases": ALIASES,
        "entrytypes": sorted(entrytypes),
        "universal": sorted(universal),
        "bytype": specific,
        "mandatory": mandatory,
    }
    json.dump(data, open(OUT, "w"), indent=1, sort_keys=False)
    print(f"wrote {OUT}: {len(entrytypes)} entry types, {len(universal)} universal "
          f"fields, {len(specific)} type-specific")


if __name__ == "__main__":
    main(sys.argv)
