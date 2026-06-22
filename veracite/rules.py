"""Static checks as a registry, plus the syntax pass.

Each per-entry check is a function (entry, report) decorated with @rule; whole-
file checks use @file_rule(entries, report). The engine (run_static) runs every
registered rule. To add a check, write a function and decorate it -- this is the
part meant to be read and edited. The syntax pass runs before everything else so
a file that does not parse is never reported as healthy.
"""

import datetime
import re

from .config import SETTINGS
from .datamodel import (DM_ENTRYTYPES, FIELD_ALIASES, legal_fields,
                        mandatory_slots)
from .identifiers import isbn_valid, issn_valid, orcid_valid
from .normalize import (ARXIV_OLD_RE, DOI_FULL_RE, bare_doi, extract_arxiv_id,
                        is_book_series_doi, is_collaboration, is_preprint,
                        norm_pages, shouted_surnames, split_authors,
                        title_is_miscased)
from .parser import FIELD_DECL, _blank_comments, field_occurrences
from .report import Severity

ENTRY_RULES = []
FILE_RULES = []


def rule(fn):
    """Register a per-entry check `fn(entry, report)`."""
    ENTRY_RULES.append(fn)
    return fn


def file_rule(fn):
    """Register a whole-file check `fn(entries, report)`."""
    FILE_RULES.append(fn)
    return fn


# --- reference data shared by rules ----------------------------------------

MONTHS = {"jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"}


_MOJIBAKE = re.compile(r"�|Ã.|Â.|â.|Ä\x9f")

# Characters that are almost always copy-paste corruption rather than intended
# content: zero-width/invisible formatting marks, the BOM, the line/paragraph
# separators, and non-breaking / exotic spaces that masquerade as a plain space.
# Plain accented or non-Latin letters are NOT here -- under UTF-8 (biber, or
# bibtex with inputenc) they are valid and preferred, so they get no finding.
_SUSPICIOUS_CHARS = {
    " ": "no-break space",
    " ": "figure space",
    " ": "narrow no-break space",
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "⁠": "word joiner",
    "﻿": "byte-order mark / zero-width no-break space",
    "‎": "left-to-right mark",
    "‏": "right-to-left mark",
    " ": "line separator",
    " ": "paragraph separator",
    "­": "soft hyphen",
}


# --- per-entry rules -------------------------------------------------------

def _has_field(e, name):
    """True if the entry supplies `name` or a legacy alias of it (so 'journal'
    satisfies 'journaltitle', 'school' satisfies 'institution', etc.)."""
    if e.get(name).strip():
        return True
    # An alias maps a legacy name -> canonical; accept either direction.
    canon = FIELD_ALIASES.get(name, name)
    if e.get(canon).strip():
        return True
    return any(e.get(legacy).strip()
               for legacy, c in FIELD_ALIASES.items() if c == canon)


# Slots biblatex's raw datamodel marks mandatory but that real .bib usage handles
# in one of two ways (keyed by the *original* entry type, before alias resolution,
# so the thesis aliases are distinguished from a literal @thesis):
#
#   AUTO  -- the slot is satisfied IMPLICITLY by the entry-type alias itself, so a
#            missing field is correct and gets NO finding. '@phdthesis'/'@mastersthesis'
#            auto-supply type='PhD thesis'/'Master's thesis', and '@techreport'
#            type='technical report'; that is the whole reason these aliases exist
#            (vs the generic @thesis/@report, which DO need an explicit type).
#   NOTE  -- biber tolerates the field as absent, but it is genuinely missing data,
#            so it is worth an advisory note (not an error). E.g. '@incollection'
#            with no editor.
_AUTOSUPPLIED_SLOTS = {
    "phdthesis": ({"type"},),
    "mastersthesis": ({"type"},),
    "techreport": ({"type"},),
}
_OVERSTRICT_SLOTS = {
    "incollection": ({"editor"},),
}


def _slot_in(table, etype, slot):
    return any(set(slot) == entry for entry in table.get(etype, ()))


def _is_autosupplied_slot(etype, slot):
    """Whether a missing mandatory `slot` is implicitly supplied by the entry-type
    alias (so it is correct to omit it -- no finding at all)."""
    return _slot_in(_AUTOSUPPLIED_SLOTS, etype, slot)


def _is_overstrict_slot(etype, slot):
    """Whether a missing mandatory `slot` is one biber tolerates but is genuinely
    incomplete data (so it is a note, not an error)."""
    return _slot_in(_OVERSTRICT_SLOTS, etype, slot)


