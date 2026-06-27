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
    # An advisory edit a reader MAY apply: {"field", "from"?, "to"}. It is a
    # suggestion to weigh, not an instruction -- the entry's content is the
    # author's to decide, so the report frames it as 'suggested', never 'fix'.
    suggested: dict = None


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
    "datamodel_recommended": "semantic",
    "missing_recommended": "semantic", "pid_missing": "semantic",
    "doi_available": "semantic", "retraction": "semantic",
    "related_work": "semantic",
    "title_case": "semantic", "title_style": "semantic", "author_format": "semantic",
    "author_completeness": "semantic", "author_truncated_marker": "syntax",
    "record_unresolved": "semantic", "dead_doi": "semantic",
    "container_granularity": "semantic",
    # context: the entry's relationship to the manuscript and to the work it points
    # at -- citation order/usage, whether the id resolves to the right paper, whether
    # a published version should be cited instead, and the LLM-relevance ratings.
    "citation_order": "context", "preprint_superseded": "context",
    "preprint_version": "context",
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


def resolve_severity(default, category):
    """A category's configured severity (from SETTINGS['severity']) if set, else
    the check's own default. Lets a user re-rank a whole class of findings (e.g.
    make 'preprint_superseded' an error) without editing code. Accepts the labels
    error/warning/note (the names shown in the report)."""
    name = SETTINGS.get("severity", {}).get(category) if category else None
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
        "the resolved record carries the bib's 'number' as the issue, so the value "
        "is not a misplaced year after all -- the offline guess is disproven"),
}


