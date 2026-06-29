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
from .normalize import (ARXIV_OLD_RE, DOI_FULL_RE, bare_doi, clean_tex,
                        extract_arxiv_id, extract_doi_from_url, extract_isbn,
                        has_etal_marker, is_book_series_doi, is_collaboration,
                        is_preprint, norm_pages, shouted_surnames, split_authors,
                        title_is_miscased)
from .parser import _blank_comments, field_occurrences, iter_field_decls
from .report import Severity
from .titles import title_key

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

def _nonempty(val):
    """True if a field value has any real content. A field is present when it holds
    ANY non-empty character once brace and whitespace wrapping is removed -- only a
    genuinely blank value ('{ }', '{}', '  ', '{{ }}') counts as missing.

    Crucially we strip ONLY braces and whitespace, NOT TeX macros: a value that is an
    unexpanded control sequence (e.g. a journal given as the AASTeX macro
    'journal={\\pra}', or any other publisher's shorthand) renders to a real journal
    name and so is PRESENT -- de-TeXing it away would make a sound entry look like it
    is missing its journal and fire a false 'missing_field' error. Whether such a
    macro is the *right* venue, or portable, is a separate (softer) question; for
    presence, any character is enough."""
    return bool(val.replace("{", "").replace("}", "").strip())


def _has_field(e, name):
    """True if the entry supplies `name` or a legacy alias of it (so 'journal'
    satisfies 'journaltitle', 'school' satisfies 'institution', etc.). A field whose
    content is only braces/whitespace does NOT count as supplied."""
    if _nonempty(e.get(name)):
        return True
    # An alias maps a legacy name -> canonical; accept either direction.
    canon = FIELD_ALIASES.get(name, name)
    if _nonempty(e.get(canon)):
        return True
    return any(_nonempty(e.get(legacy))
               for legacy, c in FIELD_ALIASES.items() if c == canon)


# URL path segments that mark a press release / news / grey-literature page rather
# than a journal article, plus a bare document file (a slide deck or PDF served off
# a personal/corporate site). These are positive, low-false-positive web-item
# signals: a real article's URL points at a publisher/DOI landing page, not a
# '/newsroom/' path or a raw .pdf. A publisher PDF still lives under a DOI/host
# path, so requiring the .pdf to be the LAST segment keeps 'doi.org/.../paper.pdf'
# style links (rare) from over-matching while catching '.../speech/Name_3.pdf'.
_WEB_SOURCE_RE = re.compile(
    r"/(?:newsroom|news|press|press-release|blog|events?|media|"
    r"announcements?|stories|insights|case-stud(?:y|ies)|tutorials?|"
    r"docs?|getting[-_]started)/", re.I)
_WEB_DOC_RE = re.compile(r"\.(?:pdf|pptx?|key)(?:[?#].*)?$", re.I)
# Well-known BLOG / grey-literature HOSTS -- never journals, so an @article pointing
# at one is mis-typed regardless of any 'journal' label it carries (a Medium post
# with journal={Medium} is still a blog post, not a journal article). High-confidence
# hosts only, so a real publisher host is never matched.
_WEB_HOST_RE = re.compile(
    r"//(?:[\w.-]+\.)?(?:medium\.com|substack\.com|wordpress\.com|blogspot\.|"
    r"hpcwire\.com|eetimes\.|thequantuminsider\.com|quantumcomputingreport\.com|"
    r"blog\.google)/",
    re.I)


def _is_web_source_url(url):
    """True when a url is a clear web/press/grey source (a news/press/blog/events
    path, a bare slide/PDF document, or a known blog host) rather than a journal-
    article landing page. Used to catch a press release or blog post mis-typed as
    @article even when it fills the 'journal' field with a site/platform label."""
    u = url.strip()
    if not u:
        return False
    return bool(_WEB_SOURCE_RE.search(u) or _WEB_DOC_RE.search(u)
                or _WEB_HOST_RE.search(u))


# A thesis/dissertation repository host, path, or filename. These hold doctoral and
# masters theses, not journal articles, so an @article pointing at one is mis-typed
# -- the biblatex type is @thesis. Kept to high-confidence markers so an ordinary
# article url never matches: known thesis hosts (theses.fr is the French national
# portal; tel.* is HAL's thesis server; ethos is the British Library; ProQuest/PQDT
# host dissertations); explicit /thesis//dissertation/ path words; the 'ETD'
# (Electronic Thesis/Dissertation) marker institutional repositories use in their
# handles (e.g. UT Austin '.../ETD-UT-2012-05-5053/...'); and a 'thesis'/
# 'dissertation' FILENAME (e.g. 'LIANG-DISSERTATION.pdf').
_THESIS_URL_RE = re.compile(
    r"theses\.fr|tel\.archives-ouvertes|ethos\.bl\.uk|pqdtopen|proquest\.com/.*dissertation"
    r"|/thesis/|/theses/|/dissertation[s]?/|diss\."
    r"|\bETD[-_/]",
    re.I)
# A thesis keyword as a WORD in the FILENAME (the last path segment), any separator/
# case -- 'LIANG-DISSERTATION.pdf', 'Dissertation_Final_v2.pdf', 'PhD_thesis.pdf',
# 'Smith-MSc.pdf'. Checked on the filename only so a journal-article URL (a DOI/
# accession slug, never a descriptive filename) is not matched. 'master' requires a
# word boundary so 'mastering' / a 'master' branch path does not match.
_THESIS_FILE_RE = re.compile(
    r"(?:^|[-_])(?:thesis|dissertation|phd|msc|masters?)(?:[-_.]|$)", re.I)