@rule
def required_fields(e, rep):
    # crossref/xref make an entry inherit fields from a parent, so a locally
    # "missing" required field may well be supplied there -- do not flag.
    if e.get("crossref").strip() or e.get("xref").strip():
        return
    # Required fields come straight from biblatex's datamodel constraints
    # (mandatory_slots), so this stays aligned with biber. A slot is satisfied by
    # any of its fields, after legacy-alias resolution (journal==journaltitle,
    # school==institution). DEFAULT (title only) covers a type with no constraint.
    for slot in mandatory_slots(e.etype) or [["title"]]:
        # The journaltitle slot for @article is handled below, where 'eprint'
        # (an arXiv preprint) is accepted as an alternative -- skip it here to
        # avoid flagging a preprint twice.
        if e.etype == "article" and slot == ["journaltitle"]:
            continue
        if not any(_has_field(e, f) for f in slot):
            shown = slot[0] if len(slot) == 1 else " or ".join(slot)
            # 'type' for @phdthesis/@mastersthesis/@techreport is auto-supplied by
            # the alias (that is why they exist), so a missing 'type' is correct --
            # no finding at all. Other tolerated-but-incomplete slots (e.g. an
            # @incollection with no editor) are an advisory note, not an error. Only
            # a genuinely required-and-missing field is an error.
            if _is_autosupplied_slot(e.etype, slot):
                continue
            if _is_overstrict_slot(e.etype, slot):
                rep.add(Severity.INFO, e, f"biblatex's datamodel lists '{shown}' as "
                        f"mandatory for @{e.etype}; consider adding it",
                        category="datamodel_recommended")
            else:
                rep.add(Severity.ERROR, e, f"missing required field '{shown}' for "
                        f"@{e.etype}", category="missing_field")
    # biber does not mandate a date, but a reference without one is hard to use --
    # flag it as a recommendation (warning), explicitly beyond the biber datamodel.
    if e.etype not in ("misc", "online", "software", "dataset") \
            and not _has_field(e, "year") and not _has_field(e, "date"):
        rep.add(Severity.WARN, e, "no 'year' or 'date' (recommended; biblatex does "
                "not require it, but a reference needs a date to be usable)",
                category="missing_recommended")

    if e.etype == "article":
        if not _has_field(e, "journal") and not e.get("eprint").strip():
            rep.add(Severity.ERROR, e, "missing 'journal'/'journaltitle' (or eprint) "
                    "for @article", category="missing_field")
        elif not is_preprint(e):
            # A published article should be locatable. 'pages' OR an article
            # number (eid/number/article-number) satisfies that; volume too.
            locatable = any(e.get(f).strip() for f in
                            ("pages", "eid", "number", "articleno", "article-number"))
            has_volume = e.get("volume").strip()
            # An @article that carries an ISBN (or a book-series DOI) is a chapter
            # in a book/proceedings volume mis-typed as a journal article, not a
            # web/press item -- so point at @incollection/@inproceedings, the
            # correct container type, rather than @online/@misc.
            if e.get("isbn").strip() or is_book_series_doi(e.get("doi", "")):
                rep.add(Severity.WARN, e, "@article carries an ISBN/book-series DOI; it "
                        "looks like a chapter in a book or proceedings volume -- use "
                        "@incollection or @inproceedings instead of @article",
                        category="entrytype_suggestion")
            # An @article with no locator and no volume but a url/howpublished or a
            # corporate author is almost always a web/press item mis-typed as
            # @article -- point at the entry type, the real fix.
            elif not locatable and not has_volume and (
                    e.get("url").strip() or e.get("howpublished").strip()
                    or is_collaboration(e.get("author", ""))):
                rep.add(Severity.WARN, e, "@article has no volume, pages or article "
                        "number and looks like a web/press item; use @online or "
                        "@misc instead", category="entrytype_suggestion")
            else:
                # Missing volume/pages on a published article is NOT invalid
                # BibTeX (biblatex builds it fine) -- it is a locator worth
                # adding, so a warning, not an error. Its own category keeps it
                # off the error-level 'missing_field' floor.
                if not has_volume:
                    rep.add(Severity.WARN, e, "published article missing 'volume'",
                            category="missing_locator")
                if not locatable:
                    rep.add(Severity.WARN, e, "published article missing 'pages' (or an "
                            "article number / eid)", category="missing_locator")


@rule
def duplicate_field(e, rep):
    """A field declared twice in one entry. BibTeX/biblatex silently keep just one
    value, so the rest are dropped from the compiled bibliography. When the repeats
    carry the SAME value nothing is lost -- a note. When they carry DIFFERENT values
    the output silently loses data (e.g. two different 'pages'), a real risk of
    contaminating the bibliography -- a warning."""
    # The body is e.raw minus the '@type{' wrapper and the closing '}', matching
    # what the parser feeds _parse_body, so field_occurrences scans it correctly.
    body = e.raw[e.raw.find("{") + 1: e.raw.rfind("}")]
    for fld, vals in field_occurrences(body).items():
        if len(vals) <= 1:
            continue
        distinct = {" ".join(v.split()).lower() for v in vals}
        if len(distinct) > 1:
            rep.add(Severity.WARN, e, f"field '{fld}' appears {len(vals)} times with "
                    "different values; BibTeX keeps only one and silently drops the "
                    "rest -- consolidate into a single field",
                    category="duplicate_field_conflict", field=fld)
        else:
            rep.add(Severity.INFO, e, f"field '{fld}' appears {len(vals)} times; BibTeX "
                    "keeps only one value (harmless -- the values agree, but remove the "
                    "extras)", category="duplicate_field", field=fld)