class Report:
    """Collects findings and renders the report."""

    # Layers whose findings a human verifies against the source of record.
    _ONLINE_LAYERS = {"record", "retract", "related", "preprint"}

    def __init__(self, color=True):
        self.findings = []
        self.color = color
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

    def mark_uncited(self, key):
        """Mark an entry as UNCITED (--tex mode): its block is just a one-line header
        saying so, with no findings -- it was skipped from all analysis."""
        self._uncited.add(key)

    def add(self, severity, target, message, layer="static", category="", field="",
            suggested=None):
        """Record a finding. `target` is an Entry (uses its key/line) or a
        (key, line) pair. `severity` is the check's default; when `category` is
        given, a matching entry in SETTINGS['severity'] overrides it. When `field`
        names a bib field, the finding points at that field's line. `suggested` is
        an optional advisory edit dict ({"field", "from"?, "to"}) surfaced in JSON."""
        if hasattr(target, "key"):           # an Entry
            key = target.key
            line = target.field_line(field) if field else target.lineno
        else:
            key, line = target
        severity = resolve_severity(severity, category)
        self.findings.append(
            Finding(severity, key, line, message, layer, category, suggested))

    def add_file(self, severity, message, layer="static", category=""):
        self.findings.append(Finding(resolve_severity(severity, category),
                                     "<file>", 0, message, layer, category))

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

    def live_findings(self):
        """Findings with the superseded ones removed. Computed at read/emit time so
        a supersede() recorded after a finding was added still takes effect,
        regardless of rule order. Every consumer that counts, scores, or renders
        findings should read THIS, not `self.findings`, so a retracted false
        positive does not leak into a count or the integrity score."""
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
        block was printed."""
        out = out if out is not None else sys.stdout
        key = entry.key
        # An UNCITED entry (--tex mode) is one line and nothing else.
        if key in self._uncited:
            # No trailing blank line: a skipped entry is one line, so a run of them
            # reads as a compact list rather than double-spaced.
            print(self._entry_header(entry, progress=progress, uncited=True), file=out)
            return True
        idx = [i for i, f in enumerate(self.findings)
               if i not in self._emitted and f.key == key
               and not self._is_superseded(f)
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
        # Only an entry with neither a status nor any finding (e.g. one never
        # resolved online and otherwise clean) has nothing to say.
        if not idx and st is None:
            return False
        print(self._entry_header(entry, st, progress), file=out)
        for i in sorted(idx, key=lambda i: self.findings[i].severity):
            print(self._finding_line(self.findings[i]), file=out)
        print(file=out)   # blank line separates entry blocks
        return True

    def _entry_header(self, entry, status=None, progress="", uncited=False):
        """The block header -- the single identifying line per record:
          [i/N] key  @type  line N  STATUS (conf) -- detail   verify: <url>
        `progress` ('[i/N]', optional) leads it. STATUS is colored by pass/fail. For
        a clean VERIFIED 1.0 only the word shows; a caveat adds '(confidence X)';
        UNVERIFIED/MISMATCH add their cause. The DOI/url (when known) follows after a
        '; ' so no separate verify line is needed. `status` is the
        (status, confidence, detail) tuple recorded by set_status. When `uncited`,
        the line ends in a dim 'UNCITED in .tex; skipped' marker and nothing else."""
        bits = []
        if progress:
            bits.append(self._c(progress, _DIM))
        bits += [self._c(entry.key, _BOLD), self._c(f"@{entry.etype}", _DIM)]
        if entry.lineno:
            bits.append(self._c(f"line {entry.lineno}", _DIM))
        if uncited:
            bits.append(self._c("UNCITED in .tex source; skipped from further analysis", _DIM))
            return "  ".join(bits)
        if status:
            st, conf, detail = (status + ("", ""))[:3] if isinstance(status, tuple) else (status, None, "")
            color = _GREEN if st == "VERIFIED" else (
                SEVERITY_STYLE[Severity.ERROR][1] if st == "MISMATCH"
                else SEVERITY_STYLE[Severity.WARN][1])
            label = st
            if conf is not None and not (st == "VERIFIED" and conf >= 1.0):
                label += f" (confidence {conf:.2f})"
            piece = self._c(label, color)
            # Detail is shown only for UNVERIFIED/MISMATCH, which have no sibling
            # finding to explain them. A VERIFIED header carries just status +
            # confidence + the link: its caveat (if any) is already spelled out by
            # the metadata_mismatch / source_conflict finding right below, so
            # repeating it here ('-- resolved and consistent', '-- a field differs')
            # would be noise.
            if detail and st != "VERIFIED":
                piece += self._c(f" -- {detail}", _DIM)
            bits.append(piece)
        line = "  ".join(bits)
        if self.links.get(entry.key):
            # The DOI/url trails the status after '; ' (no 'verify:' label, no
            # separate line) -- the header is the single identifying+verifying line.
            line += self._c(f"; {_oneline(self.links[entry.key])}", _DIM)
        return line

    def _finding_line(self, f, with_key=False):
        """One formatted finding line. Inside an entry block the key lives in the
        header, so it is omitted here; the file-level group sets `with_key` to keep
        each line self-contained. Shape (stable for LLM/script parsing):
          [TAG] category (line N): message
        The whole finding is on ONE line -- _oneline() collapses any embedded
        whitespace/newline so a finding is never split across lines."""
        tag, color, _ = SEVERITY_STYLE[f.severity]
        # Pad to the width of the widest bracketed tag ("[ERROR]") outside the
        # color span, so the spaces are uncolored and the columns stay aligned.
        pad = " " * (len("[ERROR]") - len(tag) - 2)
        code = self._c(f.category or f.layer, _DIM)
        loc = f" (line {f.line})" if f.line else ""
        # The advisory '(suggested: X -> Y)' tail is derived from f.suggested, not
        # stored in the message, so the prose stays in lock-step with the JSON.
        msg = _oneline(f.message + format_suggested(f.suggested))
        keytag = (self._c(f.key, _BOLD) + " ") if with_key else ""
        return f"    {self._c(f'[{tag}]', color)}{pad} {keytag}{code}{loc}: {msg}"

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
            print(self._finding_line(self.findings[i], with_key=True), file=out)
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
                line = self._finding_line(f, with_key=True)
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

    def _render_integrity(self, s):
        """The Layer-6 verification roll-up appended to the summary."""
        bar = self._c("=" * 64, _DIM)
        score = s["integrity_score"]
        score_color = _GREEN if score >= 90 else (
            SEVERITY_STYLE[Severity.WARN][1] if score >= 70
            else SEVERITY_STYLE[Severity.ERROR][1])
        caveat = s.get("verified_with_caveat", 0)
        caveat_txt = f" ({caveat} with a caveat)" if caveat else ""
        return [
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
            f"  {self._c('integrity score:', _BOLD)} "
            f"{self._c(str(score) + '/100', score_color)}",
            bar,
        ]

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
        d = {"severity": f.severity.name, "layer": f.layer,
             "category": f.category, "group": finding_group(f.category),
             "key": f.key, "line": f.line, "message": f.message}
        if f.suggested:
            d["suggested"] = f.suggested
        return d

    def issues_for(self, key):
        """The live findings for one key as a list of issue dicts (the per-entry
        record's `issues`). Used by the NDJSON checkpoint, which stores each entry's
        findings inside the entry's own record rather than in a separate flat list."""
        return [self._finding_dict(f) for f in self.live_findings() if f.key == key]

    def to_json(self, summary=None, results=None, statuses=None, phases_by_key=None):
        """Machine-readable report. Always includes the flat `findings` list (for
        back-compat); each finding carries its `category`, the `group` that category
        rolls up into (syntax/semantic/context), and -- when the check proposes a
        concrete advisory edit -- a structured `suggested` ({field, from?, to}).
        When verification data is supplied, also emits a `summary` block (Layer 6)
        and a per-reference `references` array (Layer 8) with each reference's
        status, confidence, identifiers, canonical record and issues. `phases_by_key`
        (key -> set of phases computed) is persisted as each reference's `phases`
        so a later run can resume only the phases an entry still lacks."""
        from .checkpoint import canonical_record as _canonical_record  # lazy: cycle
        live = self.live_findings()
        out = {"findings": [self._finding_dict(f) for f in live]}
        if summary is not None:
            out["summary"] = summary
        if results is not None and statuses is not None:
            phases_by_key = phases_by_key or {}
            by_key = {}
            for f in live:
                by_key.setdefault(f.key, []).append(self._finding_dict(f))
            # Emit a reference per entry that was analyzed in ANY phase. Online
            # entries come from `results`; an offline-only entry has no resolution
            # but still records its `phases` (so a later online run can resume it),
            # appearing with a null status/record -- honest, not a fabricated verdict.
            # Order follows the analysis order (results first, then any offline-only
            # keys) so the array is stable across incremental rewrites.
            keys = list(results)
            keys += [k for k in phases_by_key if k not in results]
            refs = []
            for key in keys:
                res = results.get(key)
                status, conf = statuses.get(key, (None, None))
                rec = (res.record if res else None) or {}
                refs.append({
                    "key": key,
                    "status": status,
                    "confidence": conf,
                    "phases": sorted(phases_by_key.get(key, set())),
                    "verify": self.links.get(key) or None,
                    "identifiers": {"doi": (res.doi if res else "") or None,
                                    "arxiv": (res.arxiv_id if res else "") or None,
                                    "isbn": (res.isbn if res else "") or None},
                    "sources": sorted(res.sources) if res else [],
                    "canonical_record": _canonical_record(rec, conf),
                    "issues": by_key.get(key, []),
                })
            out["references"] = refs
        return out
