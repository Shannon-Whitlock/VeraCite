"""Comparison layer: flag where an entry disagrees with its resolved record(s).

The DOI/arXiv id already establishes identity, so individual field disagreements
(author, title, year/volume/pages, journal) are metadata discrepancies a human
should check -- not errors. The authoritative record is the canonical reference:
each flagged discrepancy carries a suggested edit that conforms the bib TO the
record (year 2009 -> 2010), and SEVERITY follows render-impact -- a field that
changes the rendered citation (author/title/year/journal/volume/issue/pages) warns;
a purely stylistic difference (e.g. an abbreviated given name) is a note. The one
true error is the case where the first author AND the title both differ strongly
(the id likely points elsewhere). Also compares authoritative sources against each
other (cross-source conflicts) and suggests fields the record carries that the entry
omits (parity).
"""

import html
import json
import os
import re

from .normalize import (author_surnames_display, biblatex_pages, bib_given_names,
                        clean_tex, deaccent, is_container_granularity, is_preprint,
                        is_truncated, norm_pages, split_authors, title_is_miscased)
from .report import Severity
from .titles import title_is_shortened, title_key, title_overlap


# --- author comparison (source-tiered trust) -------------------------------

# Surname particles (folded, no spaces) a record may drop from the front of a
# compound surname: the bib keeps 'da Silva' -> dasilva, the record lists Silva.
# Compounds combine, so 'van de ' -> 'vande'. Kept to genuine droppable nobiliary
# particles; Mc/Mac/O' are part of the surname and are deliberately excluded.
_PARTICLE_PREFIXES = ("vandeden", "vandeter", "vande", "vander", "vanden",
                      "van", "von", "della", "del", "de", "da", "di", "du",
                      "la", "le", "den", "der", "ten", "ter", "dos", "das")


def _surname_match(a, b):
    """Whether two folded surnames denote the same person: equal, or one is the
    other with a leading nobiliary particle dropped (bib 'da Silva' -> dasilva,
    record lists 'Silva'). Only a known particle prefix is stripped -- an
    arbitrary shared suffix is NOT a match, so 'han'/'chan' stay distinct."""
    if a == b:
        return True
    long, short = (a, b) if len(a) >= len(b) else (b, a)
    if not short or not long.endswith(short):
        return False
    return long[:-len(short)] in _PARTICLE_PREFIXES


def _author_diff(left, right):
    """Surnames in `left` with no match in `right` (particle-aware)."""
    return [x for x in left if not any(_surname_match(x, y) for y in right)]


def _is_initial(name):
    """True if a given name is initials only ('L', 'L.', 'J.R.', 'J.-P.', 'J. R.')
    rather than a spelled-out name: every alphabetic run is a single letter."""
    tokens = re.findall(r"[A-Za-z]+", name)
    return not tokens or all(len(t) == 1 for t in tokens)


def _clean_name_key(name):
    """A surname reduced to its comparable form for DEVIATION detection: deaccented,
    lowercased, with internal whitespace collapsed and hyphens/apostrophes/periods
    (all legitimate in names) normalized away. What remains is the bare letters --
    so 'Cohen' and 'Cohén' and 'Cohen.' all match, but 'Cohen1' (a stray digit) or
    'Cohen*' (a footnote mark) does NOT, since the extra character survives. This is
    how the deviation check stays robust without enumerating bad-character classes:
    anything that is not an accent/case/punctuation difference shows up as a real
    deviation from the record's clean name."""
    s = deaccent(name).lower()
    return re.sub(r"[\s.'’-]+", "", s)


# Words whose interior capital is a LEGITIMATE camelCase name form, where the bib and
# the record may simply differ on casing -- not a typo. Matched on the word containing
# the interior capital.
_OK_CAP_WORD_RE = re.compile(
    r"^(?:Mc|Mac|Fitz|De|Des|Del|Della|La|Le|Di|Du|Van|Von|Der|O'|D'|Al-)\w*[A-Z]",
    re.I)


def _miscapitalized_ok(name):
    """The record's casing is authoritative, so a bib name that differs from it only
    in case is a deviation -- EXCEPT for legitimate camelCase name forms where sources
    genuinely disagree (McDonald/Mcdonald, DeWitt, O'Brien). Return True (do not flag)
    only when EVERY word with an interior capital is such a recognized form; return
    False when any word has a stray interior capital in an ordinary word ('VIncent'),
    which is a typo. An interior capital is any uppercase letter that is not the word's
    first character."""
    for word in re.split(r"[\s'-]+", name):
        if not word:
            continue
        if re.search(r".[A-Z]", word) and not _OK_CAP_WORD_RE.match(word):
            return False
    return True


