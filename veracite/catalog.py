"""The rule catalog: a publisher's audit sheet, derived from the source of truth.

`veracite --list-rules` prints, for every finding category VeraCite can emit, its
default severity, its group (syntax/semantic/context), what (if anything)
supersedes it, and a one-line description. A publisher reads this table to decide
where their house standard disagrees, then encodes the disagreements in a
settings file's `severity` block -- no code change needed.

The catalog is *introspected*, never hand-maintained: the set of categories is
scanned from the `category="..."` literals in the package source, and the four
columns are joined from the existing tables (DEFAULT_SETTINGS['severity'],
report.CATEGORY_GROUP, report.SUPERSEDES, report.CATEGORY_DOC). So it cannot drift
from what the code actually emits -- and `tests/test_catalog.py` asserts exactly
that, which is what stops the table from ever going stale.
"""

import json
import os
import re

from .config import DEFAULT_SETTINGS
from .report import (CATEGORY_DOC, CATEGORY_GROUP, finding_group,
                     resolve_severity, SEVERITY_NAMES, SUPERSEDES)

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_CATEGORY_RE = re.compile(r'category="([a-z_]+)"')
_DEF_RE = re.compile(r'^(\s*)(?:async\s+)?def (\w+)\s*\(')

# Categories deliberately NOT pinned in DEFAULT_SETTINGS['severity']: their checks
# emit MORE THAN ONE severity (author_format: a note for ALL-CAPS surnames, a
# warning for an 'and' glued to a name), and pinning a category flattens all its
# findings to one level. Listed here -- the single source of truth, referenced by
# config.py's comment and asserted by tests/test_catalog.py -- so a new
# mixed-severity category is a conscious choice, not an accidental gap.
INTENTIONALLY_UNPINNED = frozenset({"author_format"})


def category_sources():
    """Map each emittable category to the source locations that emit it.

    A single static scan of the package: it walks every `category="..."` literal
    and attributes it to the innermost enclosing `def`, recording (file, function,
    line). The catalog's index is built from this, and `emitted_categories()` is
    just its key set -- so there is ONE scanner, and the index can never disagree
    with the category list it indexes.

    The relationship is genuinely many-to-many (one rule function can emit several
    categories; one category, e.g. 'style', is emitted by many functions), so each
    category maps to a *list* of sources, sorted for stable output."""
    sources = {}
    for name in sorted(os.listdir(_PKG_DIR)):
        if not name.endswith(".py") or name == "catalog.py":
            continue  # catalog.py's own _CATEGORY_RE literal is not an emit site
        path = os.path.join(_PKG_DIR, name)
        with open(path, encoding="utf-8") as fh:
            # Track the function enclosing each line by indentation: a `def` owns
            # every following line more-indented than it, until a sibling/dedent.
            stack = []  # (indent, function name)
            for lineno, line in enumerate(fh, 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                m = _DEF_RE.match(line)
                if m:
                    while stack and stack[-1][0] >= indent:
                        stack.pop()
                    stack.append((indent, m.group(2)))
                else:
                    while stack and stack[-1][0] >= indent:
                        stack.pop()
                for cat in _CATEGORY_RE.findall(line):
                    fn = stack[-1][1] if stack else "<module>"
                    sources.setdefault(cat, set()).add((name, fn, lineno))
    return {c: sorted(locs) for c, locs in sources.items()}


def emitted_categories():
    """Every category any rule or layer can emit, scanned from the package source.

    This is the authoritative set: a static scan of the `category="..."` literals
    catches categories from the online layers and multi-category rules that
    introspecting the ENTRY_RULES/FILE_RULES registries alone would miss."""
    return set(category_sources())


def default_severity_label(category):
    """The default severity a category resolves to with no user override:
    'error'/'warning'/'note' for a pinned category, or 'mixed' for one that is
    deliberately unpinned (its checks emit several severities; see
    INTENTIONALLY_UNPINNED). Reads DEFAULT_SETTINGS['severity'] -- the same table
    resolve_severity() consults -- so it matches a real run's behaviour."""
    configured = DEFAULT_SETTINGS.get("severity", {}).get(category)
    if configured and str(configured).lower() in SEVERITY_NAMES:
        return str(configured).lower()
    if category in INTENTIONALLY_UNPINNED:
        return "mixed"
    return None  # an unexpected gap: caller/test surfaces it


def catalog():
    """The full catalog as a sorted list of dicts, one per emittable category.

    Each row also carries `sources`: the function(s) and file:line(s) that emit the
    category. This makes the catalog a faithful *index* into the detection logic --
    it cannot reproduce a check (the algorithm lives in the function body), but it
    points at exactly the code to read to see what a check does."""
    srcmap = category_sources()
    rows = []
    for cat in sorted(emitted_categories()):
        sup = SUPERSEDES.get(cat)
        rows.append({
            "category": cat,
            "default_severity": default_severity_label(cat),
            "group": finding_group(cat),
            "superseded_by": sup[1] if sup else None,
            "description": CATEGORY_DOC.get(cat, ""),
            "sources": [{"function": fn, "file": f, "line": ln}
                        for (f, fn, ln) in srcmap.get(cat, [])],
        })
    return rows


def _source_functions(row, limit=None):
    """The distinct function names that emit a category. Many-to-many is normal
    (e.g. 'style' is emitted by ten functions). For the table a `limit` keeps rows
    scannable, eliding the tail as '+N more'; the JSON `sources` lists them all,
    with file:line."""
    seen = []
    for s in row["sources"]:
        if s["function"] not in seen:
            seen.append(s["function"])
    if limit and len(seen) > limit:
        return ", ".join(seen[:limit]) + f" +{len(seen) - limit} more"
    return ", ".join(seen)


def _fmt_table(rows):
    cols = [("category", "category"), ("default_severity", "severity"),
            ("group", "group"), ("superseded_by", "superseded by"),
            ("rules", "rules (in source)"), ("description", "description")]
    def cell(r, key):
        if key == "rules":
            return _source_functions(r, limit=3) or "-"
        v = r.get(key)
        return "-" if v in (None, "") else str(v)
    widths = {k: max(len(hdr), *(len(cell(r, k)) for r in rows)) for k, hdr in cols}
    line = lambda vals: "  ".join(v.ljust(widths[k]) for (k, _), v in zip(cols, vals))
    out = [line([hdr for _, hdr in cols]),
           line(["-" * widths[k] for k, _ in cols])]
    out += [line([cell(r, k) for k, _ in cols]) for r in rows]
    return "\n".join(out)


def print_catalog(as_json=False, stream=None):
    """Print the catalog to `stream` (default stdout). The audit sheet a publisher
    reviews against their house standard; `as_json` for machine consumption."""
    import sys
    stream = stream or sys.stdout
    rows = catalog()
    if as_json:
        json.dump({"rules": rows}, stream, indent=2)
        stream.write("\n")
        return
    stream.write(_fmt_table(rows) + "\n")
    stream.write(
        f"\n{len(rows)} finding categories. The 'rules' column names the function(s) "
        "that emit each\ncategory -- read those in veracite/ for the exact logic "
        "('--list-rules json' gives\nfull file:line). Re-rank any category in a "
        "settings file's \"severity\" block, e.g.\n"
        '  {"severity": {"style": "warning", "title_case": "note"}}\n')
