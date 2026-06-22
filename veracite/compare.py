"""Comparison layer: flag where an entry disagrees with its resolved record(s).

The DOI/arXiv id already establishes identity, so individual field disagreements
(author, title, year/volume/pages, journal) are metadata discrepancies a human
should check -- warnings, not errors. The one true error is the case where the
first author AND the title both differ strongly (the id likely points elsewhere).
Also compares authoritative sources against each other (cross-source conflicts)
and suggests fields the record carries that the entry omits (parity).
"""

import html
import json
import os
import re

from .normalize import (bib_given_names, clean_tex, deaccent,
                        is_container_granularity, is_preprint, norm_pages,
                        split_authors, title_is_miscased)
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
    truncated = "others" in e.get("author", "").lower()
    # An 'and others' truncation is faithful -- not lossy -- when the authoritative
    # record enumerates no more names than the bib already carries (a collaboration
    # the record holds as a single name, or a list the bib already gives in full).
    # Then withdraw the offline author_completeness finding: there are no dropped
    # names to recover. Count ALL bib name tokens, including a collaboration name
    # that split_authors drops, so 'LHCb Collaboration and others' counts as one.
    # Done before the empty-list guard below so a pure-collaboration author (which
    # split_authors reduces to []) is still reconciled. If the record lists MORE
    # names, the finding stands -- those are exactly what belongs in the .bib.
    if truncated and rec_authors:
        bib_name_count = sum(
            1 for a in re.split(r"\s+and\s+", e.get("author", "").replace("\n", " "))
            if a.strip() and a.strip().lower() != "others")
        if len(rec_authors) <= bib_name_count:
            rep.withdraw(e.key, "author_completeness")
    if not (bib_authors and rec_authors):
        return False
    bib_only = _author_diff(bib_authors, rec_authors)
    rec_only = _author_diff(rec_authors, bib_authors)
    first_differs = not _surname_match(bib_authors[0], rec_authors[0])

    if first_differs:
        rep.add(Severity.WARN, e, f"[{source}] first author differs: "
                f"bib={bib_authors[0]!r} vs {source}={rec_authors[0]!r}",
                "record", category="metadata_mismatch")
    if bib_only:
        rep.add(Severity.WARN, e, f"[{source}] author(s) in bib not in record: "
                + ", ".join(sorted(bib_only)), "record", category="metadata_mismatch")
    if rec_only and not truncated:
        rep.add(Severity.WARN, e, f"[{source}] author(s) in record missing from bib: "
                + ", ".join(sorted(rec_only)), "record", category="metadata_mismatch")
    if not truncated and not bib_only and not rec_only and not first_differs \
            and bib_authors != rec_authors:
        rep.add(Severity.WARN, e, f"[{source}] same authors but in a different order "
                f"than the record", "record", category="metadata_mismatch")
    initials = [a for a in bib_authors if len(a) <= 1]
    if initials and not truncated:
        rep.add(Severity.INFO, e, f"[{source}] author surname(s) reduced to initials "
                f"({', '.join(initials)}); check name parsing", "record",
                category="metadata_mismatch")

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
                    abbreviated.append(f"{surname} ({bg!r}->{rg!r})")
            elif not _is_initial(bg) and deaccent(bg).lower() != deaccent(rg).lower():
                rep.add(Severity.WARN, e, f"[{source}] given name differs for "
                        f"{surname!r}: bib={bg!r} vs {source}={rg!r}",
                        "record", category="metadata_mismatch")
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

# The locator group: the soft fields that together place an article within a
# journal. A locator the bib leaves empty is co-located with a sibling locator
# mismatch (see field_diffs / compare_against_record) rather than reported as a
# separate parity note, so all the related facts read on one line.
_LOCATOR_FIELDS = {"volume", "number", "pages"}


def field_diffs(left, right, lname, rname, pages_substring_ok=False,
                report_left_missing=None):
    """The soft bibliographic fields (year/volume/issue/pages) on which two records
    disagree, as formatted '<label> (<lname>=<lv> vs <rname>=<rv>)' strings. Both
    sides must supply a value for a field to count (a field one source omits is not
    a conflict). When `pages_substring_ok`, a page value contained in the other
    (a range vs one of its endpoints) is not treated as a difference -- used for
    cross-source comparison, where neither side is the bib being checked.

    `report_left_missing`, when given, is a set of field keys for which a value the
    RIGHT side supplies but the LEFT leaves empty is also surfaced (as
    '<label> (<lname>=(empty) vs <rname>=<rv>)'). Used only for bib-vs-record on the
    locator group: it co-locates a missing locator with its sibling locator
    mismatch -- the same true facts on one line -- instead of a separate parity
    note, so a reader sees the whole locator picture at once (e.g. a 'volume=475.x'
    mismatch alongside 'number=(empty) vs 2229'). The set of keys actually reported
    this way is also returned, so the caller can suppress the duplicate parity note.
    Returns (diffs, reported_missing_keys)."""
    out, reported_missing = [], set()
    for key, label, norm in _SOFT_FIELDS:
        lv, rv = norm(left.get(key)), norm(right.get(key))
        if lv and rv:
            if lv == rv:
                continue
            if key == "pages" and pages_substring_ok and (lv in rv or rv in lv):
                continue
            out.append(f"{label} ({lname}={lv} vs {rname}={rv})")
        elif rv and not lv and report_left_missing and key in report_left_missing:
            out.append(f"{label} ({lname}=(empty) vs {rname}={rv})")
            reported_missing.add(key)
    return out, reported_missing


