"""Findings and report rendering -- the result schema shared by every layer.

A Finding has a Severity, a citation key (or "<file>"), a source line, a message,
the producing layer, and a category. Category drives per-category severity (see
resolve_severity) and prints as a stable rule code. The Report collects findings
and renders a colorized, human- and machine-readable report.
"""

import re
import sys
from dataclasses import dataclass
from enum import IntEnum

from .config import SETTINGS


def enable_ansi_colors():
    """On Windows, switch the console into virtual-terminal mode so the ANSI escape
    codes below render instead of printing literally. A no-op on POSIX and on
    Windows consoles that already support it (Windows Terminal, modern conhost).
    Returns True if color output should work."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # -11 == STD_OUTPUT_HANDLE; 0x0004 == ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Severity(IntEnum):
    ERROR = 0
    WARN = 1
    INFO = 2


# Per-severity presentation: short tag, ANSI color, and the summary label. Kept
# in one place so the look of the report is trivial to change.
SEVERITY_STYLE = {
    Severity.ERROR: ("ERROR", "\033[1;31m", "error"),    # bold red
    Severity.WARN:  ("WARN",  "\033[1;33m", "warning"),  # bold yellow
    Severity.INFO:  ("note",  "\033[2;37m", "note"),     # dim grey
}
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[1;32m"

DB_DISCLAIMER = (
    "VeraCite is only as accurate as the databases it queries (Crossref, arXiv, "
    "OpenAlex, Semantic Scholar). Record, retraction and correction/erratum "
    "findings reflect what those sources report and may be incomplete; verify "
    "any correction or erratum against the source of record before acting on it.")
LLM_DISCLAIMER = (
    "LLM ratings are based only on the abstract and citation context available "
    "to the model. They are not confirmation of relevance and must not be cited "
    "as such; treat them as a prompt for human review. They are useful mainly "
    "to flag obviously irrelevant, mismatched, or fabricated references.")


@dataclass
class Finding:
    severity: Severity
    key: str            # citation key, or "<file>" for file-wide findings
    line: int           # 1-based line in the .bib, or 0 if not applicable
    message: str
    layer: str = "static"
    category: str = ""  # finding category, used for per-category severity
    # A FINER identifier that uniquely names the precise issue WITHIN its category
    # ('<category>.<specific>', e.g. metadata_mismatch.title_overlap_strong). Defaults
    # to the category itself for a category that emits exactly one kind of issue, so
    # (category, type) is unique tool-wide. Identifies/disambiguates only; severity
    # may be re-ranked per-type or per-category (see resolve_severity).
    type: str = ""
    # An advisory edit a reader MAY apply: {"field", "from"?, "to"}. It is a
    # suggestion to weigh, not an instruction -- the entry's content is the
    # author's to decide, so the report frames it as 'suggested', never 'fix'.
    suggested: dict = None
    source_file: str = ""  # non-empty for findings whose line is in a .tex file


# Each finding category rolls up into one of three groups, so a reader (or an LLM
# co-author) can route by group without learning all ~25 categories:
#   syntax    -- the entry as written text: will it parse/build, is it well-formed?
#                Detectable from the .bib alone; the safe, mechanical bucket.
#   semantic  -- the entry's bibliographic content: is the metadata correct,
#                complete and consistent with the source of record? The bucket that
#                wants a human (or a source check), not a blind auto-edit.
#   context   -- the entry's relationship to the manuscript (how it is cited).
# A category with no entry here falls back to "semantic" (the conservative,
# review-this default) via finding_group().
CATEGORY_GROUP = {
    # syntax: written form, checkable offline without a source of record
    "syntax": "syntax", "missing_entry_header": "syntax", "style": "syntax",
    "identifier_placement": "syntax",
    "biblatex_validity": "syntax", "encoding": "syntax", "duplicate": "syntax",
    "duplicate_field": "syntax", "duplicate_field_conflict": "syntax",
    "dropped_field": "syntax", "misplaced_field": "syntax", "identifier_format": "syntax",
    # semantic: content / metadata / identity, often needs the source of record
    "metadata_mismatch": "semantic", "source_conflict": "semantic",
    "parity_suggestion": "semantic", "missing_field": "semantic",
    "journal_macro": "semantic",
    "missing_locator": "semantic", "entrytype_suggestion": "semantic",
    "isbn_unresolved": "semantic",
    "datamodel_recommended": "semantic",
    "missing_recommended": "semantic", "pid_missing": "semantic",
    "doi_available": "semantic", "retraction": "semantic",
    "related_work": "semantic",
    "title_case": "semantic", "title_style": "semantic", "author_format": "semantic",
    "title_capitalization": "semantic",
    "author_completeness": "semantic", "author_truncated_marker": "syntax",
    "record_unresolved": "semantic", "dead_doi": "semantic",
    "container_granularity": "semantic",
    # context: the entry's relationship to the manuscript and to the work it points
    # at -- citation order/usage, whether the id resolves to the right paper, whether
    # a published version should be cited instead, and the LLM-relevance ratings.
    "citation_order": "context", "preprint_superseded": "context",
    "preprint_version": "context", "preprint_retitled": "context",
    "id_resolves_wrong_record": "context", "wrong_paper": "context",
    "llm_relevance": "context", "llm_config": "context", "llm_ok": "context",
    "llm_unavailable": "context",
}


def finding_group(category):
    """The group ('syntax'/'semantic'/'context') a category belongs to. Unknown or
    empty categories default to 'semantic' -- the review-this bucket -- so a new
    category is never silently treated as a safe auto-edit."""
    return CATEGORY_GROUP.get(category, "semantic")


# One-line description per finding category, for the audit catalog
# (`veracite --list-rules`, see catalog.py). This is the prose a publisher reads
# to decide whether their house standard agrees with the category's default
# severity. Kept here next to CATEGORY_GROUP and SUPERSEDES so all category
# metadata lives together; tests/test_catalog.py asserts every emittable category
# has an entry, so this cannot fall behind the rules.
CATEGORY_DOC = {
    "syntax": "structural / does-not-parse BibTeX error",
    "missing_entry_header": "an entry's '@type{key,' header line is missing",
    "retraction": "cited work is retracted",
    "wrong_paper": "LLM opinion that the cited paper looks wrong -- verify (warning, not an error)",
    "id_resolves_wrong_record": "doi/arXiv id resolves to a different paper",
    "metadata_mismatch": "author/title/year/vol/pages/journal differ from record",
    "record_unresolved": "no authoritative source returned a record for the id",
    "dead_doi": "the recorded DOI resolves nowhere (404 at both Crossref and DataCite)",
    "isbn_unresolved": "ISBN is syntactically valid but not found in Open Library or Google Books (coverage gap)",
    "container_granularity": "id resolved to the containing volume, not the item",
    "author_completeness": "malformed author truncation (literal 'et al.' / bare 'al.')",
    "author_truncated_marker": "author list ends in the valid 'and others' marker",
    "author_format": "author names malformed (ALL-CAPS, 'and' glued, mixed forms)",
    "source_conflict": "two authoritative sources disagree on data",
    "doi_available": "a DOI exists in Crossref but the entry omits it",
    "pid_missing": "no persistent identifier where one is expected",
    "identifier_format": "malformed DOI/arXiv/ISBN/ISSN/ORCID or year",
    "llm_relevance": "LLM rated the citation weakly relevant (or no abstract)",
    "llm_unavailable": "LLM could not rate (no abstract, or provider error) -- not actionable",
    "llm_ok": "LLM rated the citation relevant (4-5/5) -- a clean-pass note",
    "llm_config": "LLM run misconfigured (e.g. unknown provider)",
    "preprint_superseded": "a published version now exists",
    "preprint_version": "bib year matches an arXiv version (v1 precedence vs latest) -- informational",
    "preprint_retitled": "arXiv renamed the preprint in a later version; the cited title matches an earlier one",
    "related_work": "erratum/correction/comment/reply linked",
    "duplicate": "duplicate citation key or DOI (two entries collide)",
    "duplicate_field": "a field repeated within ONE entry, values agree (benign)",
    "duplicate_field_conflict": "repeated field with DIFFERING values (data dropped)",
    "dropped_field": "a field outside the entry, silently dropped",
    "misplaced_field": "a value in the wrong field (e.g. a year in 'journal')",
    "missing_field": "biber-mandatory field absent (e.g. title, journal)",
    "journal_macro": "journal is an unexpanded LaTeX macro (e.g. \\pra); offer the record's name",
    "missing_locator": "article omits volume/pages -- not mandatory, advisory only",
    "identifier_placement": "an identifier sits in the url, not a structured doi/eprint field",
    "entrytype_suggestion": "the @type looks wrong for the entry's data",
    "datamodel_recommended": "mandatory in biblatex's datamodel but biber tolerates absent",
    "missing_recommended": "field biber doesn't require but we advise (year)",
    "biblatex_validity": "field invalid under biblatex datamodel",
    "title_case": "title looks miscased (mostly UPPERCASE)",
    "title_capitalization": "a CamelCase/mixed-case title term some styles will lowercase",
    "title_style": "title matches the record but punctuation/wording deviates",
    "style": "casing, punctuation, dashes, month, etc.",
    "citation_order": "a \\cite{} group is not in chronological order",
    "encoding": "non-ASCII / mojibake / invisible characters",
    "parity_suggestion": "record has data the bib could adopt",
}


def _oneline(s):
    """Collapse any run of whitespace -- including embedded newlines and tabs --
    to a single space, so a finding (or its verify link) is always exactly one
    line. Handles both deliberate multi-line messages (the title-diff bib/record
    dump) and stray newlines inside a malformed field value (e.g. a DOI that the
    .bib wrapped mid-string)."""
    return re.sub(r"\s+", " ", str(s)).strip()


def _preview(s, width=42):
    """A readable, length-bounded form of a value for the prose line. Long values
    (a whole title) are middle-elided; the JSON keeps the full value, so only the
    on-screen text is shortened -- the structured suggestion stays complete."""
    s = str(s)
    if len(s) <= width:
        return s
    head = (width - 3) // 2
    return s[:head] + "..." + s[-(width - 3 - head):]


# Characters that are brace-protection / quoting noise, not content: a leading '{'
# on a bib title vs none on the record must NOT count as the divergence point (it
# would defeat the difference-aware window). Skipped from BOTH ends before the
# common prefix/suffix is measured.
_EDGE_NOISE = "{}\"' \t"


def _diff_span(a, b):
    """The index where strings `a` and `b` first diverge and where their common
    suffix begins (as offsets from each end). Returns (prefix_len, a_suffix_len,
    b_suffix_len) so a caller can window each string around the part that actually
    changed. Leading/trailing brace/quote/space noise is stepped over so a '{'
    wrapper on one side is not mistaken for a real difference at index 0."""
    # Advance past a leading run of edge-noise that differs only in wrapping.
    p = 0
    while (p < len(a) and p < len(b) and a[p] == b[p]):
        p += 1
    # If we stalled at the very start on pure wrapping noise, skip the noise on each
    # side and retry the prefix match from there.
    if p == 0:
        ia = ib = 0
        while ia < len(a) and a[ia] in _EDGE_NOISE:
            ia += 1
        while ib < len(b) and b[ib] in _EDGE_NOISE:
            ib += 1
        while ia < len(a) and ib < len(b) and a[ia] == b[ib]:
            ia += 1
            ib += 1
        p = min(ia, ib)
    sa, sb = len(a), len(b)
    while sa > p and sb > p and a[sa - 1] == b[sb - 1]:
        sa -= 1
        sb -= 1
    # Step back over trailing wrapping noise so a '}' on one side is not the suffix.
    while sa > p and sb > p and a[sa - 1] in _EDGE_NOISE and b[sb - 1] in _EDGE_NOISE:
        sa -= 1
        sb -= 1
    return p, len(a) - sa, len(b) - sb


def _preview_pair(frm, to, width=42):
    """Preview a (from, to) pair so the part that DIFFERS stays visible. A naive
    middle-elision can hide a mid-string change (e.g. one word in a long title that
    is otherwise identical), making 'from' and 'to' look the same on screen. Center
    each value's elision window on the divergence point instead. Falls back to plain
    middle-elision when the strings share little, or are short."""
    frm, to = str(frm), str(to)
    if len(frm) <= width and len(to) <= width:
        return frm, to
    pre, _, _ = _diff_span(frm, to)
    # Only worth centering when there is a real shared prefix to elide past; with
    # little in common, the plain middle-elision is already representative.
    if pre <= width // 3:
        return _preview(frm, width), _preview(to, width)

    def window(s):
        if len(s) <= width:
            return s
        # Keep a little context before the divergence, then run to the end (the
        # tail is itself elided by _preview if still too long).
        ctx = max(0, pre - width // 3)
        head = "..." if ctx > 0 else ""
        return head + _preview(s[ctx:], width - len(head))
    return window(frm), window(to)


def format_suggested(suggested):
    """Render a structured `suggested` ({field, from?, to}) as the advisory prose
    tail ' (suggested: 'from' -> 'to')', or ' (suggested: 'to')' when there is no
    'from'. This is the ONE place the arrow text is built, so the human line is
    derived from the same dict the JSON carries -- the prose can never drift from
    the structured suggestion (long values are previewed for the screen only).
    Returns '' when there is nothing to suggest."""
    if not suggested or "to" not in suggested:
        return ""
    if "from" in suggested:
        # Difference-aware so a mid-string change (one word in a long title) stays
        # visible rather than being elided away.
        frm, to = _preview_pair(suggested["from"], suggested["to"])
        return f" (suggested: {frm!r} -> {to!r})"
    return f" (suggested: {_preview(suggested['to'])!r})"


# User-facing severity labels accepted in the settings file.
SEVERITY_NAMES = {
    "error": Severity.ERROR, "err": Severity.ERROR,
    "warning": Severity.WARN, "warn": Severity.WARN,
    "note": Severity.INFO, "info": Severity.INFO,
}


def resolve_severity(default, category, type=""):
    """The configured severity for a finding (from SETTINGS['severity']) if set, else
    the check's own default. MOST-SPECIFIC WINS: a per-`type` key (e.g.
    'metadata_mismatch.title_overlap_slight') overrides a whole-`category` key
    ('metadata_mismatch'), which overrides the check's default. Lets a user re-rank a
    single precise issue or a whole class without editing code. Accepts the labels
    error/warning/note. With no override set (the default config) it returns the
    check's default unchanged -- so a run with no settings is unaffected."""
    sev = SETTINGS.get("severity", {})
    name = None
    if type and type != category:
        name = sev.get(type)
    if name is None and category:
        name = sev.get(category)
    return SEVERITY_NAMES.get(str(name).lower(), default) if name else default