def _is_thesis_url(url):
    """True when a url points at a thesis/dissertation repository or page -- a PhD/
    MSc thesis mis-typed as @article should be @thesis. Deterministic, high-
    confidence hosts/paths only, so a journal-article url is never matched."""
    u = url.strip()
    if not u:
        return False
    if _THESIS_URL_RE.search(u):
        return True
    # The 'thesis'/'dissertation' word in the FILENAME (last path segment, before any
    # query) -- 'Dissertation_Final.pdf'. Checked on the filename only.
    filename = re.split(r"[?#]", u)[0].rstrip("/").rsplit("/", 1)[-1]
    return bool(_THESIS_FILE_RE.search(filename))


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
    # '@misc' is biblatex's explicit catch-all/fallback type, and the dominant
    # physics .bst convention (e.g. APS's RevTeX style) renders its title with a
    # plain 'output', not 'output.check' -- i.e. real-world style files do not
    # error on a titleless @misc. Common legitimate idioms have no natural title:
    # a personal communication (howpublished={personal communication}) or a
    # 'see Supplementary Material' pointer. '@software' shares the same
    # constraint in the datamodel and the same catch-all role.
    "misc": ({"title"},),
    "software": ({"title"},),
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
            elif e.etype in ("book", "mvbook") and slot == ["author"] and _has_field(e, "editor"):
                # An edited volume with NO overall author (only an editor) is not a
                # broken @book -- biblatex has a dedicated type for exactly this:
                # @collection ("the work as a whole has no overall author but it will
                # usually have an editor"; required: editor, title, year/date). Point
                # at the real defect (wrong type), not a phantom missing-author error.
                rep.add(Severity.WARN, e, "@book has an editor but no author -- "
                        "biblatex's @collection is the type for an edited volume with "
                        "no overall author; use @collection (or @mvcollection if "
                        "multi-volume) instead of @book", category="entrytype_suggestion")
            else:
                msg = f"missing required field '{shown}' for @{e.etype}"
                # If 'title' is the missing slot and the entry instead carries
                # 'booktitle' -- a field that means something else entirely
                # ("title of the containing work") and is not legal on this type --
                # the two are likely confused. Only fires when booktitle is NOT a
                # legal field here, so a real @incollection/@inbook/@inproceedings
                # (where booktitle is the chapter's correct container-title field)
                # never trips this.
                if slot == ["title"] and _has_field(e, "booktitle") \
                        and "booktitle" not in legal_fields(e.etype):
                    msg += (f" ('booktitle' is present but is not a valid field for "
                            f"@{e.etype} and means something else -- rename 'booktitle' "
                            f"to 'title' if this is a whole book, or use "
                            f"@inbook/@incollection if this is a chapter or contribution)")
                rep.add(Severity.ERROR, e, msg, category="missing_field")
    # biber does not mandate a date, but a reference without one is hard to use --
    # flag it as a recommendation (warning), explicitly beyond the biber datamodel.
    if e.etype not in ("misc", "online", "software", "dataset") \
            and not _has_field(e, "year") and not _has_field(e, "date"):
        rep.add(Severity.WARN, e, "no 'year' or 'date' (recommended; biblatex does "
                "not require it, but a reference needs a date to be usable)",
                category="missing_recommended")

    if e.etype == "article":
        # An @article is a JOURNAL article. Classify by venue, with the URL as a
        # second, stronger signal than the journal FIELD (a press release often
        # fills 'journal' with a site/company label like 'Pasqal news'):
        #   * the URL is a clear web/press/grey source (a /newsroom//news//blog/
        #     /press//events/ path, or a bare .pdf/slides) -> a web item mis-typed
        #     as @article, EVEN IF a 'journal' string is present. Suggest the type.
        #   * else names a journal/eprint -> a real article; at most omits a locator.
        #   * else has a url/howpublished/corporate author -> a web item (no journal
        #     to vouch for it). Suggest the type.
        #   * else (no journal, no url) -> a genuinely broken @article (the error).
        has_venue = _has_field(e, "journal") or e.get("eprint").strip()
        has_weblike = e.get("url").strip() or e.get("howpublished").strip()
        web_url = _is_web_source_url(e.get("url", ""))
        # A BOOK signal beats the web-item guess: an ISBN (a field, or embedded in
        # the url -- publisher 'book/<isbn>/chapter/...' links carry one) or a
        # book-series DOI means this is a book/chapter mis-typed as @article, NOT a
        # web item. Point at the container type (@incollection/@inbook/@book), not
        # @online -- the id is right there in the url (e.g. Sibalic 'Rydberg Physics').
        isbn_in_entry = e.get("isbn").strip() or extract_isbn(e.get("url"), e.get("note"))
        book_like = bool(isbn_in_entry) or is_book_series_doi(e.get("doi", "")) \
            or bool(re.search(r"/book[s]?/", e.get("url", ""), re.I))
        # A THESIS signal (a thesis-repository url host or a /thesis//dissertation/
        # path) also beats the web-item guess: a PhD/MSc thesis mis-typed as @article
        # has its own biblatex type. Checked before the generic web-item branch.
        thesis_like = _is_thesis_url(e.get("url", ""))
        if book_like:
            rep.add(Severity.WARN, e, "@article looks like a book or book chapter (it "
                    "carries an ISBN / book DOI / a publisher book url), not a journal "
                    "article -- use @book, @inbook or @incollection instead of @article",
                    field="journal", category="entrytype_suggestion")
        elif thesis_like:
            rep.add(Severity.WARN, e, "@article looks like a thesis (its url is a thesis "
                    "repository / dissertation page), not a journal article -- use "
                    "@thesis (with type={phd}/{mastersthesis}) instead of @article",
                    field="journal", category="entrytype_suggestion")
        elif web_url or (not has_venue and (has_weblike or is_collaboration(e.get("author", "")))):
            rep.add(Severity.WARN, e, "@article looks like a web or press item (its "
                    "url is a news/press/grey source or it names no journal), not a "
                    "journal article -- use @online or @misc (or @thesis if it is a "
                    "thesis the url does not make obvious) instead of @article",
                    field="journal", category="entrytype_suggestion")
        elif not has_venue:
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
            else:
                # A journal IS named, so this is a real article that merely omits a
                # locator. volume/pages are NOT mandatory for @article in EITHER the
                # biblatex datamodel (it requires only author/journaltitle/title) or
                # traditional BibTeX -- the entry is valid and renders fine. So this
                # is purely advisory completeness, a NOTE, not a conformance warning.
                # When the online layer resolves the record it emits a more useful
                # parity_suggestion naming the actual value to add (volume '638'),
                # which SUPERSEDES this generic note so the same fact is not doubled.
                if not has_volume:
                    rep.add(Severity.INFO, e, "published article omits 'volume' "
                            "(not required, but aids citeability)",
                            category="missing_locator")
                if not locatable:
                    rep.add(Severity.INFO, e, "published article omits 'pages' / an "
                            "article number (not required, but aids citeability)",
                            category="missing_locator")