def _title_punct_key(title):
    """A title reduced to a CASE- and ACCENT-insensitive form that PRESERVES
    punctuation -- so a hyphen, '&', colon or spacing difference survives while a
    casing or accent difference does not. Used to detect a title that matches the
    record as the same work (title_key equal) but whose written PUNCTUATION deviates
    from the record's canonical form (e.g. 'open source' vs 'open-source'). De-TeX
    first so brace-protection ('{Yb}') is not counted as a difference."""
    s = deaccent(clean_tex(title)).lower()
    return re.sub(r"\s+", " ", s).strip()


def _given_abbreviates(short, full):
    """Whether `short` is an abbreviated form of the spelled-out `full`, so it is
    not a different person. Covers a pure initial ('K.' for 'Karl') and a
    partly-abbreviated hyphenated name ('Karl-C.' for 'Karl-Christian'): each
    hyphen segment of `short` must initial-or-equal the matching segment of
    `full`."""
    if _is_initial(short):
        return True
    ss = [p for p in re.split(r"[-\s]+", short.strip()) if p]
    fs = [p for p in re.split(r"[-\s]+", full.strip()) if p]
    if not ss or len(ss) > len(fs):
        return False
    for sp, fp in zip(ss, fs):
        sp_letters = re.sub(r"[^A-Za-z]", "", sp)
        if sp_letters and len(sp_letters) == 1:        # this segment is an initial
            if not deaccent(fp).lower().startswith(sp_letters.lower()):
                return False
        elif deaccent(sp).lower() != deaccent(fp).lower():
            return False
    return any(re.sub(r"[^A-Za-z]", "", p) and len(re.sub(r"[^A-Za-z]", "", p)) == 1
               for p in ss)