@rule
def entry_type_known(e, rep):
    """An entry type not in the biblatex datamodel (with legacy BibTeX types
    aliased) is almost always a typo (e.g. '@artical') that BibTeX will not
    process -- a structural error, not a stylistic one."""
    if e.etype not in DM_ENTRYTYPES:
        rep.add(Severity.ERROR, e, f"unknown entry type '@{e.etype}'; not a BibTeX/"
                f"biblatex type (likely a typo)", "syntax", category="syntax")


@rule
def biblatex_field_validity(e, rep):
    """Fields the biblatex datamodel does not allow for this entry type. Validity
    is derived from the standard datamodel (universal + per-type fields), with
    legacy BibTeX field/type names resolved through aliases. An unknown entry type
    is not flagged (no datamodel to judge against)."""
    if e.etype not in DM_ENTRYTYPES:
        return   # unknown type already reported by entry_type_known
    legal = legal_fields(e.etype)
    if not legal:
        return   # entry type unknown to the datamodel; cannot judge fields
    invalid = []
    for f in sorted(e.fields):
        if not e.get(f).strip():
            continue
        canonical = FIELD_ALIASES.get(f, f)
        if canonical in legal or f in legal:
            continue
        if f == "journal" and "arxiv" in e.get("journal", "").lower():
            continue   # arXiv-in-journal handled by arxiv_consistency
        invalid.append(f)
    if not invalid:
        return
    # One finding per entry, not one per field: an entry that carries several
    # foreign fields (e.g. ADS exports with adsurl/adsnote/...) would otherwise
    # flood the log with near-identical notes. List every offending field and its
    # line so a correction tool still has each location, anchored at the first.
    by_line = sorted(invalid, key=e.field_line)
    if len(by_line) == 1:
        f = by_line[0]
        msg = (f"field '{f}' is not valid for @{e.etype} in the biblatex "
               f"datamodel; it is dropped by biblatex")
    else:
        named = ", ".join(f"'{f}' (line {e.field_line(f)})" for f in by_line)
        msg = (f"{len(by_line)} fields are not valid for @{e.etype} in the "
               f"biblatex datamodel and are dropped by biblatex: {named}")
    rep.add(Severity.WARN, e, msg, category="biblatex_validity", field=by_line[0])


@rule
def legacy_month(e, rep):
    """A month should be the bare three-letter macro (month = jun) or an integer:
    biblatex resolves and localizes those and sorts them correctly. Only flag the
    forms it cannot use as a month -- a *braced/quoted* value (the delimiters stop
    macro expansion, so 'month = {jun}' is an opaque string) or a spelled-out name
    ('month = {June}'). A bare 'jun' or '6' is canonical and produces no finding.

    The parsed field value is authoritative for the month *text* (the parser has
    already split off any sibling field that shared the line, e.g. 'month=sep,
    pages={327}'); e.raw is consulted only to learn whether that value was
    brace/quote-delimited, since the parser strips those delimiters."""
    val = e.get("month").strip()
    if not val:
        return
    bare = val.lower().rstrip(".")
    if bare.isdigit():
        return                                    # integer: fine
    abbr = bare[:3]
    if abbr not in MONTHS:
        return                                    # not a month name -- nothing to say
    # Was the field delimited in the source? Look at the field's own line only,
    # and at just the start of the value, so a following field on the same line
    # cannot be mistaken for a delimiter.
    m = re.search(r"(?im)^\s*month\s*=\s*(\S)", e.raw)
    delimited = bool(m) and m.group(1) in "{\""
    if bare in MONTHS and not delimited:
        return                                    # 'month = jun' -- canonical macro
    shown = ("{" + val + "}") if delimited else val
    rep.add(Severity.INFO, e,
            f"month {shown!r} is not a bare month macro; biblatex will not "
            f"sort/localize it", category="style", field="month",
            suggested={"field": "month", "from": shown, "to": abbr})


# A value that is meant to be a year: a bare 4-digit run (optionally with a
# biblatex date tail like '-05' or '/2021'). 'in press', 'forthcoming', 'n.d.'
# and similar non-numeric placeholders are legitimate and deliberately NOT matched
# here -- only a value that *contains digits* but no sane 4-digit year is flagged.
_YEAR_RE = re.compile(r"\b(\d{4})\b")