def compare_against_record(e, rec, source, rep):
    """RECORD layer: flag where the entry disagrees with its id-resolved record.
    The DOI/arXiv id already establishes identity, so individual field
    disagreements (author, title, year/volume/pages, journal) are metadata
    discrepancies a human should check -- warnings, not errors. The one true
    error is `id_resolves_wrong_record`: the first author AND the title both
    differ strongly, the fingerprint of a copy-pasted wrong identifier."""
    first_differs = _compare_authors(e, rec, source, rep)

    # Title: with identity fixed by the id, a strong mismatch is a discrepancy to
    # verify (WARN). A dropped subtitle ('Combinatorial Optimization' vs '...:
    # Theory and Algorithms') is informational, not a difference.
    title_differs_strongly = False
    btitle, atitle = e.get("title", ""), rec.get("title", "")
    bt, at = title_key(btitle), title_key(atitle)
    # Same title, wrong casing: when the normalized titles agree but the bib is
    # SHOUTED in uppercase, the record carries the canonical casing -- recommend
    # adopting it, and withdraw the offline 'looks miscased' guess (this is the
    # authoritative form). Only when the record is itself sensibly cased.
    if bt and at and bt == at and btitle != atitle \
            and title_is_miscased(btitle) and not title_is_miscased(atitle):
        rep.withdraw(e.key, "title_case")
        rep.add(Severity.INFO, e, f"[{source}] title casing differs from the record; "
                f"adopt the record's casing:\n        bib:    {btitle[:90]}\n"
                f"        {source}: {atitle[:90]}", "record", category="title_case")
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
                rep.add(Severity.WARN, e, f"[{source}] title differs from record (overlap {overlap:.0%}):\n"
                        f"        bib:    {btitle[:90]}\n"
                        f"        {source}: {atitle[:90]}", "record",
                        category="metadata_mismatch")
            else:
                rep.add(Severity.INFO, e, f"[{source}] title differs slightly (overlap {overlap:.0%})",
                        "record", category="metadata_mismatch")

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
    diffs, _ = field_diffs(e, rec, "bib", source)
    folded_missing = set()
    if any(d.split(" ")[0] in ("volume", "issue", "pages") for d in diffs):
        diffs, folded_missing = field_diffs(e, rec, "bib", source,
                                            report_left_missing=_LOCATOR_FIELDS)
    if diffs:
        rep.add(Severity.WARN, e, f"[{source}] differs from record: " + "; ".join(diffs),
                "record", category="metadata_mismatch")

    bj, aj = clean_tex(e.get("journal", "")).lower(), clean_tex(rec.get("journal", "")).lower()
    if bj and aj and "arxiv" not in bj and "arxiv" not in aj and not _journal_equiv(bj, aj):
        rep.add(Severity.WARN, e, f"[{source}] journal differs: bib={e.get('journal', '')!r} "
                f"vs {source}={rec.get('journal', '')!r}", "record", category="metadata_mismatch")

    _suggest_parity(e, rec, source, rep, skip=folded_missing)


def compare_sources(e, records, rep):
    """CROSS-SOURCE (Layer 4): compare authoritative records against EACH OTHER,
    not just against the bib. `records` is {source_name: record}. When two sources
    disagree on a data field (year/volume/issue/pages, or a genuinely different
    journal) it is a `source_conflict` WARN naming both. Purely stylistic
    differences (title casing, a full journal title vs its ISO-4 abbreviation) are
    NOT flagged -- both forms are valid. This surfaces stale or corrupted
    authoritative metadata the single-source comparison cannot see."""
    names = [n for n in records if records.get(n)]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            sa, sb = names[i], names[j]
            ra, rb = records[sa], records[sb]
            # Data conflicts -> WARN (one finding listing all disagreeing fields).
            # A page value contained in the other (range vs an endpoint) is not a
            # conflict here, since neither side is the bib being checked.
            data, _ = field_diffs(ra, rb, sa, sb, pages_substring_ok=True)
            if data:
                rep.add(Severity.WARN, e, f"sources disagree: " + "; ".join(data),
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
        rep.add(Severity.INFO, e, f"[{source}] record has a DOI the entry omits: "
                f"{rec['doi']}", "record", category="parity_suggestion", field="doi")
    if not is_preprint(e):
        for fld, rval in (("volume", rec.get("volume")),
                          ("number", rec.get("number")),
                          ("pages", rec.get("pages")),
                          ("year", str(rec.get("year") or ""))):
            if rval and not e.get(fld).strip() and fld not in skip:
                rep.add(Severity.INFO, e, f"[{source}] record has {fld} {rval!r} "
                        f"the entry omits", "record", category="parity_suggestion")