# A plausible CALENDAR YEAR (1900-2099). Strict on purpose: it decides whether a
# value sitting in 'number'/'issue' is really a misplaced publication year. A journal
# issue number is freely 4-digit (Nature numbers issues in the 7000s), so anything
# NOT shaped like a 1900-2099 year must not be read as a year here.
_CALENDAR_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

# A DATE STRING in a number/issue field -- a month name (full or abbreviated,
# with or without a trailing period) optionally followed by a year, or an
# ISO-ish YYYY-MM or MM/YYYY form. All are definitively wrong in an issue field:
# a valid issue number is a bare integer or short alphanumeric code.
# Anchored with word-boundary so 'March' fires but 'marching' does not.
_MONTH_NAMES = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
                r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
                r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")
_DATE_IN_NUMBER_RE = re.compile(
    r"(?:"
    r"(?:" + _MONTH_NAMES + r")\.?\s*(?:\d{1,2},?\s*)?\d{0,4}"  # "December 2019", "May 20, 2020"
    r"|(?:19|20)\d{2}[-/]\d{1,2}"                                # "2019-12", "2020/05"
    r"|\d{1,2}[-/](?:19|20)\d{2}"                                # "12/2019", "5-2020"
    r")",
    re.I,
)


@rule
def misplaced_field(e, rep):
    """A field holding a value that cannot belong there -- it was put in the wrong
    field. Two cases:

    * 'journal'/'journaltitle' that is PURELY NUMERIC ('journal={2024}', '={5}'). A
      journal name always has letters; an all-digit value is never a journal -- it is
      a year/volume/issue misplaced here. This also masks problems: a numeric journal
      satisfies the @article venue check, so a non-article escapes the entry-type
      suggestion.
    * 'number'/'issue' that is a 4-digit YEAR ('number={2024}'). Those fields ARE
      legitimately numeric (issue 3, article number 031320), so only a year-SHAPED
      value (1900-2099) is flagged, never an ordinary issue/article number.

    WARN; when the value is a year, it is suggested for the 'year' field."""
    flagged = False
    for field in ("journal", "journaltitle"):
        val = e.get(field, "").strip().strip("{}").strip()
        if val and val.isdigit():
            is_year = bool(_CALENDAR_YEAR_RE.fullmatch(val))
            same_as_year = e.get("year", "").strip()[:4] == val
            sug = {"field": "year", "to": val} if is_year and not same_as_year else None
            tail = " -- it likely belongs in the 'year'/'date' field" if is_year \
                else " -- a journal name is text, not a number"
            rep.add(Severity.WARN, e, f"'{field}' is a number ({val}); a journal name "
                    f"is never purely numeric{tail}", category="misplaced_field",
                    field=field, suggested=sug)
            flagged = True
    for field in ("number", "issue"):
        val = e.get(field, "").strip().strip("{}").strip()
        if _CALENDAR_YEAR_RE.fullmatch(val):
            same_as_year = e.get("year", "").strip()[:4] == val
            sug = None if same_as_year else {"field": "year", "to": val}
            rep.add(Severity.WARN, e, f"'{field}' is a year ({val}); an issue number "
                    "is not a year -- it likely belongs in the 'year'/'date' field",
                    category="misplaced_field", field=field, suggested=sug)
            flagged = True
        elif _DATE_IN_NUMBER_RE.fullmatch(val):
            # A date string like 'October', 'December 2019', or '2019-12' in the
            # issue field is a cover-date label placed in the wrong field (often
            # by a reference manager). A valid issue value is a bare integer or
            # short alphanumeric code. A bare month name belongs in 'month'; a
            # month+year or ISO date belongs in 'month'/'date' (the year part
            # likely duplicates 'year' already). 'note' is a last resort for forms
            # that don't parse cleanly as a month.
            is_bare_month = bool(re.fullmatch(
                r"(?:" + _MONTH_NAMES + r")\.?", val, re.I))
            if is_bare_month:
                dest = "'month' field (use the bare three-letter macro, e.g. oct)"
            else:
                dest = "'month' or 'date' field (or 'note' if it is a label)"
            rep.add(Severity.WARN, e, f"'{field}' is a date string ({val!r}); an "
                    f"issue number is not a date -- move it to the {dest}",
                    category="misplaced_field", field=field)
            flagged = True
    return flagged


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


# ANY bare 4-digit run, used to scan a 'year'/'date' string for a candidate year.
# This is deliberately permissive -- the caller (year_sanity) filters the matches
# down to a plausible range itself -- so it is NOT a year test and must not be used
# to classify a value as a year (use _CALENDAR_YEAR_RE for that). 'in press',
# 'forthcoming', 'n.d.' carry no 4-digit run and so are left alone.
_FOUR_DIGIT_RE = re.compile(r"\b(\d{4})\b")


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
        years = [int(y) for y in _FOUR_DIGIT_RE.findall(val)]
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

# CamelCase / mixed-case scan: a word with an INTERIOR capital -- a lowercase letter
# immediately followed by an uppercase one ('QuantumCumulants', 'BibTeX', 'arXiv',
# 'MoSe2', 'McClung'). Distinct from an all-caps acronym (no lowercase, handled above).
# Such casing is almost always a deliberate proper noun / software / chemical name that
# sentence-casing would mangle ('Quantumcumulants'), so the author should check/protect
# it. The token may carry trailing non-letters ('.jl', '2'); we capture the run from
# its first letter through any following letters/digits/dots so the suggested {..} wraps
# the whole name. Anchored so a token inside braces or after a backslash is excluded.
_CAMELCASE_RE = re.compile(r"(?<![\\{])\b([A-Za-z]*[a-z][A-Z][A-Za-z0-9.]*)\b(?![}])")

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