@rule
def year_sanity(e, rep):
    """A 'year'/'date' value that carries digits but no plausible 4-digit year is
    almost always a typo ('20201', '0218', a stray range). Non-numeric placeholders
    ('in press', 'forthcoming') are left alone -- they are valid, intentional, and
    handled elsewhere; only a malformed numeric year is a transcription error."""
    next_year = datetime.date.today().year + 5
    for field in ("year", "date"):
        val = e.get(field, "").strip()
        if not val or not any(c.isdigit() for c in val):
            continue   # empty, or a non-numeric placeholder we do not police here
        years = [int(y) for y in _YEAR_RE.findall(val)]
        plausible = [y for y in years if 1500 <= y <= next_year]
        if not plausible:
            rep.add(Severity.WARN, e, f"{field} {val!r} has no plausible 4-digit year "
                    f"(1500-{next_year}); likely a typo", category="identifier_format",
                    field=field)
        # Only police 'year' for the 4-digit shape; 'date' legitimately carries a
        # month/day tail ('2020-05-01'), so a clean year embedded there is fine.
        elif field == "year" and not re.fullmatch(r"\d{4}", val) \
                and not re.fullmatch(r"\d{4}\s*[-/]\s*\d{4}", val):
            rep.add(Severity.INFO, e, f"year {val!r} is not a bare 4-digit year; "
                    f"biblatex sorts/derives the date from a clean year",
                    category="style", field=field,
                    suggested={"field": field, "from": val, "to": plausible[0]})


# Acronym scan (2+ capitals not adjacent to a brace/backslash). Compiled once.
_ACRONYM_RE = re.compile(r"(?<![\\{])\b([A-Z]{2,})\b(?![}])")

# Per-term (occurrence, protected?) patterns for the configured protected_terms,
# compiled once and cached: the list is fixed for a run, so rebuilding a pair of
# re.escape patterns per term *per entry* (title_caps is one of the hottest rules)
# is pure waste. Keyed on the list's identity so a settings reload rebuilds it.
_PROTECTED_CACHE = {}


def _protected_term_patterns():
    terms = SETTINGS.get("protected_terms", [])
    key = id(terms)
    cached = _PROTECTED_CACHE.get(key)
    if cached is None or cached[0] != terms:
        compiled = [(hint,
                     re.compile(rf"(?<!\{{)\b{re.escape(hint)}\b(?!\}})"),
                     re.compile(rf"\{{[^}}]*{re.escape(hint)}[^}}]*\}}"))
                    for hint in terms]
        cached = (list(terms), compiled)
        _PROTECTED_CACHE.clear()        # only the current settings list is useful
        _PROTECTED_CACHE[key] = cached
    return cached[1]


def _protected_in_braces(word, title):
    """Whether `word` already sits inside a brace group in `title`."""
    return re.search(rf"\{{[^}}]*{re.escape(word)}[^}}]*\}}", title)


@rule
def title_caps(e, rep):
    """Flag proper nouns/acronyms in a title that are not brace-protected and so
    may be lowercased by some bibliography styles. The protected-term list is
    project-configurable via the `protected_terms` setting.

    A wholly miscased (SHOUTED all-caps) title is a different defect with a
    different fix -- the whole title needs recasing, not word-by-word brace
    protection -- so it gets ONE 'convert to title case' note and the per-word
    acronym scan is skipped (every word would otherwise be flagged as an acronym).
    The record layer refines this to 'adopt the record's casing' when online."""
    title = e.get("title", "")
    if not title:
        return
    if title_is_miscased(title):
        rep.add(Severity.INFO, e, "title looks miscased (mostly UPPERCASE); convert "
                "to title/sentence case (and brace-protect any genuine acronyms)",
                category="title_case", field="title")
        return
    for hint, occ_re, prot_re in _protected_term_patterns():
        if occ_re.search(title) and not prot_re.search(title):
            rep.add(Severity.WARN, e, "title term not brace-protected; may be lowercased "
                    "by some styles", category="style", field="title",
                    suggested={"field": "title", "from": hint, "to": "{" + hint + "}"})
    for m in _ACRONYM_RE.finditer(title):
        word = m.group(1)
        if word not in ("A", "I") and not _protected_in_braces(word, title):
            rep.add(Severity.INFO, e, "acronym in title not brace-protected",
                    category="style", field="title",
                    suggested={"field": "title", "from": word, "to": "{" + word + "}"})


@rule
def title_punctuation(e, rep):
    t = e.get("title", "").strip()
    if t.endswith(".") and not t.endswith(".."):
        # The suggestion carries the full title (from -> to); the renderer previews
        # long values for the screen, so the prose stays readable and the JSON exact.
        rep.add(Severity.INFO, e, "title ends with a period; usually dropped in references",
                category="style", field="title",
                suggested={"field": "title", "from": t, "to": t[:-1]})


@rule
def truncated_authors(e, rep):
    """A name list that has been truncated rather than stored in full. Two forms,
    both flagged so the .bib keeps complete author data:

    * a literal 'et al.' -- worst: 'et al.' is a *publisher rendering*, not data.
      BibTeX/biblatex treat the spelled-out form as a real author and it bakes one
      journal's convention into the .bib. The fix is the 'and others' marker.
    * the 'and others' marker itself -- valid, and the style WILL render it, but it
      discards the dropped names. A style that must list the first N authors before
      'et al.' cannot recover names the .bib never stored. For good record-keeping
      the full list belongs in the .bib; the style applies journal-specific
      truncation (maxnames/maxbibnames) at format time."""
    for field in ("author", "editor"):
        val = e.get(field, "")
        # 'et al.' in any of its written forms: a space, a LaTeX tie 'et~al.', or
        # the run-together 'et.al.' -- all hard-code a rendering into the .bib.
        if re.search(r"\bet[\s~.]+al\.?", val, re.I):
            rep.add(Severity.WARN, e, f"{field} list contains a literal 'et al.'; "
                    "BibTeX treats it as an author and it hard-codes a journal's "
                    "rendering -- store the full author list and use 'and others' "
                    "if a marker is needed (the style produces 'et al.')",
                    category="author_completeness", field=field)
        elif re.search(r"\band\s+others\b", val, re.I):
            rep.add(Severity.WARN, e, f"{field} list is truncated with 'and others'; "
                    "valid, but the dropped names are lost -- store the full list so "
                    "the style can apply journal-specific truncation",
                    category="author_completeness", field=field)