def _compare_authors(e, rec, source, rep):
    """Author comparison against an id-resolved record. The DOI/arXiv id already
    proves this is the right paper, so an author discrepancy is a metadata issue
    to check (WARN), not a wrong-paper error. The returned `first_differs` is the
    strongest single signal and is combined with a strong title mismatch by the
    caller into the one genuine wrong-record error. arXiv's API yields only a
    folded last token per author, so its data stays advisory either way."""
    bib_authors = split_authors(e.get("author", ""))
    rec_authors = rec.get("authors", [])
    # A list ending in ANY truncation marker -- the valid 'and others' OR a
    # malformed 'et al.'/'al.' -- is truncated: in both the record enumerates names
    # the bib stopped recording, so the "missing from bib" / given-name checks below
    # are suppressed the same way for each.
    truncated = is_truncated(e.get("author", ""))
    # Folded key -> original surname, so a message shows 'Biten'/'Furkan Biten'
    # rather than the folded matching key 'biten'/'furkanbiten'. Display only;
    # all comparison below stays on the folded keys.
    display = dict(zip(rec_authors, rec.get("authors_display") or []))
    display.update(zip(bib_authors, author_surnames_display(e.get("author", ""))))
    show = lambda k: display.get(k) or k
    # A truncation is faithful -- not lossy -- when the authoritative record
    # enumerates no more names than the bib already carries (a collaboration the
    # record holds as a single name, or a list the bib already gives in full). Then
    # withdraw the offline finding: there are no dropped names to recover. The
    # valid 'and others' marker is author_truncated_marker (a note); a malformed
    # 'et al.'/'al.' is author_completeness (a warning) -- withdraw whichever fired.
    # Count ALL bib name tokens via split_authors' token set (the marker already
    # stripped) plus any collaboration name split_authors drops, so 'LHCb
    # Collaboration and others' counts as one. Done before the empty-list guard
    # below so a pure-collaboration author is still reconciled.
    if truncated and rec_authors:
        bib_name_count = sum(
            1 for a in re.split(r"\s+and\s+", e.get("author", "").replace("\n", " "))
            if a.strip() and a.strip().lower() not in ("others", "et al.", "al.", "al"))
        if len(rec_authors) <= bib_name_count:
            rep.withdraw(e.key, "author_completeness")
            rep.withdraw(e.key, "author_truncated_marker")
    if not (bib_authors and rec_authors):
        return False
    bib_only = _author_diff(bib_authors, rec_authors)
    rec_only = _author_diff(rec_authors, bib_authors)
    first_differs = not _surname_match(bib_authors[0], rec_authors[0])

    if first_differs:
        rep.add(Severity.WARN, e, f"[{source}] first author differs: "
                f"bib={show(bib_authors[0])!r} vs {source}={show(rec_authors[0])!r}",
                "record", category="metadata_mismatch")
    if bib_only:
        rep.add(Severity.WARN, e, f"[{source}] author(s) in bib not in record: "
                + ", ".join(sorted(show(a) for a in bib_only)), "record",
                category="metadata_mismatch")
    if rec_only and not truncated:
        rep.add(Severity.WARN, e, f"[{source}] author(s) in record missing from bib: "
                + ", ".join(sorted(show(a) for a in rec_only)), "record",
                category="metadata_mismatch")
    if not truncated and not bib_only and not rec_only and not first_differs \
            and bib_authors != rec_authors:
        rep.add(Severity.WARN, e, f"[{source}] same authors but in a different order "
                f"than the record", "record", category="metadata_mismatch")
    initials = [a for a in bib_authors if len(a) <= 1]
    if initials and not truncated:
        rep.add(Severity.INFO, e, f"[{source}] author surname(s) reduced to initials "
                f"({', '.join(show(a) for a in initials)}); check name parsing", "record",
                category="metadata_mismatch")

    # SURNAME DEVIATION: an author folds-equal to the record (so it IS the right
    # person -- identity is fine) but its written form DEVIATES from the record's by
    # more than accent/case -- a stray digit or footnote mark glued on ('Cohen1'), a
    # typo, an extra fragment. The record is the canonical reference, so this is a
    # metadata discrepancy to fix (WARN), with the record's clean name suggested.
    # Crossref only (arXiv folds names to a last token, so its display is unreliable);
    # skipped when truncated (the lists do not align). `_clean_name_key` normalizes
    # accent+case so a legitimate stylistic difference never trips it -- only a real
    # character deviation does, which generalizes past any single bad-character class.
    if source == "crossref" and not truncated:
        rec_disp = dict(zip(rec_authors, rec.get("authors_display") or []))
        bib_disp = dict(zip(bib_authors, author_surnames_display(e.get("author", ""))))
        for key in bib_disp:
            bd, rd = bib_disp.get(key, ""), rec_disp.get(key, "")
            if bd and rd and _clean_name_key(bd) != _clean_name_key(rd):
                rep.add(Severity.WARN, e, f"[{source}] author name differs from the "
                        f"record: bib={bd!r} vs {source}={rd!r}", "record",
                        category="metadata_mismatch", field="author",
                        suggested={"field": "author", "from": bd, "to": rd})

    # Given-name check (Crossref only -- arXiv folds names to a last token). For a
    # shared surname, a differing full given name is a discrepancy to verify
    # (WARN, not error: the id still resolves to this paper). Skipped when the bib
    # list is truncated ('and others'), where a shared common surname can be a
    # different person in the full list. An abbreviation ('K.' or 'Karl-C.' for
    # 'Karl-Christian') is informational, not a difference.
    rec_given = rec.get("given") or {}
    if rec_given and not truncated:
        abbreviated = []
        for surname, bg in bib_given_names(e.get("author", "")).items():
            rg = rec_given.get(surname)
            if not rg or _is_initial(rg):
                continue
            if _given_abbreviates(bg, rg):
                if bg != rg:
                    abbreviated.append(f"{show(surname)} ({bg!r}->{rg!r})")
            elif not _is_initial(bg) and deaccent(bg).lower() != deaccent(rg).lower():
                rep.add(Severity.WARN, e, f"[{source}] given name differs for "
                        f"{show(surname)!r}: bib={bg!r} vs {source}={rg!r}",
                        "record", category="metadata_mismatch")
            elif not _is_initial(bg) and bg != rg \
                    and deaccent(bg).lower() == deaccent(rg).lower() \
                    and not _miscapitalized_ok(bg):
                # Same name, but the bib's CAPITALIZATION deviates from the record's
                # ('VIncent' vs 'Vincent') -- a transcription typo, not a style choice
                # (a name is not freely re-cased the way a title is). Flag it toward
                # the record's form. `_miscapitalized_ok` allows legitimate intra-name
                # capitals (McDonald, O'Brien, von-particle forms the record may also
                # vary on) so only a genuine mis-case is reported.
                rep.add(Severity.WARN, e, f"[{source}] given name {bg!r} is miscapitalized "
                        f"vs the record ({rg!r})", "record", category="metadata_mismatch",
                        field="author", suggested={"field": "author", "from": bg, "to": rg})
        # Abbreviations are collapsed into one note per entry (they were noisy at
        # one line per author); the record's full names are advisory, not errors.
        if abbreviated:
            shown = ", ".join(abbreviated[:6]) + (" ..." if len(abbreviated) > 6 else "")
            rep.add(Severity.INFO, e, f"[{source}] {len(abbreviated)} given name(s) "
                    f"abbreviated vs the record; could expand: {shown}",
                    "record", category="style")
    return first_differs


# --- journal equivalence ---------------------------------------------------