# --- supersession: which findings retract which others ---------------------
# The ONE place the cross-finding dependencies live. A later (usually online)
# layer can prove an earlier (usually offline) finding was a false positive; when
# it does it calls Report.supersede(key, loser), and at emit time the finding in
# the loser category PRODUCED BY THE LOSING LAYER (for that key) is dropped.
# Declaring the pairs here -- rather than scattering imperative rep.withdraw()
# calls through the rules -- keeps the relationships visible in one table and makes
# them order-independent (resolved at emit, so it no longer matters which layer ran
# first). The losing layer is named so a *replacement* finding in the same category
# emitted by the winning layer (e.g. the record's 'adopt the record's casing') is
# NOT itself dropped.
#
#   loser category  ->  (losing layer, who supersedes it, why)
SUPERSEDES = {
    "author_completeness": ("static", "record layer",
        "the authoritative record lists no more authors than the bib already has, "
        "so an 'et al.'/'al.' truncation is faithful, not lossy"),
    "author_truncated_marker": ("static", "record layer",
        "the authoritative record lists no more authors than the bib already has, "
        "so an 'and others' truncation is faithful, not lossy"),
    "missing_locator": ("static", "record layer",
        "the record's parity_suggestion names the exact volume/pages to add, so the "
        "generic 'missing locator' note would state the same fact twice"),
    "identifier_placement": ("static", "record layer",
        "the online 'doi_available' finding reports the SAME url DOI (and confirms it "
        "resolved), so the offline placement nudge would state the same fact twice"),
    "entrytype_suggestion": ("static", "record layer",
        "the entry RESOLVED to a real journal record, disproving the offline "
        "'looks like a web item' guess -- it is a genuine @article"),
    "title_case": ("static", "record layer",
        "the record carries the canonical casing, so the offline 'looks miscased' "
        "guess is replaced by 'adopt the record's casing'"),
    "misplaced_field": ("static", "record layer",
        "once the entry resolves, either the record corroborates the bib's value "
        "(not a misplacement) or a metadata_mismatch already gives the correct value "
        "with a concrete suggested fix -- the structural diagnosis adds nothing"),
    "preprint_retitled": ("record", "preprint",
        "a published version of record now exists, so citing it (the "
        "preprint_superseded suggestion) is the one fix -- the 'renamed in a later "
        "version' note would describe the same action a second time"),
}