# A name-separator 'and' fused to the preceding initial with no space:
# 'Pientka, F.and Peng, Y.' (a dropped space after 'F.'). BibTeX then reads
# 'F.and' as the given name and never sees the following author as separate, so
# this surfaces downstream as both a 'given name differs' and a 'missing author'.
# Anchored to an initial+period (or a lone capital) directly before 'and' + a
# capitalized next name, so an ordinary surname containing 'and' (Anderson,
# Brandt) is not matched.
_GLUED_AND_RE = re.compile(r"(?:^|[\s,])([A-Z]\.?and)\s+[A-Z]")


@rule
def glued_and_separator(e, rep):
    """The ' and ' author separator glued to the preceding initial ('F.and Peng').
    Reported as one delimiter error, since otherwise it splinters into unrelated
    'given name differs' and 'missing author' findings against the record."""
    for field in ("author", "editor"):
        m = _GLUED_AND_RE.search(e.get(field, ""))
        if m:
            rep.add(Severity.WARN, e, f"{field} list has 'and' fused to a name with no "
                    f"space ({m.group(1)!r}); BibTeX reads it as one author and drops "
                    f"the separator -- add a space (e.g. 'F. and')",
                    category="author_format", field=field)


@rule
def shouted_authors(e, rep):
    """Surnames written in ALL-CAPS ('CHEN, Q. and ZHANG, X.'), a publisher export
    convention rather than data. Reported once per field listing the shouted names,
    so the .bib stores the canonical 'Chen'/'Zhang' casing -- the same class of
    defect as a SHOUTED title."""
    for field in ("author", "editor"):
        shouted = shouted_surnames(e.get(field, ""))
        if len(shouted) >= 2 or (shouted and len(split_authors(e.get(field, ""))) == 1):
            shown = ", ".join(shouted[:6]) + (" ..." if len(shouted) > 6 else "")
            rep.add(Severity.INFO, e, f"{field} surname(s) in ALL-CAPS ({shown}); use "
                    f"normal name casing (publishers SHOUT names on export; the .bib "
                    f"should store the canonical form)", category="author_format",
                    field=field)


@rule
def encoding(e, rep):
    if _MOJIBAKE.search(e.raw):
        bad = sorted({m.group(0) for m in _MOJIBAKE.finditer(e.raw)})
        rep.add(Severity.ERROR, e, "looks like mis-encoded text (mojibake): " + " ".join(bad[:6]),
                category="encoding")
        return
    # Plain non-ASCII letters (Loïc, Erdős, ...) are valid UTF-8 and the modern
    # preferred form, so they are not flagged. Only characters that are almost
    # always copy-paste corruption -- invisible/zero-width marks and spaces that
    # impersonate a plain space -- get a note, since they silently break sorting,
    # matching, or spacing without being visible in an editor.
    found = {c for c in e.raw if c in _SUSPICIOUS_CHARS}
    if found:
        names = sorted(f"{_SUSPICIOUS_CHARS[c]} (U+{ord(c):04X})" for c in found)
        rep.add(Severity.INFO, e, "suspicious invisible/non-standard character(s): "
                + "; ".join(names[:6]) + "; likely copy-paste corruption -- delete or "
                "replace with the intended ASCII character", category="encoding")


@rule
def page_dashes(e, rep):
    pages = e.get("pages", "")
    if ("–" in pages or "—" in pages) and "--" not in pages:
        fixed = re.sub(r"\s*[–—]\s*", "--", pages)
        rep.add(Severity.WARN, e, "page range uses a literal en/em dash",
                category="style", field="pages",
                suggested={"field": "pages", "from": pages, "to": fixed})
    # A single hyphen between two page numbers ('10-20') is not the canonical
    # biblatex range separator '--'; the style renders it as a hyphen, not an
    # en-dash. Only fire on a clean digit-hyphen-digit range (an article number or
    # a single page is left alone), and not when an en/em dash was already flagged.
    elif re.fullmatch(r"\s*\w?\d+\s*-\s*\w?\d+\s*", pages) and "--" not in pages:
        fixed = re.sub(r"\s*-\s*", "--", pages.strip())
        rep.add(Severity.INFO, e, "page range uses a single hyphen; biblatex's range "
                "separator is '--'", category="style", field="pages",
                suggested={"field": "pages", "from": pages.strip(), "to": fixed})