def _load_journal_abbrev():
    """Load the curated abbreviation->full-title table. Both directions are
    indexed on a depunctuated key so a lookup works whichever form the bib uses."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "journal_abbrev.json")
    pairs = {}
    try:
        with open(path, encoding="utf-8") as fh:
            pairs = json.load(fh).get("abbreviations", {})
    except (OSError, json.JSONDecodeError):
        pairs = {}
    canon = {}
    for abbr, full in pairs.items():
        key = _journal_key(full)
        canon[_journal_key(abbr)] = key
        canon[key] = key
    return canon


def _journal_key(name):
    """Canonical key for a journal name: lowercased, depunctuated, despaced. HTML
    entities are decoded first so Crossref's 'Astronomy &amp; Astrophysics' does
    not leave a spurious 'amp' in the key (the '&' is punctuation, dropped here);
    a leading article ('The Astrophysical Journal' vs the ISO-4 'Astrophysical
    Journal') is dropped so the two forms key alike."""
    name = re.sub(r"^\s*the\s+", "", html.unescape(name), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Words dropped from a full title when checking an ISO-4 abbreviation against it
# (ISO-4 omits articles, conjunctions and prepositions).
_ISO4_STOPWORDS = {"of", "and", "the", "for", "in", "on", "a", "an", "to",
                   "der", "die", "und"}


def _journal_words(name):
    """Lowercased alphabetic word tokens of a journal name (periods/punctuation
    are separators, so 'Phys. Rev. B' -> ['phys','rev','b']). HTML entities are
    decoded first so '&amp;' does not contribute a bogus 'amp' token (the '&' is a
    separator), which would inflate the word count and break the ISO-4 check."""
    name = html.unescape(name)
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def _is_iso4_abbrev(abbrev, full):
    """Whether `abbrev` is a valid ISO-4-style abbreviation of `full`: period- and
    case-insensitive, the abbreviation words map IN ORDER and ONE-TO-ONE onto the
    full title's significant words (ISO-4 stopwords dropped), each abbreviation
    word equal to or a prefix of its full word. Every significant full-title word
    must be accounted for, so 'Nature' does NOT abbreviate 'Nature Physics'."""
    aw = _journal_words(abbrev)
    fw = [w for w in _journal_words(full) if w not in _ISO4_STOPWORDS]
    if not aw or len(aw) != len(fw):
        return False
    return all(f.startswith(a) for a, f in zip(aw, fw))


_JOURNAL_CANON = _load_journal_abbrev()


def _journal_equiv(a, b):
    """Whether two journal names denote the same journal. Accepts a name when it
    matches the other via (1) the curated abbreviation table, or (2) a valid
    ISO-4-style abbreviation in either direction. No substring matching, so
    'Nature' and 'Nature Physics' are NOT equated."""
    ka, kb = _journal_key(a), _journal_key(b)
    if not ka or not kb:
        return True   # nothing to compare
    if ka == kb:
        return True
    ca, cb = _JOURNAL_CANON.get(ka), _JOURNAL_CANON.get(kb)
    if ca is not None and cb is not None:
        # Both are in the curated table: it is authoritative. Equal canonical
        # titles mean the same journal; different ones mean genuinely different
        # journals -- do NOT fall through to the ISO-4 prefix heuristic, which
        # would wrongly equate e.g. 'ApJ' and 'ApJL' (one a prefix of the other).
        return ca == cb
    if (ca or ka) == (cb or kb):
        return True   # one side known to the table maps onto the other's key
    return _is_iso4_abbrev(a, b) or _is_iso4_abbrev(b, a)


# --- record comparison + parity --------------------------------------------

# Soft bibliographic fields compared between two metadata sources, as
# (key, label, normalizer). `number` is reported as "issue"; pages are
# dash-normalized so 'pp. 10-20' and '10--20' compare equal. The normalizer maps
# a raw value (str or int, possibly None/"") to its comparable string form.
def _soft(v):
    return str(v or "").strip()


_SOFT_FIELDS = [
    ("year", "year", _soft),
    ("volume", "volume", _soft),
    ("number", "issue", _soft),
    ("pages", "pages", lambda v: norm_pages(str(v or ""))),
]


# Embedded markup that a registry sometimes leaks into a title (Crossref serves
# math titles as MathML; some sources include stray XML/HTML entities). A title
# carrying any of these is unsafe to adopt verbatim into a .bib.
_MARKUP_RE = re.compile(r"<\s*/?\s*[a-z][\w:-]*[^>]*>|&[a-z]+;|&#\d+;", re.I)


def _has_markup(s):
    """True if a (title) string contains embedded XML/HTML/MathML markup or a stray
    control character -- a value that must NOT be suggested as a verbatim bib edit."""
    if not s:
        return False
    if _MARKUP_RE.search(s):
        return True
    return any(ord(c) < 32 and c not in "\t" for c in s)


def _strip_markup(s):
    """Remove embedded markup TAGS/entities from a title, keeping the text content,
    so the clean (non-math) parts remain comparable: '...in <mml:math>...171...Yb
    </mml:math> Atoms' -> '...in 171Yb Atoms'. The result is good enough to COMPARE
    against the bib (so deviations in the prose parts are still caught), but it has
    lost the math FORMATTING, so it must not be offered as a verbatim suggestion."""
    if not s:
        return s
    s = re.sub(r"<[^>]+>", "", s)            # drop tags
    s = re.sub(r"&[a-z]+;|&#\d+;", "", s, flags=re.I)  # drop entities
    s = "".join(c for c in s if ord(c) >= 32 or c == "\t")
    return re.sub(r"\s+", " ", s).strip()


def _bib_math_matches_record(btitle, raw_atitle):
    """True when the bib title carries LaTeX '$...$' math AND, with math/markup
    removed from both sides, the bib and record titles agree. In that case the bib
    already has the math in the canonical .bib form ('$...$') and the only difference
    from the record is LaTeX vs the registry's MathML -- a serialization artifact,
    not a defect, so the title check should be skipped entirely. (title_key strips
    both '$...$' and remaining markup, so it yields the bare prose for the compare.)"""
    if "$" not in btitle:
        return False
    # Compare space-INSENSITIVELY: stripping '$...$' vs MathML leaves the same tokens
    # but with cosmetic spacing differences around the math ('171yb' vs '171 yb').
    bk = title_key(btitle).replace(" ", "")
    rk = title_key(_strip_markup(raw_atitle)).replace(" ", "")
    return bool(bk) and bk == rk


def _bib_year_matches_a_version(bibval, rec):
    """True when `rec` is a versioned arXiv record (v1 year and a later vN year that
    differ) and the bib's year matches some version in that span -- i.e. the only
    reason the years disagree is which version is cited, not a wrong year. Bounds
    are inclusive; a bib year OUTSIDE the span is a genuine mismatch and not
    softened."""
    v1, vn = rec.get("year"), rec.get("updated_year")
    if not (v1 and vn) or v1 == vn:
        return False
    try:
        by = int(str(bibval).strip())
    except (TypeError, ValueError):
        return False
    return min(v1, vn) <= by <= max(v1, vn)


def _soft_field_diffs(e, rec):
    """Per-field bib-vs-record disagreements on the soft fields (year/volume/issue/
    pages), as (field, label, bib_value, record_value) using the ORIGINAL values --
    so each can be emitted as its own finding with a concrete 'bib -> record'
    suggested edit. A field only counts when BOTH sides supply a value and their
    NORMALIZED forms differ (so '1--2' vs '1-2' is not a difference). The record is
    the canonical reference: record_value is the proposed value."""
    out = []
    for key, label, norm in _SOFT_FIELDS:
        bibraw = str(e.get(key, "") or "").strip()
        recraw = str(rec.get(key, "") or "").strip()
        if bibraw and recraw and norm(bibraw) != norm(recraw):
            # The proposed value is handed back in its biblatex-canonical written
            # form (a page range as '920--926', not the registry's '920-926'), so an
            # applied suggestion does not itself trip the dash-style check.
            to = biblatex_pages(recraw) if key == "pages" else recraw
            out.append((key, label, bibraw, to))
    return out

def field_diffs(left, right, lname, rname, pages_substring_ok=False, skip_keys=()):
    """The soft bibliographic fields (year/volume/issue/pages) on which two records
    disagree, as formatted '<label> (<lname>=<lv> vs <rname>=<rv>)' strings. Both
    sides must supply a value for a field to count (a field one source omits is not
    a conflict). When `pages_substring_ok`, a page value contained in the other
    (a range vs one of its endpoints) is not treated as a difference. `skip_keys`
    drops named fields outright (e.g. 'year' for a superseded preprint, where the
    preprint-vs-journal year gap is expected, not a conflict). Used for CROSS-SOURCE
    comparison (record vs record), where neither side is the bib; the bib-vs-record
    path uses _soft_field_diffs, which carries per-field suggestions."""
    out = []
    for key, label, norm in _SOFT_FIELDS:
        if key in skip_keys:
            continue
        lv, rv = norm(left.get(key)), norm(right.get(key))
        if lv and rv and lv != rv:
            if key == "pages" and pages_substring_ok and (lv in rv or rv in lv):
                continue
            out.append(f"{label} ({lname}={lv} vs {rname}={rv})")
    return out


def compare_against_record(e, rec, source, rep):
    """RECORD layer: flag where the entry disagrees with its id-resolved record.
    The DOI/arXiv id already establishes identity, so individual field
    disagreements (author, title, year/volume/pages, journal) are metadata
    discrepancies a human should check -- warnings, not errors. The one true
    error is `id_resolves_wrong_record`: the first author AND the title both
    differ strongly, the fingerprint of a copy-pasted wrong identifier."""
    # The offline entrytype heuristic may have guessed "web/press item -> @online"
    # for an @article whose only locator was a url (e.g. a journal PDF on a personal
    # site). If it RESOLVED to a real journal record, that guess is disproved -- the
    # entry is a genuine journal article -- so withdraw it. Only for a journal record
    # (a real venue, not arXiv and not a book), and only for an @article (a book/
    # chapter keeps its own, correct, book-type suggestion).
    rec_journal = (rec.get("journal") or "").strip()
    if e.etype == "article" and rec_journal and "arxiv" not in rec_journal.lower():
        rep.withdraw(e.key, "entrytype_suggestion")
    # When the resolved record reports a document type (INSPIRE does) that is NOT a
    # journal article -- a thesis or proceedings -- a @article entry is mis-typed.
    # Withdraw the offline guess (which may have said '@online') and point at the
    # correct biblatex type. This is how a thesis cited by its INSPIRE page (no DOI/
    # arXiv id) gets the right '@thesis' suggestion instead of '@online'.
    doc_type = (rec.get("document_type") or "").lower()
    if e.etype == "article" and doc_type in ("thesis", "proceedings", "book chapter"):
        rep.withdraw(e.key, "entrytype_suggestion")
        target = {"thesis": "@thesis", "proceedings": "@inproceedings/@proceedings",
                  "book chapter": "@incollection"}[doc_type]
        rep.add(Severity.WARN, e, f"[{source}] the record is a {doc_type}, not a "
                f"journal article -- use {target} instead of @article", "record",
                category="entrytype_suggestion", field="journal")

    first_differs = _compare_authors(e, rec, source, rep)

    # Title: with identity fixed by the id, a strong mismatch is a discrepancy to
    # verify (WARN). A dropped subtitle ('Combinatorial Optimization' vs '...:
    # Theory and Algorithms') is informational, not a difference.
    title_differs_strongly = False
    btitle, raw_atitle = e.get("title", ""), rec.get("title", "")
    # The record title sometimes arrives with embedded markup (Crossref serves math
    # titles as raw MathML). Strip the TAGS for COMPARISON so the clean (non-math)
    # parts are still checked -- but remember markup was present so no mangled value
    # is offered as a verbatim suggestion (the `to` is withheld, the finding stays).
    rec_has_markup = _has_markup(raw_atitle) and not _has_markup(btitle)
    atitle = _strip_markup(raw_atitle) if rec_has_markup else raw_atitle
    # A suggested 'to' is the record title only when it is safe to paste verbatim;
    # when the record carried markup the stripped form lost formatting, so withhold it.
    safe_to = None if rec_has_markup else atitle
    mangle_note = " (record title contains markup; verify the exact form manually)" \
        if rec_has_markup else ""

    def _title_sug():
        return {"field": "title", "from": btitle, "to": safe_to} if safe_to else None

    bt, at = title_key(btitle), title_key(atitle)
    # When the record carries MathML math AND the bib already has the math in proper
    # LaTeX '$...$' form, and the titles agree once math is stripped from BOTH sides,
    # the bib title is already correct -- the only "difference" is LaTeX vs the
    # registry's MathML serialization, not a defect. Skip ALL title findings: the
    # bib's '$...$' is the canonical .bib form, so there is nothing to fix or nudge.
    if rec_has_markup and _bib_math_matches_record(btitle, raw_atitle):
        bt = at = ""
    # Same title, wrong casing: when the normalized titles agree but the bib is
    # SHOUTED in uppercase, the record carries the canonical casing -- recommend
    # adopting it, and withdraw the offline 'looks miscased' guess (this is the
    # authoritative form). Only when the record is itself sensibly cased.
    if bt and at and bt == at and btitle != atitle \
            and title_is_miscased(btitle) and not title_is_miscased(atitle):
        rep.withdraw(e.key, "title_case")
        rep.add(Severity.INFO, e, f"[{source}] title casing differs from the record; "
                f"adopt the record's casing{mangle_note}:\n        bib:    {btitle[:90]}\n"
                f"        {source}: {atitle[:90]}", "record", category="title_case",
                field="title", suggested=_title_sug())
    elif bt and at and bt == at and not title_is_miscased(btitle) \
            and _title_punct_key(btitle) != _title_punct_key(atitle):
        # The title matches the record as the SAME work (folds equal) and is not just
        # a casing difference, but its punctuation/wording deviates from the record's
        # canonical form -- a hyphen ('open source' vs 'open-source'), '&' vs 'and', a
        # spacing or colon difference. The record is the source of record, so nudge
        # toward its exact form -- a NOTE (it renders fine; this is a metadata-quality
        # improvement), not a warning. If the record carried markup the deviation in
        # the CLEAN parts is still reported, just without an auto-applicable 'to'.
        rep.add(Severity.INFO, e, f"[{source}] title matches the record but its "
                f"punctuation/wording differs from the canonical form{mangle_note}",
                "record", category="title_style", field="title", suggested=_title_sug())
    if bt and at and bt != at:
        if title_is_shortened(btitle, atitle):
            rep.add(Severity.INFO, e, f"[{source}] title is a shortened form of the "
                    f"record's (likely a dropped subtitle)", "record",
                    category="metadata_mismatch")
        else:
            overlap = title_overlap(btitle, atitle)
            # A near-zero overlap against a record reached via a book/proceedings
            # DOI or ISBN is almost always a granularity mismatch -- the id
            # resolves to the *volume*, whose title is the book title, not the
            # chapter being cited. Down-rank to a note rather than a WARN (and do
            # not let it feed the wrong-record error), since the entry-type rule
            # already points at the real fix (use @incollection/@inproceedings).
            # The comparison above already used the markup-STRIPPED record title, so
            # the clean parts are checked; the suggested 'to' (safe_to) is withheld
            # and a 'verify manually' note added when the record carried markup, so a
            # mangled value is never offered as a verbatim edit (the Rec #4 safety).
            if overlap <= 0.1 and is_container_granularity(e):
                # Deliberately a note, not 'metadata_mismatch' (a warning): the
                # title "differs" only because the id resolved to the containing
                # volume, so this points at the entry type rather than a data error.
                rep.add(Severity.INFO, e, f"[{source}] title differs from the record "
                        f"(overlap {overlap:.0%}); the id resolves to the containing "
                        f"book/volume, not the chapter -- check the entry type", "record",
                        category="container_granularity")
            elif overlap < 0.6:
                title_differs_strongly = True
                rep.add(Severity.WARN, e, f"[{source}] title differs from record (overlap {overlap:.0%}){mangle_note}:\n"
                        f"        bib:    {btitle[:90]}\n"
                        f"        {source}: {atitle[:90]}", "record",
                        category="metadata_mismatch", field="title", suggested=_title_sug())
            else:
                rep.add(Severity.INFO, e, f"[{source}] title differs slightly (overlap {overlap:.0%}){mangle_note}",
                        "record", category="metadata_mismatch", field="title", suggested=_title_sug())

    # The single genuine wrong-paper error: identity-level fields ALL point
    # elsewhere, so the id itself is probably wrong (copy-paste). Both the first
    # author and the title must diverge for this to fire.
    if first_differs and title_differs_strongly:
        rep.add(Severity.ERROR, e, f"[{source}] first author AND title both differ from "
                f"the record this id resolves to -- the doi/arXiv id may be wrong",
                "record", category="id_resolves_wrong_record")

    # Soft metadata: year/volume/pages legitimately diverge between a bib and a
    # registry (online-first vs issue year, supplement volumes, eLocator vs page
    # range). The id fixes identity, so even a cluster is a metadata discrepancy
    # to check, not a wrong paper -- a warning either way.
    #
    # When there IS a locator mismatch, also fold in any locator field the bib
    # leaves empty but the record supplies (e.g. number=(empty) vs 2229), so the
    # reader sees all the related facts on one line. This asserts nothing about WHY
    # (no 'you packed volume.number' inference) -- both halves are literally true:
    # the volume differs AND the number is absent. We only co-locate when there is
    # already a real locator mismatch; an entry that merely omits 'number' with no
    # other conflict keeps its benign parity note rather than gaining a warning.
    # Soft bibliographic fields (year/volume/issue/pages): one finding PER field, so
    # each carries a concrete 'bib -> record' suggested edit (the record is the
    # canonical reference; the suggestion leans toward conforming the bib to it) and
    # its own severity. All four appear in a rendered citation, so a differing value
    # is render-affecting -> WARN.
    folded_missing = set()
    for fld, label, bibval, recval in _soft_field_diffs(e, rec):
        # arXiv preprints are VERSIONED: v1 and a later vN can carry different years
        # (<published> vs <updated>). When the bib's year matches SOME version in the
        # record's version span [year, updated_year] -- just not the one the record
        # reports -- the bib is not wrong, only under-specified. Best practice is to
        # cite the version explicitly (arXiv:ID vN) so the year is unambiguous. So
        # emit a version-pinning NOTE, not a corrective 'year 2024 -> 2023' warning
        # whose direction is the author's choice, not a fact.
        if fld == "year" and _bib_year_matches_a_version(bibval, rec):
            v1, vn = rec.get("year"), rec.get("updated_year")
            rep.add(Severity.INFO, e, f"[{source}] bib year {bibval} matches one of the "
                    f"arXiv versions (v1 {v1}, latest {vn}) but not the other; the year "
                    f"depends on which version is cited -- pin the version (e.g. "
                    f"'arXiv:ID v1') so it is unambiguous",
                    "record", category="preprint_version", field="year")
            continue
        # The before -> after lives in the suggested tail, so the prose stays terse
        # ('year differs') rather than repeating 'bib=X vs crossref=Y'.
        rep.add(Severity.WARN, e, f"[{source}] {label} differs",
                "record", category="metadata_mismatch",
                field=fld, suggested={"field": fld, "from": bibval, "to": recval})

    bj, aj = clean_tex(e.get("journal", "")).lower(), clean_tex(rec.get("journal", "")).lower()
    if bj and aj and "arxiv" not in bj and "arxiv" not in aj and not _journal_equiv(bj, aj):
        # Journal renders in the citation -> WARN, with the record's name suggested.
        rep.add(Severity.WARN, e, f"[{source}] journal differs", "record",
                category="metadata_mismatch", field="journal",
                suggested={"field": "journal", "from": e.get("journal", ""),
                           "to": rec.get("journal", "")})

    _suggest_parity(e, rec, source, rep, skip=folded_missing)


def compare_sources(e, records, rep, skip_year=False):
    """CROSS-SOURCE (Layer 4): compare authoritative records against EACH OTHER,
    not just against the bib. `records` is {source_name: record}. When two sources
    disagree on a data field (year/volume/issue/pages, or a genuinely different
    journal) it is a `source_conflict` WARN naming both. Purely stylistic
    differences (title casing, a full journal title vs its ISO-4 abbreviation) are
    NOT flagged -- both forms are valid. This surfaces stale or corrupted
    authoritative metadata the single-source comparison cannot see.

    `skip_year` drops the year field: for a superseded preprint the entry is checked
    against the preprint it cites, so the preprint-vs-journal year gap (e.g. arXiv
    2021 vs INSPIRE's journal 2022) is expected and already reported as
    preprint_superseded -- not a second 'sources disagree on year' finding."""
    skip = {"year"} if skip_year else ()
    names = [n for n in records if records.get(n)]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            sa, sb = names[i], names[j]
            ra, rb = records[sa], records[sb]
            # Data conflicts -> WARN (one finding listing all disagreeing fields).
            # A page value contained in the other (range vs an endpoint) is not a
            # conflict here, since neither side is the bib being checked.
            data = field_diffs(ra, rb, sa, sb, pages_substring_ok=True, skip_keys=skip)
            if data:
                rep.add(Severity.WARN, e, "sources disagree: " + "; ".join(data),
                        "record", category="source_conflict")
            # Journals: a full title vs its abbreviation (or any two forms
            # _journal_equiv accepts) is NOT a discrepancy -- both are valid, so it
            # is not flagged at all. Only journals that are genuinely different
            # (not equivalent) are a real cross-source conflict.
            ja, jb = ra.get("journal", ""), rb.get("journal", "")
            if ja and jb and "arxiv" not in ja.lower() and "arxiv" not in jb.lower() \
                    and not _journal_equiv(ja, jb):
                rep.add(Severity.WARN, e, f"sources disagree on the journal: "
                        f"{sa}={ja!r} vs {sb}={jb!r}", "record", category="source_conflict")


def _suggest_parity(e, rec, source, rep, skip=()):
    """Completeness notes: data the record carries that the bib omits, so a user
    can opt into registry parity. These are never errors -- the bib is correct,
    just less complete. Crossref only (arXiv lacks structured bibliographic
    fields); bibliographic fields are skipped for preprints, where they N/A.

    `skip` is the set of fields already reported on the metadata_mismatch locator
    line (a missing locator co-located with a sibling mismatch); we do not also
    emit a parity note for them, to avoid stating the same fact twice."""
    if source != "crossref":
        return
    if not e.get("doi").strip() and rec.get("doi"):
        # A parity note carries the value to add as a structured patch (field + to),
        # so a consumer can apply it from the finding alone without re-reading
        # canonical_record. There is no 'from' -- the field is absent in the bib.
        rep.add(Severity.INFO, e, f"[{source}] record has a DOI the entry omits: "
                f"{rec['doi']}", "record", category="parity_suggestion", field="doi",
                suggested={"field": "doi", "to": rec["doi"]})
    if not is_preprint(e):
        for fld, rval in (("volume", rec.get("volume")),
                          ("number", rec.get("number")),
                          ("pages", rec.get("pages")),
                          ("year", str(rec.get("year") or ""))):
            if rval and not e.get(fld).strip() and fld not in skip:
                # Hand back the biblatex-canonical written form so the suggested value
                # would not itself trip a style check -- a page range uses '--', not
                # the registry's single hyphen ('920-926' -> '920--926').
                val = biblatex_pages(rval) if fld == "pages" else str(rval)
                rep.add(Severity.INFO, e, f"[{source}] record has {fld} {val!r} "
                        f"the entry omits", "record", category="parity_suggestion",
                        field=fld, suggested={"field": fld, "to": val})
                # The offline missing_locator note says the SAME thing generically
                # ("missing 'volume'"); this parity note names the value to add, so
                # it supersedes the generic one -- the fact appears once.
                if fld in ("volume", "pages"):
                    rep.withdraw(e.key, "missing_locator")