def _title_start(title):
    """Index of the title's first alphanumeric char (skipping a leading brace/quote)."""
    return next((i for i, c in enumerate(title) if c.isalnum()), 0)


# Short function words ignored when judging whether a title is in author Title Case.
_TITLE_FUNCTION_WORDS = frozenset((
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "nor", "but",
    "with", "by", "from", "as", "into", "onto", "via", "per", "vs", "versus", "near",
    "over", "under", "between", "through"))


def _is_author_title_case(title):
    """True if the title is written in author TITLE CASE -- (nearly) every significant
    word capitalized -- rather than sentence case with a few standout proper nouns.

    Why it matters: the brace-protection nudges (a capitalized proper noun, an acronym,
    a CamelCase term) only signal a real risk when the capital is the EXCEPTION in an
    otherwise-lowercase title; then a style that sentence-cases will mangle that one
    word. When the WHOLE title is capitalized, that is just the author's casing style
    (biber re-cases it per the chosen style anyway), so singling out individual words
    is noise -- suppress all three nudges. Significant = not a short function word; a
    word counts as capitalized if its first letter is upper OR it has an interior
    capital (CamelCase). Title Case when every significant word is capitalized, or at
    least four are (broadly capitalized even if a stray word is lower)."""
    t = re.sub(r"\$[^$]*\$", " ", title)        # drop math
    t = re.sub(r"[{}\\]", " ", t)                # drop braces/control chars
    words = re.findall(r"[A-Za-z][A-Za-z0-9.'-]*", t)
    sig = [w for w in words if w.lower() not in _TITLE_FUNCTION_WORDS]
    if not sig:
        return False
    capped = sum(1 for w in sig if w[0].isupper() or any(c.isupper() for c in w[1:]))
    return capped >= 4 or capped == len(sig)


def _safe_at_title_start(term, occ_re, title):
    """True if `term` needs NO brace protection purely because of its position.

    BibTeX/biber sentence-casing (change.case$ "t") lowercases every letter at brace
    depth 0 EXCEPT the very first character of the title (Tame the BeaST). So only the
    first CHARACTER is preserved, NOT the whole first word: a first-word term with
    INTERIOR capitals ('QuantumCumulants', 'QED', 'RNA') would still be mangled
    ('Quantumcumulants', 'Qed', 'Rna') and must be flagged. A term is position-safe
    only when (a) every occurrence is the title's first word AND (b) it has no interior
    uppercase (its only capital, if any, is its first letter) -- e.g. 'Rydberg', whose
    'R' is preserved and 'ydberg' is already lowercase."""
    start = _title_start(title)
    matches = list(occ_re.finditer(title))
    if not matches or any(m.start() != start for m in matches):
        return False
    return not any(c.isupper() for c in term[1:])