@rule
def page_sanity(e, rep):
    pages = e.get("pages", "").strip()
    if not pages:
        return
    p = norm_pages(pages)
    m = re.match(r"^(\d+)-(\d+)$", p)
    if m and int(m.group(2)) < int(m.group(1)):
        rep.add(Severity.WARN, e, f"page range descends: {pages}", category="style", field="pages")
        return
    if m or "-" in p:
        return
    # A single page or a modern article id is a valid locator, not "unusual":
    # a leading-letter page ('L123', 'S45'), or a journal article id that is
    # letters-then-digits, optionally with a trailing letter or digits
    # ('eaam9288', 'staf1642', 'rspa20090232', 'psaf050', 'e0123456', '012345').
    # Only a value that is NOT a recognizable locator (e.g. 'pp.', 'in press',
    # 'ix, 277 p.', 'arXiv:...') is flagged.
    if re.fullmatch(r"[A-Za-z]*\d+[A-Za-z]?", p):
        return
    rep.add(Severity.INFO, e, f"unusual pages value: {pages!r}", category="style", field="pages")


@rule
def numpages_agreement(e, rep):
    np = e.get("numpages", "").strip()
    pg = norm_pages(e.get("pages", ""))
    if not (np.isdigit() and re.match(r"^\d+-\d+$", pg)):
        return
    a, b = (int(x) for x in pg.split("-"))
    if b - a + 1 != int(np):
        rep.add(Severity.WARN, e, f"numpages={np} disagrees with page range "
                f"{e.get('pages')} (= {b - a + 1} pages)", category="style", field="numpages")


@rule
def arxiv_consistency(e, rep):
    if not is_preprint(e):
        return
    j, doi, eprint = e.get("journal", ""), e.get("doi", ""), e.get("eprint", "")
    id_journal = extract_arxiv_id(j)
    id_doi = extract_arxiv_id(doi) if "arxiv" in doi.lower() else None
    id_eprint = extract_arxiv_id(eprint)
    ids = {x for x in (id_journal, id_doi, id_eprint) if x}
    if len(ids) > 1:
        rep.add(Severity.ERROR, e, "inconsistent arXiv identifiers across fields: "
                + ", ".join(sorted(ids))
                + f"  (journal={id_journal}, doi={id_doi}, eprint={id_eprint})",
                category="identifier_format")
    if doi and "arxiv" in doi.lower():
        m = re.search(r"arXiv\.([\d.]+|\S+/\d+)", doi, re.I)
        if m and id_journal and m.group(1).rstrip(".") != id_journal:
            rep.add(Severity.ERROR, e, f"arXiv DOI ({m.group(1)}) does not match "
                                       f"journal field ({id_journal})",
                    category="identifier_format")
    if "arxiv" in j.lower() and not re.match(r"^arXiv:\d{4}\.\d{4,5}$", j.strip()) \
            and not ARXIV_OLD_RE.search(j):
        rep.add(Severity.INFO, e, f"arXiv journal field not canonical 'arXiv:XXXX.XXXXX': {j!r}",
                category="style", field="journal")


@rule
def doi_format(e, rep):
    doi = e.get("doi", "").strip()
    if not doi:
        return
    bare = bare_doi(doi)
    _doi_shaped = DOI_FULL_RE.match(bare)
    # 'doi field contains a URL; use the bare DOI' only makes sense when stripping a
    # doi.org wrapper actually yields a bare, DOI-shaped value. If stripping changes
    # nothing (an arXiv link or a mangled non-doi.org string in the doi field), the
    # value is not a URL-wrapped DOI -- suggesting 'X -> X' is a useless no-op, so
    # fall through to the accurate 'does not match 10.xxxx/...' diagnosis below.
    if (doi.lower().startswith("http") or "doi.org" in doi.lower()) \
            and bare != doi and _doi_shaped:
        rep.add(Severity.INFO, e, "doi field contains a URL; bare DOI preferred",
                category="style", field="doi",
                suggested={"field": "doi", "from": doi, "to": bare})
    elif not DOI_FULL_RE.match(doi):
        # The DOI syntax is '10.' + a registrant code (digits, dot-separated) +
        # '/' + suffix. The registrant is usually 4-5 digits but the spec only
        # requires it be non-empty, so don't reject a short or sub-divided prefix.
        rep.add(Severity.WARN, e, f"DOI does not match 10.xxxx/... pattern: {doi!r}",
                category="identifier_format", field="doi")
    elif re.search(r"[.,;]$", doi):
        # A trailing sentence period/comma/semicolon is almost always a copy-paste
        # artifact (DOIs may technically end in '.', but it is vanishingly rare and
        # the value will not resolve as written). A ')' is left alone -- balanced
        # parens do occur in real DOI suffixes.
        fixed = doi.rstrip(".,;")
        rep.add(Severity.WARN, e, "DOI ends with stray punctuation (likely a "
                "copy-paste artifact; will not resolve as written)",
                category="identifier_format", field="doi",
                suggested={"field": "doi", "from": doi, "to": fixed})