class Report:
    """Collects findings and renders the report."""

    # Layers whose findings a human verifies against the source of record.
    _ONLINE_LAYERS = {"record", "retract", "related", "preprint"}

    def __init__(self, color=True, show_suppressed=False):
        self.findings = []
        self.color = color
        # When True, the terminal view also shows suppressed findings (dimmed, with
        # the winner that retracted them). Off by default so the default output is
        # byte-identical to the pre-persist-all behavior; the JSON always carries them.
        self.show_suppressed = show_suppressed
        self.links = {}      # citation key -> URL a human can open to verify
        self.status = {}     # citation key -> (status, confidence, detail) for header
        self._emitted = set()  # indices into findings already printed (emit_entry)
        self._superseded = set()  # (key, category) pairs retracted by a later layer
        self._uncited = set()  # keys reduced to a one-line UNCITED header (--tex mode)

    def set_link(self, key, url):
        """Record a URL (publisher/DOI/arXiv page) for an entry, shown beside its
        online findings so a flagged disagreement is one click to check."""
        if url and key not in self.links:
            self.links[key] = url

    def set_status(self, key, status, confidence, detail=""):
        """Record an entry's verification verdict for its header line (the status no
        longer prints as its own finding -- see verify.classify). `detail` is the
        short human reason shown for a non-clean status."""
        self.status[key] = (status, confidence, detail)

    def status_detail(self, key):
        """The short header reason recorded for `key` (the 3rd element of its status
        tuple), or "" -- shown after a non-clean status and persisted to the record so
        the header is reconstructible from the record alone."""
        st = self.status.get(key)
        return (st[2] if st and len(st) > 2 else "") or ""

    def mark_uncited(self, key):
        """Mark an entry as UNCITED (--tex mode): its block is just a one-line header
        saying so, with no findings -- it was skipped from all analysis."""
        self._uncited.add(key)

    def add(self, severity, target, message, layer="static", category="", field="",
            suggested=None, source_file="", type=""):
        """Record a finding. `target` is an Entry (uses its key/line) or a
        (key, line) pair. `severity` is the check's default; a matching entry in
        SETTINGS['severity'] for the `type` (most specific) or the `category`
        overrides it. When `field` names a bib field, the finding points at that
        field's line. `suggested` is an optional advisory edit dict surfaced in JSON.
        `source_file` names the .tex file when `line` refers to a tex location.
        `type` is the finer issue identifier within the category; it defaults to the
        category when omitted (a category that emits one kind of issue)."""
        if hasattr(target, "key"):           # an Entry
            key = target.key
            line = target.field_line(field) if field else target.lineno
        else:
            key, line = target
        type = type or category
        severity = resolve_severity(severity, category, type)
        self.findings.append(
            Finding(severity, key, line, message, layer=layer, category=category,
                    type=type, suggested=suggested, source_file=source_file))

    def add_file(self, severity, message, layer="static", category="", type=""):
        type = type or category
        self.findings.append(Finding(resolve_severity(severity, category, type),
                                     "<file>", 0, message, layer=layer,
                                     category=category, type=type))

    def seed_superseded(self, pairs):
        """Restore (key, category) supersessions re-derived from a saved report
        (checkpoint resume), so a reused phase's findings stay suppressed without the
        phase re-running to re-declare them.

        Unlike supersede() -- which asserts, because IT is called with literal
        categories from this version's own rules -- this receives data deserialized
        from a saved report that a FUTURE version may have written, possibly stamping
        a suppression category this version does not know. Per the charter
        ('tolerate a report a future version wrote; treat unknown fields as opaque'),
        an unknown category is SKIPPED, not asserted: we simply do not re-derive a
        suppression we do not understand, leaving that finding visible -- a safe
        degradation, never a crash on resume."""
        for key, category in pairs:
            if category in SUPERSEDES:
                self._superseded.add((key, category))

    def seed_findings(self, findings):
        """Pre-load findings rebuilt from a saved report (checkpoint resume), so a
        replayed run reproduces the prior phases' findings without recomputing them.
        They are appended as-is (their severity was already resolved when first
        saved). The driver only seeds the phases it is NOT recomputing this run, so
        a re-run of one phase naturally replaces that phase's findings."""
        self.findings.extend(findings)

    def count(self, severity):
        return sum(1 for f in self.live_findings() if f.severity is severity)

    def supersede(self, key, category):
        """Mark a finding category for `key` as superseded by a later layer that
        disproved it; it is dropped at emit time. The pair MUST be declared in the
        SUPERSEDES table -- that table, not this call, documents the relationship,
        so every cross-finding dependency is visible in one place and the outcome
        does not depend on which layer ran first."""
        assert category in SUPERSEDES, \
            f"supersede({category!r}) not declared in report.SUPERSEDES"
        self._superseded.add((key, category))

    # Back-compat alias: earlier code called this `withdraw`.
    withdraw = supersede

    def _is_superseded(self, f):
        """Whether finding `f` is the losing side of a recorded supersession: its
        category was superseded for its key, and it comes from the layer the
        SUPERSEDES table names as the loser (so a replacement finding in the same
        category from the winning layer survives)."""
        if (f.key, f.category) not in self._superseded:
            return False
        losing_layer = SUPERSEDES[f.category][0]
        return f.layer == losing_layer

    def _superseded_by(self, f):
        """The category (the table's `who supersedes it` label) that retracted `f`,
        or None when `f` is live. Recorded ON the loser so the persisted record is
        self-describing about its own suppression: a reader sees the issue WAS
        detected and WHY it is not surfaced. Derived at read time from the same
        _superseded set + SUPERSEDES table that drives _is_superseded, so the stamp
        and the filtering can never disagree."""
        if not self._is_superseded(f):
            return None
        return SUPERSEDES[f.category][1]   # the human 'who supersedes it' label

    def live_findings(self):
        """Findings with the superseded ones removed. Computed at read/emit time so
        a supersede() recorded after a finding was added still takes effect,
        regardless of rule order. Every consumer that COUNTS or SCORES findings
        should read THIS, not `self.findings`, so a retracted false positive does
        not leak into a count or the integrity score. (The JSON record persists ALL
        issues, live and suppressed -- see issues_for(include_suppressed=True) -- but
        the suppressed ones are stamped and excluded from every aggregate.)"""
        if not self._superseded:
            return self.findings
        return [f for f in self.findings if not self._is_superseded(f)]

    def emit_entry(self, entry, out=None, skip_notes=False, progress=""):
        """Print, once, a block for one entry: a header line that identifies the
        record (key, @type, line, verification status+confidence, cause, and the
        verify: link), then its findings indented beneath in severity order, then a
        blank line. The driver calls this once per entry so the report is a sequence
        of per-entry blocks in bibtex order. `progress` is an optional '[i/N]'
        counter prepended to the header. A block prints when the entry has findings
        OR a status that asks for attention (anything but a clean VERIFIED 1.0), so
        a bare UNVERIFIED still shows even with no other finding. Returns True if a
        block was printed.

        emit_entry owns only the RUN-STATE decisions -- which not-yet-emitted, live,
        not-note-suppressed findings to show, and marking them emitted. It then builds
        the entry's canonical record dict (the same shape written to --json) and hands
        it to `render_entry_record`, so the terminal block is a pretty-print of the
        record, never a parallel formatting path."""
        from .checkpoint import entry_record   # lazy: avoid import cycle
        out = out if out is not None else sys.stdout
        key = entry.key
        # An UNCITED entry (--tex mode) is one line and nothing else.
        if key in self._uncited:
            rec = entry_record(key, None, None, None, set(), [],
                               entry_type=entry.etype, line=entry.lineno, uncited=True)
            return self.render_entry_record(rec, out=out, progress=progress)
        # `visible` decides whether the block has anything to SHOW (drives the
        # return-False-when-silent rule): not-yet-emitted, not suppressed, not a
        # note hidden by skip_notes. Suppressed findings never make a block appear.
        visible = [i for i, f in enumerate(self.findings)
                   if i not in self._emitted and f.key == key
                   and not self._is_superseded(f)
                   and not (skip_notes and f.severity is Severity.INFO)]
        # `shown` is what is PERSISTED into the record's issues: every not-yet-emitted
        # finding for the key INCLUDING suppressed ones (stamped) -- so the NDJSON is
        # self-describing -- but excluding notes hidden by skip_notes (a presentation
        # choice, not a suppression). render_entry_record filters the suppressed ones
        # out of the terminal view, keeping the default output byte-identical.
        shown = [i for i, f in enumerate(self.findings)
                 if i not in self._emitted and f.key == key
                 and not (skip_notes and f.severity is Severity.INFO)]
        # Mark every finding for this key emitted (even notes hidden by skip_notes)
        # so it is not re-printed later; counts in the summary still include them.
        for i, f in enumerate(self.findings):
            if f.key == key:
                self._emitted.add(i)
        st = self.status.get(key)
        # An analyzed entry always has a status, so it always prints at least its
        # one-line header -- a clean reference shows a single 'VERIFIED (...)' line
        # rather than vanishing, which also keeps the [i/N] counter contiguous.
        # Only an entry with neither a status nor any VISIBLE finding (e.g. one never
        # resolved online and otherwise clean) has nothing to say.
        if not visible and st is None:
            return False
        status, conf = (st[0], st[1]) if st else (None, None)
        issues = [self._finding_dict(self.findings[i])
                  for i in sorted(shown, key=lambda i: self.findings[i].severity)]
        rec = entry_record(key, None, status, conf, set(), issues,
                           verify=self.links.get(key),
                           entry_type=entry.etype, line=entry.lineno,
                           status_detail=self.status_detail(key))
        return self.render_entry_record(rec, out=out, progress=progress)

    def render_entry_record(self, rec, out=None, progress="", show_suppressed=None):
        """Pretty-print one entry's canonical record (the dict `entry_record` builds /
        an --json NDJSON line) as its terminal block: the identifying header, then its
        `issues` as indented finding lines in severity order, then a blank line. This
        is the single rendering path -- the live run and a saved report render through
        here, so the terminal output is reconstructible from the NDJSON alone. Returns
        True if a block was printed (uncited or status/findings present).

        The record's `issues` may carry SUPPRESSED issues (stamped `suppressed_by`);
        they are hidden by default so the terminal stays byte-identical to the
        pre-persist-all behavior. `show_suppressed` reveals them, dimmed, with the
        winner that retracted them -- a debugging/audit view, never the default."""
        out = out if out is not None else sys.stdout
        if show_suppressed is None:
            show_suppressed = self.show_suppressed
        if rec.get("uncited"):
            # No trailing blank line: a skipped entry is one line, so a run of them
            # reads as a compact list rather than double-spaced.
            print(self._record_header(rec, progress=progress), file=out)
            return True
        all_issues = rec.get("issues") or []
        # The DEFAULT view is the live (non-suppressed) issues only -- the single
        # display filter point, keyed on the persisted stamp so a saved report and a
        # live run filter identically.
        visible = [d for d in all_issues
                   if show_suppressed or not d.get("suppressed_by")]
        if not visible and rec.get("status") is None:
            return False
        print(self._record_header(rec, progress=progress), file=out)
        for issue in sorted(visible, key=lambda d: Severity[d.get("severity", "INFO")]):
            print(self._issue_line(issue), file=out)
        print(file=out)   # blank line separates entry blocks
        return True

    def _record_header(self, rec, progress=""):
        """The block header from a record dict -- the single identifying line:
          [i/N] key  @type  line N  STATUS (conf) -- detail   verify: <url>
        Mirrors `_entry_header`, reading every field from the record so the header is
        reconstructible from the NDJSON. STATUS is colored by pass/fail; a clean
        VERIFIED 1.0 shows just the word, a caveat adds '(confidence X)', and
        UNVERIFIED/MISMATCH add their cause. An uncited record ends in a dim marker."""
        bits = []
        if progress:
            bits.append(self._c(progress, _DIM))
        bits.append(self._c(rec["key"], _BOLD))
        if rec.get("entry_type"):
            bits.append(self._c(f"@{rec['entry_type']}", _DIM))
        if rec.get("line"):
            bits.append(self._c(f"line {rec['line']}", _DIM))
        if rec.get("uncited"):
            bits.append(self._c("UNCITED in .tex source; skipped from further analysis", _DIM))
            return "  ".join(bits)
        st = rec.get("status")
        if st is not None:
            conf = rec.get("confidence")
            detail = rec.get("status_detail") or ""
            color = _GREEN if st == "VERIFIED" else (
                SEVERITY_STYLE[Severity.ERROR][1] if st == "MISMATCH"
                else SEVERITY_STYLE[Severity.WARN][1])
            label = st
            if conf is not None and not (st == "VERIFIED" and conf >= 1.0):
                label += f" (confidence {conf:.2f})"
            piece = self._c(label, color)
            # Detail is shown only for UNVERIFIED/MISMATCH: a VERIFIED header's caveat
            # is already spelled out by the metadata_mismatch/source_conflict finding
            # below it, so repeating it here would be noise.
            if detail and st != "VERIFIED":
                piece += self._c(f" -- {detail}", _DIM)
            bits.append(piece)
        line = "  ".join(bits)
        if rec.get("verify"):
            line += self._c(f"; {_oneline(rec['verify'])}", _DIM)
        return line

    def _issue_line(self, d, with_key=False, key=""):
        """One formatted finding line from an issue dict (the `_finding_dict` shape),
        the record-based twin of `_finding_line`. Shape (stable for parsing):
          [TAG] category (line N): message (suggested: X -> Y)
        The advisory tail is derived from the issue's `suggested`, so the prose stays
        in lock-step with the structured patch. An `action` tag is appended when it
        adds information: [fix] when a suggested patch is attached, [check manually]
        when no fix exists but action is needed -- so the reader knows at a glance
        whether the finding is self-contained or requires a lookup."""
        sev = Severity[d.get("severity", "INFO")]
        tag, color, _ = SEVERITY_STYLE[sev]
        pad = " " * (len("[ERROR]") - len(tag) - 2)
        code = self._c(d.get("category") or d.get("layer", ""), _DIM)
        if d.get("source_file") and d.get("line"):
            loc = f" ({d['source_file']} line {d['line']})"
        elif d.get("line"):
            loc = f" (line {d['line']})"
        else:
            loc = ""
        action = d.get("action", "info")
        if action == "fix":
            action_tag = self._c(" [fix]", _GREEN)
        elif action == "investigate":
            action_tag = self._c(" [check manually]", SEVERITY_STYLE[Severity.WARN][1])
        else:
            action_tag = ""
        msg = _oneline(d.get("message", "") + format_suggested(d.get("suggested")))
        # A suppressed issue (only reachable here under --show-suppressed) is dimmed
        # and annotated with the winner that retracted it, so the audit view shows
        # WHAT was detected and WHY it is not surfaced by default.
        if d.get("suppressed_by"):
            msg = self._c(f"{msg}  (suppressed by {d['suppressed_by']})", _DIM)
        keytag = (self._c(key, _BOLD) + " ") if with_key and key else ""
        return f"    {self._c(f'[{tag}]', color)}{pad} {keytag}{code}{loc}:{action_tag} {msg}"

    def emit_remaining(self, out=None, skip_notes=False, only_key=None):
        """Print any findings not yet emitted by emit_entry -- the file-level group
        (<file> syntax, cross-entry rules) plus any entry never visited by the
        driver -- under a 'file-level' header. Each line keeps its key (with_key)
        since there is no per-entry header here. Returns True if anything printed.
        When `only_key` is set (--key), restrict to that key plus the reserved
        <file> record, so a focused run shows just the asked-about entry."""
        out = out if out is not None else sys.stdout
        rest = [i for i, f in enumerate(self.findings)
                if i not in self._emitted
                and not self._is_superseded(f)
                and not (skip_notes and f.severity is Severity.INFO)
                and (only_key is None or f.key in (only_key, "<file>"))]
        for i, f in enumerate(self.findings):
            self._emitted.add(i)
        if not rest:
            return False
        print(self._c("file-level", _BOLD), file=out)
        for i in sorted(rest, key=lambda i: (self.findings[i].key, self.findings[i].severity)):
            f = self.findings[i]
            print(self._issue_line(self._finding_dict(f), with_key=True, key=f.key), file=out)
        print(file=out)
        return True

    def emit_by_severity(self, out=None, skip_notes=False, only_key=None):
        """Triage view (`--sort=severity`): every live finding as one flat, global
        list grouped by severity (errors, then warnings, then notes), each group in
        bibtex-key order. Every line is self-contained (carries its key), so the
        whole block parses without per-entry headers. Marks all findings emitted.
        When `only_key` is set (--key), restrict to that key plus the <file> record.
        Returns True if anything printed."""
        out = out if out is not None else sys.stdout
        live = [f for f in self.findings
                if not self._is_superseded(f)
                and not (skip_notes and f.severity is Severity.INFO)
                and (only_key is None or f.key in (only_key, "<file>"))]
        for i in range(len(self.findings)):
            self._emitted.add(i)
        if not live:
            return False
        for sev in (Severity.ERROR, Severity.WARN, Severity.INFO):
            group = sorted((f for f in live if f.severity is sev), key=lambda f: f.key)
            for f in group:
                line = self._issue_line(self._finding_dict(f), with_key=True, key=f.key)
                if self.links.get(f.key) and f.layer in self._ONLINE_LAYERS:
                    line += self._c(f"; {_oneline(self.links[f.key])}", _DIM)
                print(line, file=out)
        print(file=out)
        return True

    def error_keys(self):
        return sorted({f.key for f in self.live_findings()
                       if f.severity is Severity.ERROR})

    # -- rendering ----------------------------------------------------------

    def _c(self, text, code):
        return f"{code}{text}{_RESET}" if self.color else text

    def render_header(self, out=None, online=True, llm_used=False):
        """Print the accuracy disclaimer that leads the report, before the
        per-entry findings stream out."""
        out = out if out is not None else sys.stdout
        if online:
            for line in self._render_disclaimer(llm_used):
                print(line, file=out)
            print(file=out)

    def render_summary(self, n_entries, n_cited, out=None, skip_notes=False,
                       tex_mode=True, any_findings=True, integrity=None):
        """Print the closing verdict/counts. Findings themselves are printed
        per entry by emit_entry/emit_remaining, so this is summary-only. When an
        `integrity` dict (from verify.integrity) is given, append the verification
        counts, coverage and score (Layer 6)."""
        out = out if out is not None else sys.stdout
        ne = self.count(Severity.ERROR)
        nw = self.count(Severity.WARN)
        ni = self.count(Severity.INFO)
        if not any_findings:
            print(self._c("No problems found.", _GREEN), file=out)
        print(file=out)
        for line in self._render_summary(n_entries, n_cited, ne, nw, ni, skip_notes,
                                         tex_mode):
            print(line, file=out)
        if integrity:
            for line in self._render_integrity(integrity):
                print(line, file=out)

    def _score_color(self, score):
        """Green >= 90, yellow >= 70, else red -- shared by both 0-100 scores."""
        return _GREEN if score >= 90 else (
            SEVERITY_STYLE[Severity.WARN][1] if score >= 70
            else SEVERITY_STYLE[Severity.ERROR][1])

    def _render_integrity(self, s):
        """The Layer-6 roll-up appended to the summary: the verification counts,
        coverage, and the two headline scores (integrity + confidence)."""
        bar = self._c("=" * 64, _DIM)
        integ = s["integrity_score"]
        conf = s.get("confidence_score")
        caveat = s.get("verified_with_caveat", 0)
        caveat_txt = f" ({caveat} with a caveat)" if caveat else ""
        lines = [
            f"  verified {self._c(str(s['verified']), _GREEN)}{caveat_txt}  "
            f"unverified {self._c(str(s['unverified']), SEVERITY_STYLE[Severity.WARN][1])}  "
            f"mismatch {self._c(str(s['mismatch']), SEVERITY_STYLE[Severity.ERROR][1])}"
            f"  (of {s['checked']} checked)",
            # Two different denominators: DOI coverage is over eligible records only
            # (post-2005 journal articles, where a DOI is expected); PID coverage is
            # over ALL checked, counting any persistent id (DOI, arXiv, or ISBN).
            f"  DOI coverage: {s['doi_coverage']:.0%} of {s['doi_eligible']} eligible "
            f"(post-2005 articles)   "
            f"PID coverage: {s['pid_coverage']:.0%} of {s['checked']} checked "
            f"(DOI/arXiv/ISBN)",
        ]
        # Two headline scores, side by side: integrity (is the bib sound?) and
        # confidence (how much we trust the verifications we made?).
        score_line = (f"  {self._c('integrity', _BOLD)} "
                      f"{self._c(str(integ) + '/100', self._score_color(integ))}")
        if conf is not None:
            score_line += (f"   {self._c('confidence', _BOLD)} "
                           f"{self._c(str(conf) + '/100', self._score_color(conf))}")
        lines += [score_line, bar]
        return lines

    def _render_disclaimer(self, llm_used):
        """Lead the report with the accuracy disclaimer(s)."""
        def wrap(text):
            words, line, out = text.split(), "", []
            for w in words:
                if len(line) + len(w) + 1 > 76:
                    out.append("  " + line)
                    line = w
                else:
                    line = f"{line} {w}".strip()
            if line:
                out.append("  " + line)
            return out

        lines = [self._c("NOTE", _BOLD) + ": " + self._c("read before acting on findings", _DIM)]
        lines += [self._c(s, _DIM) for s in wrap(DB_DISCLAIMER)]
        if llm_used:
            lines.append("")
            lines += [self._c(s, _DIM) for s in wrap(LLM_DISCLAIMER)]
        return lines

    def _render_summary(self, n_entries, n_cited, ne, nw, ni, skip_notes=False,
                        tex_mode=True):
        # The verdict word only -- the error/warning/note counts are on their own
        # line just below, so the old '-- N error(s)' tail was a duplicate.
        if ne:
            verdict = self._c("NEEDS ATTENTION", SEVERITY_STYLE[Severity.ERROR][1])
        elif nw:
            verdict = self._c("OK", SEVERITY_STYLE[Severity.WARN][1])
        else:
            verdict = self._c("HEALTHY", _GREEN)
        bar = self._c("=" * 64, _DIM)
        # Notes are always counted (so the user knows they exist) even when hidden.
        note_count = f"{self._c(str(ni), SEVERITY_STYLE[Severity.INFO][1])} notes"
        if skip_notes and ni:
            note_count += self._c(" (hidden; drop --skipnotes to see)", _DIM)
        counts = (f"{self._c(str(ne), SEVERITY_STYLE[Severity.ERROR][1])} errors   "
                  f"{self._c(str(nw), SEVERITY_STYLE[Severity.WARN][1])} warnings   "
                  + note_count)
        # Reflect which mode ran: cited-aware (--tex) vs every-entry (bib-only).
        if tex_mode:
            scope = (f"checked {n_cited} of {n_cited} cited references "
                     f"({n_entries} in bib)")
        else:
            scope = f"checked all {n_entries} references in bib (no .tex)"
        from .config import VERSION
        lines = [bar,
                 f"{self._c('BIBLIOGRAPHY HEALTH:', _BOLD)} {verdict}"
                 f"{self._c(f'   (VeraCite {VERSION})', _DIM)}",
                 f"  {scope}",
                 f"  {counts}"]
        if ne:
            lines.append("  affected: " + ", ".join(self.error_keys()))
        lines.append(bar)
        return lines

    def _finding_dict(self, f):
        # `action` classifies what the reader should do, independent of severity:
        #   "fix"         -- a structured `suggested` patch is attached; a tool can
        #                    apply it or a human can follow it directly.
        #   "investigate" -- a real problem was detected but the right value is unknown
        #                    or uncertain; human judgement required.
        #   "info"        -- no action needed, or the action is fully described in the
        #                    prose (e.g. a portability nudge the author may choose to act
        #                    on or ignore).
        has_fix = bool(f.suggested and f.suggested.get("to"))
        if has_fix:
            action = "fix"
        elif f.severity in (Severity.ERROR, Severity.WARN):
            action = "investigate"
        else:
            action = "info"
        d = {"severity": f.severity.name, "layer": f.layer,
             "category": f.category, "type": f.type or f.category,
             "group": finding_group(f.category),
             "line": f.line, "action": action,
             "message": f.message,
             "suggested": f.suggested if f.suggested else None}
        if f.source_file:
            d["source_file"] = f.source_file
        # A retracted finding is PERSISTED (not dropped) but stamped with the winner
        # that retracted it, so the record is self-describing about its suppression
        # decisions. The stamp is derived from the live _superseded set, so it cannot
        # disagree with what live_findings()/the renderer filter on. Live findings
        # carry no key (kept out of the dict so a clean issue is byte-identical to
        # before this field existed).
        sup = self._superseded_by(f)
        if sup:
            d["suppressed_by"] = sup
        return d

    def issues_for(self, key, include_suppressed=True):
        """One key's findings as a list of issue dicts (the per-entry record's
        `issues`). Persists ALL of them -- live and suppressed -- so the NDJSON record
        is self-describing; suppressed issues carry a `suppressed_by` stamp and are
        excluded from every count/score/default-render. Pass include_suppressed=False
        for the rare consumer that wants only the live set."""
        src = self.findings if include_suppressed else self.live_findings()
        return [self._finding_dict(f) for f in src if f.key == key]

    def to_json(self, summary=None, results=None, statuses=None, phases_by_key=None,
                entries=None):
        """Machine-readable report. Always includes the flat `findings` list (for
        back-compat); each finding carries its `category`, the `group` that category
        rolls up into (syntax/semantic/context), and -- when the check proposes a
        concrete advisory edit -- a structured `suggested` ({field, from?, to}).
        When verification data is supplied, emits a per-reference `references` array
        (one entry record per analyzed entry, built by the SAME `entry_record` that
        writes each --json NDJSON line -- one record shape, never two) and a `summary`
        block DERIVED from those records (the single source of truth) plus the live
        file-level findings. `entries` (an iterable of Entry, optional) supplies each
        record's identifying `entry_type`/`line`; without it those are null. A caller
        may still pass an explicit `summary` to override the derived one."""
        from .checkpoint import entry_record   # lazy: avoid import cycle
        from .verify import integrity          # lazy: avoid import cycle
        live = self.live_findings()
        out = {"findings": [self._finding_dict(f) for f in live]}
        if results is not None and statuses is not None:
            phases_by_key = phases_by_key or {}
            meta = {e.key: e for e in (entries or [])}   # key -> Entry (etype/lineno)
            # One reference per entry analyzed in ANY phase. Online entries come from
            # `results`; an offline-only entry has no resolution but still records its
            # `phases` (so a later run can resume it), with a null status/record --
            # honest, not a fabricated verdict. Order follows analysis order (results
            # first, then offline-only keys) so the array is stable across rewrites.
            keys = list(results)
            keys += [k for k in phases_by_key if k not in results]
            refs = []
            for key in keys:
                status, conf = statuses.get(key, (None, None))
                e = meta.get(key)
                refs.append(entry_record(
                    key, results.get(key), status, conf,
                    phases_by_key.get(key, set()), self.issues_for(key),
                    verify=self.links.get(key) or None,
                    entry_type=(e.etype if e else None),
                    line=(e.lineno if e else 0),
                    bib_year=(e.get("year") if e else None),
                    status_detail=self.status_detail(key)))
            out["references"] = refs
            # Summary is DERIVED from the records just built (not a stored aggregate),
            # so the web payload and the CLI summary take the identical parse path.
            if summary is None:
                summary = integrity(refs, self)
        if summary is not None:
            out["summary"] = summary
        return out