def add_title_brace_protection(title):
    """Return `title` with brace-protection added around any term that title_caps
    would flag: configured protected terms, all-caps acronyms, and CamelCase words.
    Used to post-process a plain Crossref title before offering it as a suggested
    replacement, so the suggestion never strips protection the bib already has.

    Title Case titles are returned unchanged (the whole-title suppression in
    title_caps applies: if every significant word is capitalised, there are no
    standout proper nouns to protect individually)."""
    if not title or _is_author_title_case(title):
        return title
    result = title
    # Apply in reverse position order so earlier replacements don't shift offsets.
    # Collect (start, end, replacement) for each unprotected term, then apply.
    patches = []

    def _already_braced(word, text):
        return _protected_in_braces(word, text)

    for hint, occ_re, _prot_re in _protected_term_patterns():
        if occ_re.search(result) and not _already_braced(hint, result):
            occ_re2 = re.compile(rf"(?<![\\{{])\b{re.escape(hint)}\b(?![}}])")
            for m in occ_re2.finditer(result):
                if not _safe_at_title_start(hint, occ_re2, result):
                    patches.append((m.start(), m.end(), "{" + hint + "}"))

    seen = set()
    for m in _ACRONYM_RE.finditer(result):
        word = m.group(1)
        if word in ("A", "I") or word in seen:
            continue
        seen.add(word)
        occ_re = re.compile(rf"\b{re.escape(word)}\b")
        if not _safe_at_title_start(word, occ_re, result) and not _already_braced(word, result):
            for m2 in occ_re.finditer(result):
                patches.append((m2.start(), m2.end(), "{" + word + "}"))

    seen_camel = set()
    for m in _CAMELCASE_RE.finditer(result):
        word = m.group(1)
        if word in seen_camel:
            continue
        seen_camel.add(word)
        if not _already_braced(word, result):
            patches.append((m.start(), m.end(), "{" + word + "}"))

    # Deduplicate overlapping patches (take the first) and apply in reverse order.
    patches.sort(key=lambda p: p[0])
    merged = []
    last_end = -1
    for start, end, repl in patches:
        if start >= last_end:
            merged.append((start, end, repl))
            last_end = end
    for start, end, repl in reversed(merged):
        result = result[:start] + repl + result[end:]
    return result


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
    # If the WHOLE title is in author Title Case, capitalized words are the author's
    # styling (which biber re-cases per style anyway), not standout proper nouns -- so
    # none of the per-word brace-protection nudges below apply. Only in a sentence-case
    # title does a capitalized/acronym/CamelCase word signal a term at real risk.
    if _is_author_title_case(title):
        return
    # All three checks below flag a title term that a style sentence-casing the title
    # will silently lowercase -- a proper noun, an acronym, or a CamelCase name. This is
    # a common, real defect (the rendered reference shows 'rydberg'/'qed'), so they
    # share the WARN-level `title_capitalization` category, not a quiet style note.
    # Track every term already flagged by any sub-check so the CamelCase loop
    # does not emit a redundant finding for the same word (e.g. 'QuTiP' in
    # protected_terms fires the "not brace-protected" note; the CamelCase scan
    # must not then fire a second "mixed-case term" note on the same word).
    already_flagged = set()
    for hint, occ_re, prot_re in _protected_term_patterns():
        if (occ_re.search(title) and not prot_re.search(title)
                and not _safe_at_title_start(hint, occ_re, title)):
            rep.add(Severity.WARN, e, "title term not brace-protected; may be lowercased "
                    "by some styles", category="title_capitalization", field="title",
                    suggested={"field": "title", "from": hint, "to": "{" + hint + "}"})
            already_flagged.add(hint)
    seen_acronyms = set()
    for m in _ACRONYM_RE.finditer(title):
        word = m.group(1)
        if word in ("A", "I") or word in seen_acronyms:
            continue
        seen_acronyms.add(word)
        # An all-caps acronym ALWAYS has interior capitals, so it is never position-safe
        # ('QED' as the first word still mangles to 'Qed'); _safe_at_title_start returns
        # False for it. Use a word-boundary occurrence regex for the position check.
        occ_re = re.compile(rf"\b{re.escape(word)}\b")
        if _safe_at_title_start(word, occ_re, title):
            continue
        if not _protected_in_braces(word, title):
            rep.add(Severity.WARN, e, "acronym in title not brace-protected; some "
                    "styles will lowercase it", category="title_capitalization",
                    field="title",
                    suggested={"field": "title", "from": word, "to": "{" + word + "}"})
            already_flagged.add(word)
    # CamelCase / interior-capital terms (a WARN: the author very likely intended this
    # casing -- a package/proper/chemical name like 'QuantumCumulants.jl', 'BibTeX',
    # 'MoSe2' -- and sentence-casing would silently mangle it, so they should check and
    # brace-protect it). Skipped when already brace-protected, already flagged by the
    # protected-terms or acronym loops above (same word, same fix -- no duplicate), and
    # (like acronyms) an interior capital is never position-safe so first-word ones fire.
    seen_camel = set()
    for m in _CAMELCASE_RE.finditer(title):
        word = m.group(1)
        if word in seen_camel or word in already_flagged:
            continue
        seen_camel.add(word)
        if not _protected_in_braces(word, title):
            rep.add(Severity.WARN, e, "title has a mixed-case (CamelCase) term that "
                    "some styles will lowercase; if the casing is intentional (a "
                    "software/proper name) brace-protect it", category="title_capitalization",
                    field="title", suggested={"field": "title", "from": word,
                                              "to": "{" + word + "}"})


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
    """A name list truncated rather than stored in full. Three forms, split by how
    wrong each is:

    * a literal 'et al.' or a bare 'al.' (the user dropped the 'et') -- the WARN
      case. 'et al.' is a *publisher rendering*, not data: BibTeX/biblatex treat
      the spelled-out form as a real author ('al.' becomes a phantom surname) and
      it bakes one journal's convention into the .bib. The fix is the proper
      'and others' marker.
    * the 'and others' marker itself -- a NOTE, not a warning. It is a valid,
      deliberate biblatex marker that the style renders correctly as 'et al.'; the
      only downside is the dropped names are not stored, so a style that must list
      the first N authors cannot recover them. Truncating the displayed count is the
      style sheet's job (maxnames/maxbibnames), not the .bib's -- so this is a
      stylistic record-keeping note, separated from the malformed cases above."""
    for field in ("author", "editor"):
        val = e.get(field, "")
        # A literal 'et al.' in any written form (space, LaTeX tie 'et~al.',
        # run-together 'et.al.') OR a bare trailing 'al.' with the 'et' dropped
        # ('Pedram Roushan al.') -- both hard-code a rendering / a phantom name.
        if has_etal_marker(val):
            rep.add(Severity.WARN, e, f"{field} list contains a literal 'et al.' "
                    "(or variant); BibTeX treats it as an author and it hard-codes a "
                    "journal's rendering -- store the full author list and use 'and "
                    "others' if a marker is needed (the style produces 'et al.')",
                    category="author_completeness", field=field)
        elif re.search(r"\band\s+others\b", val, re.I):
            rep.add(Severity.INFO, e, f"{field} list is truncated with 'and others'; "
                    "valid (the style renders it 'et al.'), but the dropped names are "
                    "not stored -- keep the full list and let the style truncate the "
                    "displayed count (maxnames/maxbibnames)",
                    category="author_truncated_marker", field=field)


# A name-separator 'and' fused to the preceding name token with no space:
# 'Pientka, F.and Peng, Y.' (initial fused) or 'Kir..., Gabijaand Pregnolato'
# (full given name fused). BibTeX then reads the fused token as the given name
# and never sees the following author as separate, producing a spurious 'given
# name differs' and a 'missing author' finding against the record.
#
# Two sub-patterns, both requiring a capitalized next word so 'Anderson' /
# 'Brandt' / other legitimate surnames ending in 'and' are not matched:
#   1. An initial (single capital + optional period) directly before 'and'.
#      Unambiguous: a real initial is never followed by 'and' with no space, so
#      this sub-pattern needs no further check.
#   2. A multi-letter word in the given-name position (after a comma) ending in
#      'and'. Surnames ending in 'and' (Bertrand, Armand) only appear *before* a
#      comma, so they cannot reach this sub-pattern. But a GIVEN name ending in
#      'and' (Roland, Ferdinand, Armand-as-given-name) is indistinguishable by
#      this pattern alone from a genuinely fused separator: 'Farrell, Roland C.'
#      and 'Pregnolato, Gabijaand Lodahl' both look like '<word>and <Capital>'.
#      Sub-pattern 2 alone is therefore only a CANDIDATE; _is_fused_and()
#      disambiguates using the structural fact that splits them: after a
#      genuine fused 'and', the surname it introduces is followed by ITS OWN
#      ', GivenName' before the next ' and '/end of list (the next author has
#      not been read yet), whereas after a given name that merely ends in
#      'and', the matched capital is a trailing initial/given-name token of the
#      SAME author and no comma intervenes before the next ' and ' or the end.
_GLUED_AND_RE = re.compile(
    r"(?:^|[\s,])([A-Z]\.?and)\s+[A-Z]"           # initial fused: 'F.and'
)
_GLUED_AND_CANDIDATE_RE = re.compile(
    r",\s*([A-Za-z]{2,}and)\s+([A-Z][A-Za-z'\-]*)"  # given name fused: 'Gabijaand Pregnolato'
)