@rule
def identifier_formats(e, rep):
    """Validate the check digits of ISBN/ISSN/ORCID fields when present. A bad
    check digit is a transcription error -- the identifier will not resolve."""
    for field, label, ok in (("isbn", "ISBN", isbn_valid),
                             ("issn", "ISSN", issn_valid),
                             ("orcid", "ORCID", orcid_valid)):
        val = e.get(field, "").strip()
        # A field may carry several ids (e.g. print + online ISSN) separated by
        # ',', ';' or whitespace; each must be valid.
        for part in re.split(r"[;,\s]+", val):
            if not part or ok(part):
                continue
            # A journal ISSN dropped into the isbn field is a recurring mistake
            # (Nature, Nano Lett., ...). It fails the ISBN check digit, but it is
            # not a typo'd ISBN -- it is a valid ISSN in the wrong field. Say so,
            # rather than "ISBN fails its check digit", which misdiagnoses it.
            if field == "isbn" and issn_valid(part):
                rep.add(Severity.WARN, e, f"value in 'isbn' looks like an ISSN, not "
                        f"an ISBN -- move it to the 'issn' field: {part!r}",
                        category="identifier_format", field=field)
                continue
            # The mirror case: a valid ISBN sitting in the issn field.
            if field == "issn" and isbn_valid(part):
                rep.add(Severity.WARN, e, f"value in 'issn' looks like an ISBN, not "
                        f"an ISSN -- move it to the 'isbn' field: {part!r}",
                        category="identifier_format", field=field)
                continue
            rep.add(Severity.WARN, e, f"{label} fails its check digit (likely a "
                    f"typo): {part!r}", category="identifier_format", field=field)


# --- file-wide rules -------------------------------------------------------

@file_rule
def duplicate_keys_and_dois(entries, rep):
    seen_keys, seen_dois = {}, {}
    for e in entries:
        if e.key in seen_keys:
            rep.add(Severity.ERROR, e, f"duplicate citation key (also at line {seen_keys[e.key]})",
                    category="duplicate")
        else:
            seen_keys[e.key] = e.lineno
        # Compare DOIs in their bare, lowercased form so a URL-wrapped DOI
        # ('https://doi.org/10.1/x') and the same DOI written bare ('10.1/x') --
        # a very common mix within one .bib -- are recognized as the same work.
        doi = bare_doi(e.get("doi", "").strip()).lower()
        if doi:
            if doi in seen_dois:
                rep.add(Severity.WARN, e, f"DOI shared with '{seen_dois[doi]}': {doi}",
                        category="duplicate")
            else:
                seen_dois[doi] = e.key


def _is_personal_name_token(a):
    """Whether an author token is a personal name (vs a corporate/collaboration
    name). A brace-wrapped single unit ('{LIGO Scientific Collaboration}',
    '{SciPy 1.0 Contributors}') or a collaboration marker is NOT a 'First Last'
    personal name -- it is one organizational name that legitimately carries no
    comma and several words, so it must not trip the mixed-format check. A real
    multi-word surname is always written WITH a comma ('{van der Walt}, S.'), so
    excluding brace-wrapped comma-less tokens removes only organizations."""
    s = a.strip()
    if s.startswith("{") and s.endswith("}"):
        return False
    return not is_collaboration(s)


@file_rule
def consistent_author_format(entries, rep):
    # 'others' (the biblatex 'and others' marker) and a literal 'et al.' are
    # completeness markers, not names -- 'et al.' has no comma and two words, so
    # left in it would read as a bogus 'First Last' name and fabricate a
    # mixed-format finding on an otherwise uniform 'Last, First' list. (The literal
    # 'et al.' is already surfaced by the author_completeness rule.)
    _markers = ("others", "et al.", "et al", "et~al.", "et~al")
    fmt_count = {}
    for e in entries:
        names = [a.strip() for a in re.split(r"\s+and\s+", e.get("author", "").replace("\n", " "))
                 if a.strip() and a.strip().lower() not in _markers]
        if not names:
            continue
        has_comma = any("," in a for a in names)
        # Only PERSONAL names count toward the 'First Last' form -- a corporate or
        # collaboration author (a braced group) carries no comma but is not a
        # mixed-format personal name, so it must not falsely flag a uniform list.
        has_space = any("," not in a and len(a.split()) > 1
                        for a in names if _is_personal_name_token(a))
        if has_comma and has_space:
            rep.add(Severity.INFO, e, "author field mixes 'Last, First' and 'First Last' forms",
                    category="author_format")
        else:
            fmt_count.setdefault("comma" if has_comma else "space", []).append(e.key)
    if len(fmt_count) > 1:
        summary = "; ".join(f"{k}: {len(v)}" for k, v in sorted(fmt_count.items()))
        rep.add_file(Severity.INFO, f"author name format is inconsistent across the file "
                                    f"({summary}). Pick one convention.", category="author_format")


@file_rule
def consistent_arxiv_style(entries, rep):
    styles = {}
    for e in entries:
        if not is_preprint(e):
            continue
        if e.get("eprint").strip():
            s = "eprint"
        elif "arxiv" in e.get("journal", "").lower():
            s = "journal"
        elif "arxiv" in e.get("doi", "").lower():
            s = "doi-only"
        else:
            s = "other"
        styles.setdefault(s, []).append(e.key)
    if len(styles) > 1:
        summary = "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(styles.items()))
        rep.add_file(Severity.INFO, f"arXiv preprints are encoded inconsistently "
                                    f"({summary}). Prefer one style (e.g. eprint).", category="style")


# --- syntax pass + engine --------------------------------------------------

def syntax_pass(raw, entries, problems, rep):
    """SYNTAX layer: structural validity, before any field/record checks. A file
    that does not parse must not be reported as healthy. Surfaces the parser's
    per-entry structural problems, missing field '=' separators, and a file-level
    brace-balance check. Returns the set of entry keys with a structural error so
    the caller can skip online record comparison for an entry that did not parse
    cleanly (a garbled parse produces spurious author/field mismatches)."""
    broken = set()
    for lineno, msg in problems:
        # A field 'outside the entry' parses fine -- BibTeX just drops the stray
        # field -- so it is a data-loss WARNING, not a parse-breaking error, and the
        # entry is NOT marked broken (its own fields are intact).
        if "outside the entry" in msg:
            rep.add(Severity.WARN, ("<file>", lineno), msg, "syntax",
                    category="dropped_field")
            continue
        # A header-less block (an entry whose '@type{key,' line was deleted) is a
        # structural error: its fields are dropped and its closing brace unbalances
        # the file. Its own category links it to the brace-imbalance finding the
        # same defect produces, so the user fixes one cause, not three symptoms.
        if "unlabelled block" in msg:
            rep.add(Severity.ERROR, ("<file>", lineno), msg, "syntax",
                    category="missing_entry_header")
            continue
        rep.add(Severity.ERROR, ("<file>", lineno), msg, "syntax", category="syntax")
        # The parser embeds the offending key as '@type{key}:' -- capture it so the
        # caller can skip online comparison for an entry that did not parse cleanly.
        km = re.match(r"@\w+\{([^}]+)\}", msg)
        if km:
            broken.add(km.group(1))

    # Per-entry: a field declaration that opens its value with '{' or '"' but has
    # no '=' (e.g. 'title {...}') is a structural error BibTeX rejects. Only a
    # declaration at the TOP LEVEL of the entry body counts -- an identifier
    # followed by '{' INSIDE a field value (e.g. a multi-line braced author list
    # '... and {Brett}, Matthew ...') is part of the value, not a new field, and
    # BibTeX accepts it. So the match must sit at brace depth 0 within e.raw.
    for e in entries:
        for m in FIELD_DECL.finditer(e.raw):
            if m.group(2) not in "{\"":
                continue
            prefix = e.raw[:m.start()]
            depth = (prefix.count("{") - prefix.count("\\{")
                     - prefix.count("}") + prefix.count("\\}"))
            # depth 1 == inside the entry's own braces but not inside a field value.
            if depth != 1:
                continue
            fld = m.group(1).lower()
            line = e.lineno + prefix.count("\n")
            rep.add(Severity.ERROR, ("<file>", line),
                    f"@{e.etype}{{{e.key}}}: field '{fld}' is missing its '=' "
                    f"separator (structural BibTeX error)", "syntax", category="syntax")
            broken.add(e.key)

    # File-level brace balance: a reliable, parser-independent signal that the
    # file is structurally broken even if resync recovered some entries. Braces
    # inside '%' line comments are blanked first, so a deliberately commented-out
    # entry does not register as an imbalance (biber ignores comment text).
    counted = _blank_comments(raw)
    opens = counted.count("{") - counted.count("\\{")
    closes = counted.count("}") - counted.count("\\}")
    if opens != closes:
        d = opens - closes
        where = f"{abs(d)} unclosed '{{'" if d > 0 else f"{abs(d)} unmatched '}}'"
        rep.add_file(Severity.ERROR, f"brace imbalance across file: {where} "
                     f"(BibTeX will not parse this)", "syntax", category="syntax")
    return broken


def run_entry_rules(e, rep):
    """Apply every registered per-entry @rule to one entry. The per-entry driver
    calls this so an entry's offline findings are produced alongside its online
    ones (one pass, in bibtex order)."""
    for fn in ENTRY_RULES:
        fn(e, rep)


def run_file_rules(entries, rep):
    """Apply every registered whole-file @file_rule once (duplicate keys/DOIs,
    author-format and arXiv-style consistency). Run after the per-entry pass."""
    for fn in FILE_RULES:
        fn(entries, rep)


def run_static(entries, rep):
    """STATIC layer: apply every registered rule to the entries (per-entry rules
    then file-wide rules). Kept as the all-entries entry point used by tests."""
    for e in entries:
        run_entry_rules(e, rep)
    run_file_rules(entries, rep)