def _is_fused_and(field_value, match):
    """True if `match` (a _GLUED_AND_CANDIDATE_RE hit) is a genuinely fused 'and'
    rather than a given name that happens to end in 'and' followed by its own
    trailing initial. Looks at the text after the matched capitalized word, up
    to the next ' and ' separator or the end of the field: a real fused
    separator introduces a NEW author, so a ',' (starting that author's given
    name) appears before either boundary; a trailing initial/given-name token of
    the SAME author is not followed by one."""
    tail_start = match.end()
    rest = field_value[tail_start:]
    next_and = re.search(r"\sand\s", rest)
    window = rest[:next_and.start()] if next_and else rest
    return "," in window


@rule
def glued_and_separator(e, rep):
    """The ' and ' author separator glued to the preceding name token with no space
    ('F.and Peng', 'Gabijaand Pregnolato'). Reported as one delimiter error, since
    otherwise it splinters into unrelated 'given name differs' and 'missing author'
    findings against the record."""
    for field in ("author", "editor"):
        val = e.get(field, "")
        m = _GLUED_AND_RE.search(val)
        fused = m.group(1) if m else None
        if not fused:
            cm = _GLUED_AND_CANDIDATE_RE.search(val)
            if cm and _is_fused_and(val, cm):
                fused = cm.group(1)
        if fused:
            rep.add(Severity.WARN, e, f"{field} list has 'and' fused to a name with no "
                    f"space ({fused!r}); BibTeX reads it as one author and drops "
                    f"the separator -- add a space before 'and'",
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


# Footnote / affiliation markers that get copy-pasted into a name from a rendered
# byline (superscripts), alongside digits. None belongs in a personal name.
_NAME_MARKER = r"\d|[*†‡§¶∗★☆]"  # digit, * † ‡ § ¶ ⋆ ★ ☆

# TeX *spacing* macros that do not belong in a name field: an explicit inter-initial
# space ('H.{\hspace{0.167em}}L.'), a kern, or one of the short spacing primitives
# (\, \; \: \! \quad \qquad \thinspace \enspace). These are typesetting, not name
# content -- biblatex already spaces initials -- so a name should store 'H. L.', not
# the macro. This is DELIBERATELY not accents (\o, \"{u}, \'{e}) or other name markup,
# which are legitimate encoding and must be left alone.
_TEX_SPACING = re.compile(
    r"\\(?:hspace\*?|kern|hskip|mskip)\s*\{?[^{}]*\}?"   # \hspace{..}, \kern 2pt, ...
    r"|\\(?:thinspace|enspace|quad|qquad|space)\b"        # named spaces
    r"|\\[,;:!> ]")                                        # \, \; \: \! \> and '\ '


def _strip_name_spacing(tok):
    """Return `tok` with TeX spacing macros collapsed to a single normal space, plus
    a flag for whether any were present. Brace pairs left empty by the removal (the
    common '{\\hspace{..}}' wrapper) are cleaned up, and runs of space coalesced, so
    'H.{\\hspace{0.167em}}L.' -> 'H. L.'. Accents/encoding are untouched."""
    if not _TEX_SPACING.search(tok):
        return tok, False
    s = _TEX_SPACING.sub(" ", tok)
    s = re.sub(r"\{\s*\}", " ", s)          # '{}' left where '{\hspace..}' was
    s = re.sub(r"\s+", " ", s).strip()
    return s, True


@rule
def marker_in_author_name(e, rep):
    """A digit or footnote symbol glued to an author/editor name token -- 'Sam R.
    Cohen1', 'Smith*', 'Lee†' -- a leftover affiliation/footnote superscript
    copy-pasted from a rendered byline. None is part of a real personal name, so it
    is a transcription defect that deviates the name from the published record (and
    renders wrong). WARN, with the marker stripped in the suggested fix. Offline
    fallback for the same deviation the record layer flags online -- caught even with
    no network or no record. (A brace-protected token -- a collaboration like
    '{Team 2}' -- is left alone: it is deliberate, not a stray superscript.)"""
    marker = _NAME_MARKER
    glued = re.compile(rf"[A-Za-z](?:{marker})|(?:{marker})[A-Za-z]")
    strip = re.compile(rf"(?<=[A-Za-z])(?:{marker})+|(?:{marker})+(?=[A-Za-z])")
    # A standalone all-marker WORD inside a name -- 'David Weiss 2017' (a stray year),
    # 'Smith 1' -- where the digit/symbol is a separate space-delimited token rather
    # than glued. A bare number is never a name part, so this is the same defect.
    bare_word = re.compile(rf"(?:^|\s)((?:{marker})+)(?=\s|$)")
    for field in ("author", "editor"):
        val = e.get(field, "")
        if not val.strip():
            continue
        for tok in re.split(r"\s+and\s+", val.replace("\n", " ")):
            tok = tok.strip()
            if not tok or tok.startswith("{"):
                continue
            # A TeX *spacing* macro inside a name ('H.{\hspace{0.167em}}L.') is
            # typesetting, not name content -- biblatex spaces initials itself, and
            # the record stores 'H. L.'. Flag it and suggest the macro -> a plain
            # space. Done FIRST, and the marker scan below then runs on the cleaned
            # name, so the macro's own dimension digits ('0.167em') are never misread
            # as a stray-year/footnote superscript (that was a false positive).
            despaced, had_spacing = _strip_name_spacing(tok)
            if had_spacing:
                # A NOTE, not a warning: a TeX spacing macro is a portability/style
                # nudge, not a data defect -- biblatex spaces initials itself and the
                # name (and its record match) is unaffected, so it never survives as a
                # "real problem". (The stray-superscript case below stays a WARN: that
                # one changes WHO the author is.) Also why Crossref verifying the entry
                # does not supersede this -- the record has no opinion on .bib markup.
                rep.add(Severity.INFO, e, f"{field} name {tok!r} contains a TeX "
                        "spacing macro (e.g. \\hspace) -- typesetting, not part of the "
                        "name; biblatex spaces initials itself",
                        category="author_format", field=field,
                        suggested={"field": field, "from": tok, "to": despaced})
            scan = despaced
            if glued.search(scan) or bare_word.search(scan):
                fixed = bare_word.sub("", strip.sub("", scan)).strip()
                rep.add(Severity.WARN, e, f"{field} name {scan!r} contains a digit or "
                        "footnote marker (likely a stray year or affiliation "
                        "superscript); it deviates from the real name",
                        category="author_format", field=field,
                        suggested={"field": field, "from": tok, "to": fixed})


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
    # APS Rapid Communications / Letters append a parenthetical marker to the article
    # id ('040101(R)', '060301(L)') -- a standard, valid form, not unusual. Only a
    # value that is NOT a recognizable locator (e.g. 'pp.', 'in press', 'ix, 277 p.',
    # 'arXiv:...') is flagged.
    if re.fullmatch(r"[A-Za-z]*\d+[A-Za-z]?(?:\([A-Za-z]\))?", p):
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
        # The arXiv id properly lives in the 'eprint' field, not in 'journal' -- a
        # bare 'journal={arXiv}' is just a venue label. So only nag about the
        # 'journal' string when NO arXiv id is recoverable anywhere in the entry
        # (eprint/doi/url): then the entry is genuinely missing the id and the
        # journal string is the only place to put it. When the id IS elsewhere, a
        # plain 'arXiv' label is accepted silently.
        id_url = extract_arxiv_id(e.get("url", ""))
        if id_journal:
            # The journal string itself carries an id but in a non-canonical wrapper
            # ('arXiv preprint arXiv:2304.14360'): suggest the bare canonical form.
            rep.add(Severity.INFO, e, "arXiv journal field not in canonical form",
                    category="style", field="journal",
                    suggested={"field": "journal", "from": j, "to": f"arXiv:{id_journal}"})
        elif not (id_eprint or id_doi or id_url):
            rep.add(Severity.INFO, e, f"arXiv journal field not in canonical form "
                    f"'arXiv:XXXX.XXXXX': {j!r}", category="style", field="journal")


@rule
def identifier_in_url(e, rep):
    """Good-practice nudge (note): an identifier sits in the `url` but not in a
    structured biblatex field. biblatex stores a DOI in `doi` and an arXiv id in
    `eprint`+`eprinttype={arxiv}` so styles, hyperlinks and tools can use it; an id
    buried in a free-text url is invisible to them. Deterministic -- it only points
    at an id already extractable from the url, never a guess -- and note-level, so it
    teaches the standard without blocking. This is the upstream fix for most
    doi_available / arXiv-journal findings."""
    url = e.get("url", "")
    if not url.strip():
        return
    # A DOI in the url but no (non-arXiv) doi field. Its own category (not the
    # overloaded 'style') so it is independently tunable AND so the online layer can
    # withdraw it when 'doi_available' reports the SAME DOI (the online finding is
    # richer -- it confirms the DOI resolved -- so the two should not both show).
    if not e.get("doi").strip():
        url_doi = extract_doi_from_url(url)
        if url_doi:
            rep.add(Severity.INFO, e, f"DOI {url_doi} is in the url but not in a "
                    "'doi' field; biblatex stores it in 'doi' so styles and tools can "
                    "use it", category="identifier_placement", field="doi",
                    suggested={"field": "doi", "to": url_doi})
    # An arXiv id in the url but no eprint field.
    if not e.get("eprint").strip():
        url_arxiv = extract_arxiv_id(url)
        if url_arxiv:
            rep.add(Severity.INFO, e, f"arXiv id {url_arxiv} is in the url but not in "
                    "an 'eprint' field; biblatex stores it in 'eprint' with "
                    "'eprinttype={arxiv}' so it is linkable and machine-readable",
                    category="identifier_placement", field="eprint",
                    suggested={"field": "eprint", "to": url_arxiv})


@rule
def online_needs_urldate(e, rep):
    """Good-practice nudge (note): an @online / web-cited entry should carry an
    `urldate` (access date) -- biblatex recommends it for sources whose content can
    change or vanish, which is the only date a page without a stable date can be
    pinned to.

    Scoped to entries that are ACTUALLY online/grey, not merely url-bearing. A url is
    not by itself an 'online source': in many bibs the DOI/arXiv id lives inside the
    url rather than a structured field, so a published @article landing page (a DOI
    or arXiv abstract page) carries a url yet is a stable source of record an access
    date adds nothing to. So fire only when:
      * the entry's TYPE is online-ish (@online/@misc/@electronic/@www), or
      * its url is a clear web/press/grey source (a news/press/blog path, a bare
        slide/PDF off a personal/corporate site, a known blog host) AND it exposes
        no stable identifier (no doi/eprint field, and no DOI/arXiv id mineable from
        the url) -- i.e. it cannot be pinned by an id, only by an access date.
    A stable, identifier-bearing landing page never gets the nudge, regardless of
    type. Pure best practice, zero risk -- it asks for a field, never guesses one."""
    url = e.get("url").strip()
    if e.get("urldate").strip() or not url:
        return
    # A STABLE identifier (a doi/eprint field, or a DOI/arXiv id mineable from the
    # url) makes this a fixed source of record -- an access date adds nothing -- so it
    # never gets the nudge, REGARDLESS of entry type. This is the key guard: an arXiv
    # preprint is commonly @misc with eprint=<id> + an arxiv.org url, and must not be
    # nudged just because @misc is an online-ish type.
    has_stable_id = bool(e.get("doi").strip() or e.get("eprint").strip()
                         or extract_doi_from_url(url, e.get("note"))
                         or extract_arxiv_id(url, e.get("note")))
    if has_stable_id:
        return
    online_type = e.etype in ("online", "electronic", "www", "misc")
    if online_type or _is_web_source_url(url):
        rep.add(Severity.INFO, e, "online/url-cited entry has no 'urldate' (access "
                "date); biblatex recommends one for online sources",
                category="style", field="urldate")


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

def duplicate_keys_and_dois(entries, rep, cited_keys=None):
    """Flag entries that refer to the same work: first by duplicate citation key,
    then by shared DOI (unambiguous), then by title+year+first-author similarity
    (catches duplicates that lack a DOI or where one entry is a preprint and the
    other the published version).

    When `cited_keys` is supplied (--tex mode), a duplicate pair where NEITHER
    key is cited is noise -- the reader never sees both in the reference list, so
    it is not flagged. A pair where BOTH keys are cited produces a harder warning
    (the same work appears twice in the reference list); a pair where only one is
    cited is still flagged (the uncited entry is a latent collision).
    """
    # Build a fast lookup: is a given key cited (or are we in non-tex mode)?
    def _cited(key):
        return cited_keys is None or key in cited_keys

    seen_keys, seen_dois = {}, {}
    # Secondary check: (title_key, year, folded_first_surname) -> (key, lineno)
    seen_fingerprints = {}

    for e in entries:
        if e.key in seen_keys:
            rep.add(Severity.ERROR, e, f"duplicate citation key (also at line {seen_keys[e.key]})",
                    category="duplicate")
        else:
            seen_keys[e.key] = e.lineno

        # --- DOI deduplication (unambiguous) ---
        # Compare DOIs in their bare, lowercased form so a URL-wrapped DOI
        # ('https://doi.org/10.1/x') and the same DOI written bare ('10.1/x') --
        # a very common mix within one .bib -- are recognized as the same work.
        doi = bare_doi(e.get("doi", "").strip()).lower()
        if doi:
            if doi in seen_dois:
                other_key = seen_dois[doi]
                # Skip if neither key is cited (bib maintenance noise, not a reader problem).
                if not _cited(e.key) and not _cited(other_key):
                    pass
                else:
                    both_cited = _cited(e.key) and _cited(other_key)
                    sev = Severity.ERROR if both_cited else Severity.WARN
                    msg = (f"DOI shared with '{other_key}': {doi}"
                           + (" -- same paper cited twice under different keys"
                              if both_cited else ""))
                    rep.add(sev, e, msg, category="duplicate")
            else:
                seen_dois[doi] = e.key

        # --- Title+year+first-author fingerprint (secondary, no DOI match needed) ---
        # This catches the common preprint-vs-published case where the bib has two
        # entries for the same paper with different DOIs (or one has no DOI), as
        # long as the title, year, and first author all agree. The title key is
        # already normalized (lowercased, de-TeXed, stopwords removed); the year
        # must match exactly; the first surname must fold equal. This is conservative:
        # all three must agree to avoid flagging distinct papers that share a title word.
        tk = title_key(e.get("title", ""))
        year = str(e.get("year", "") or "").strip()
        bib_authors = split_authors(e.get("author", ""))
        first = bib_authors[0] if bib_authors else ""   # already folded by split_authors
        if tk and year and first:
            fp = (tk, year, first)
            if fp in seen_fingerprints:
                other_key, other_line = seen_fingerprints[fp]
                # Skip if the DOI match already reported this pair.
                if doi and doi in seen_dois and seen_dois[doi] == other_key:
                    pass
                # Skip if neither key is cited.
                elif not _cited(e.key) and not _cited(other_key):
                    pass
                else:
                    both_cited = _cited(e.key) and _cited(other_key)
                    sev = Severity.WARN
                    msg = (f"possible duplicate of '{other_key}' (line {other_line}): "
                           f"same title, year, and first author"
                           + (" -- same paper cited twice under different keys"
                              if both_cited else ""))
                    rep.add(sev, e, msg, category="duplicate")
            else:
                seen_fingerprints[fp] = (e.key, e.lineno)


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
        # Lead with the COUNT per style (like the author_format note), then the full
        # key list in parentheses so automated correction still has every entry.
        summary = "; ".join(f"{k}: {len(v)} ({', '.join(v)})"
                            for k, v in sorted(styles.items()))
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
    # no '=' (e.g. 'title {...}') is a structural error BibTeX rejects. We ask the
    # PARSER where the real top-level fields are (iter_field_decls), rather than
    # re-scanning the raw text with a brace-only heuristic: that heuristic is blind
    # to '"..."'-delimited values, which may wrap across lines and contain commas
    # (e.g. 'publisher = "..., Inc., New\n York"'), and so would fabricate a phantom
    # 'york' field and flag a sound entry. iter_field_decls advances over each value
    # with the parser's own atom reader, so it and the parser can never disagree on
    # what is a field. A declaration whose separator is '{' or '"' (not '=') is the
    # missing-'=' error. Offsets are into the body (after '@type{'); map back to a
    # line via the entry's own start line.
    for e in entries:
        body_start = e.raw.find("{") + 1
        if body_start <= 0:
            continue
        body = e.raw[body_start:]
        for off, fld, sep in iter_field_decls(body):
            if sep == "=":
                continue
            line = e.lineno + body[:off].count("\n")
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


def run_file_rules(entries, rep, cited_keys=None):
    """Apply every registered whole-file @file_rule once (duplicate keys/DOIs,
    author-format and arXiv-style consistency). Run after the per-entry pass.
    `cited_keys` is the set of keys actually cited in the manuscript (--tex mode);
    when None (no .tex), all entries are treated as cited."""
    duplicate_keys_and_dois(entries, rep, cited_keys=cited_keys)
    for fn in FILE_RULES:
        fn(entries, rep)


def run_static(entries, rep):
    """STATIC layer: apply every registered rule to the entries (per-entry rules
    then file-wide rules). Kept as the all-entries entry point used by tests."""
    for e in entries:
        run_entry_rules(e, rep)
    run_file_rules(entries, rep)
