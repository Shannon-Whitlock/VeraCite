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
                        clean_tex, deaccent, is_collaboration, is_container_granularity,
                        is_preprint, is_truncated, norm_pages, split_authors,
                        title_is_miscased)
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


def _reconstructed_surnames(rec_authors, given_full):
    """Folded surnames a registry author may ALSO be known by once a mis-split
    compound surname is rejoined. Crossref sometimes files 'A. Lecavelier des Etangs'
    as given='A. Lecavelier', family='des Etangs' (or 'Alain Lecavelier' / 'des
    Etangs'); the real surname 'Lecavelier des Etangs' then never matches the bib.

    The leading given tokens are the actual first name(s); only a TRAILING run of them
    is the shifted surname part. We don't know how many, so we emit every suffix
    rejoin: for given 'Alain Lecavelier' + family 'des Etangs' we yield both
    'lecavelierdesetangs' (drop 'Alain') and 'alainlecavelierdesetangs', so the bib's
    'Lecavelier des Etangs' matches the first without the real first name 'Alain'
    polluting it. Initial-only tokens ('A.') are never surname parts and stop the run
    (an ordinary 'Alan Smith' yields nothing once initials/first-name-only are gone)."""
    extra = []
    for surname in rec_authors:
        full = (given_full or {}).get(surname, "")
        if not full:
            continue
        toks = full.split()
        # Build suffix rejoins from the shortest (just the last given token) upward,
        # stopping at an initial (a first-initial is not part of a surname).
        for start in range(len(toks) - 1, -1, -1):
            if _is_initial(toks[start]):
                break
            rejoined = _clean_name_key("".join(toks[start:]) + surname)
            if rejoined and rejoined != surname:
                extra.append(rejoined)
    return extra


def _author_diff(left, right, right_extra=()):
    """Surnames in `left` with no match in `right` (particle-aware). `right_extra`
    adds reconstructed compound surnames a `right` author may also be known by, so a
    bib's correct compound surname is not falsely reported as 'not in record'."""
    pool = list(right) + list(right_extra)
    return [x for x in left if not any(_surname_match(x, y) for y in pool)]


def _is_initial(name):
    """True if a given name is initials only ('L', 'L.', 'J.R.', 'J.-P.', 'J. R.')
    rather than a spelled-out name: every alphabetic run is a single letter."""
    tokens = re.findall(r"[A-Za-z]+", name)
    return not tokens or all(len(t) == 1 for t in tokens)


def _clean_name_key(name):
    """A surname reduced to its comparable form for DEVIATION detection: deaccented,
    lowercased, with internal whitespace collapsed and name punctuation
    (hyphens/apostrophes/periods/commas -- all legitimate in or around names, or
    record noise) normalized away. What remains is the bare letters -- so 'Cohen' and
    'Cohén' and 'Cohen.' all match, but 'Cohen1' (a stray digit) or 'Cohen*' (a
    footnote mark) does NOT, since the extra character survives. The comma is included
    because Crossref sometimes leaves a trailing comma in the `family` field ('Gaume,',
    'Wilson,') -- record noise, not a real deviation of the bib's clean surname. This
    is how the deviation check stays robust without enumerating bad-character classes:
    anything that is not an accent/case/punctuation difference shows up as a real
    deviation from the record's clean name.

    The stripped punctuation covers Unicode hyphen/dash variants too (U+2010 hyphen,
    U+2011 non-breaking hyphen, the U+2012-2015 dashes, U+2212 minus), not just the
    ASCII '-': Crossref serves a hyphenated surname with a real Unicode hyphen
    ('Glover‐Kapfer', U+2010) while the bib uses the ASCII '-' ('Glover-Kapfer') -- the
    SAME name, so it must not read as a deviation (and never be 'corrected' toward the
    non-ASCII form). Same idea as the curly apostrophe (U+2019) already handled."""
    s = deaccent(name).lower()
    return re.sub(r"[\s.,'’‐-―−-]+", "", s)


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


# Unicode hyphen/dash variants that are TYPOGRAPHIC equivalents of the ASCII '-':
# U+2010 hyphen, U+2011 non-breaking hyphen, U+2012-2015 figure/en/em/horizontal-bar
# dashes, U+2212 minus. Crossref serves a hyphenated title/name with one of these
# ('Camera‐trapping', U+2010) where the bib uses ASCII '-' -- the SAME punctuation, so
# it must not register as a deviation (nor be 'corrected' toward the non-ASCII form).
_UNICODE_DASHES = "‐‑‒–—―−"


def _norm_dashes(s):
    """Fold every Unicode hyphen/dash variant to the ASCII '-' so a codepoint-only
    difference is not mistaken for a punctuation difference."""
    return re.sub("[" + _UNICODE_DASHES + "]", "-", s)


def _title_punct_key(title):
    """A title reduced to a CASE- and ACCENT-insensitive form that PRESERVES
    punctuation -- so a hyphen, '&', colon or spacing difference survives while a
    casing or accent difference does not. Used to detect a title that matches the
    record as the same work (title_key equal) but whose written PUNCTUATION deviates
    from the record's canonical form (e.g. 'open source' vs 'open-source'). De-TeX
    first so brace-protection ('{Yb}') is not counted as a difference; fold Unicode
    dash variants to ASCII so 'Camera-trapping' vs 'Camera‐trapping' (U+2010) is NOT a
    deviation (same hyphen, different codepoint).

    Whitespace immediately touching ':' / ';' / ',' is collapsed away (not just
    runs of whitespace generally) so a multi-line .bib field's embedded newline/tab
    -- 'Colloquium\\n\\t\\t: Strongly...' -- compares equal to the record's single-line
    'Colloquium: Strongly...'. This is wrapping noise, not an authored space, so it
    must not register as the punctuation deviation this key exists to detect; a
    genuine word-level spacing difference ('open source' vs 'open-source') has no
    punctuation mark there at all and is unaffected.

    BibTeX '--' (two ASCII hyphens) is the en-dash separator; it must compare equal
    to the Unicode '–' (U+2013) that registries serve for the same character. After
    _norm_dashes folds every Unicode dash to '-', a residual '--' is still two hyphens
    rather than the single '-' the fold produced, so fold '--' -> '-' as a second step
    so both representations of the en-dash produce the same key."""
    s = _norm_dashes(deaccent(clean_tex(title))).lower()
    s = re.sub(r"--+", "-", s)   # BibTeX en-dash == Unicode en-dash for comparison
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s*([:;,])\s*", r"\1", s)


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
    # If the author field already has a glued-'and' defect (caught by the offline
    # glued_and_separator rule), skip the record comparison entirely: the parser
    # merged two authors into one name, so every downstream finding (given-name
    # differs, author missing from bib) is a symptom of the same root cause.
    # The offline finding is the actionable one; the record comparison would only
    # produce noise on top of it.
    from .rules import _GLUED_AND_RE  # avoid module-level cycle
    if _GLUED_AND_RE.search(e.get("author", "")):
        return False
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
    # Crossref may mis-split a compound surname into given+family ('A. Lecavelier' /
    # 'des Etangs' for 'Lecavelier des Etangs'); reconstruct the real surname so the
    # bib's correct form is not falsely flagged as a different author in either
    # direction. `rec_aug` is the record surname pool with the rejoined forms added.
    given_full = rec.get("given_full") or {}
    rec_extra = _reconstructed_surnames(rec_authors, given_full)
    bib_only = _author_diff(bib_authors, rec_authors, rec_extra)
    # rec_only: a record author is "missing from bib" only if NEITHER its filed
    # surname NOR its reconstructed compound surname matches a bib author -- so the
    # mis-split 'des Etangs' (real surname 'Lecavelier des Etangs') is found in the
    # bib and not falsely reported missing.
    def _rec_in_bib(surname):
        if any(_surname_match(surname, b) for b in bib_authors):
            return True
        rejoined = _reconstructed_surnames([surname], given_full)
        return any(_surname_match(r, b) for r in rejoined for b in bib_authors)
    rec_only = [x for x in rec_authors if not _rec_in_bib(x)]
    # First-author match privileges position: fold the first record author plus any
    # reconstruction OF that first author, not the whole list.
    first_extra = _reconstructed_surnames(rec_authors[:1], rec.get("given_full"))
    first_differs = not (_surname_match(bib_authors[0], rec_authors[0])
                         or any(_surname_match(bib_authors[0], y) for y in first_extra))

    # Drop empty surnames (a malformed author like '{}, A.' folds to '') -- listing
    # them yields a finding with an empty name ('author(s) in bib not in record: '),
    # which states nothing actionable. A blank author IS a defect, but it is reported
    # by the offline author-format rule pointing at the exact token, not here.
    bib_only = [a for a in bib_only if show(a).strip()]
    rec_only = [a for a in rec_only if show(a).strip()]
    # When the first author already differs, the same person also shows up in bib_only
    # and rec_only -- the 'first author differs' line says it once, so don't restate it
    # two more ways. Suppress the first-author pair from the set-difference lists.
    # When the record indexes under a collaboration name and the bib has individual
    # authors, the individual names being absent from the record is a consequence of
    # the collaboration indexing -- not an independent defect. The "first author
    # differs" finding already flags the mismatch; suppress the redundant "in bib
    # not in record" list so the user gets one clear message, not two.
    rec_first_is_collab = rec_authors and is_collaboration(show(rec_authors[0]))
    if first_differs:
        bib_only = [a for a in bib_only if not _surname_match(a, bib_authors[0])]
        rec_only = [a for a in rec_only if not _surname_match(a, rec_authors[0])]
        rep.add(Severity.WARN, e, f"[{source}] first author differs: "
                f"bib={show(bib_authors[0])!r} vs {source}={show(rec_authors[0])!r}"
                + (f" (record uses a collaboration name; individual authors are not "
                   f"listed separately in the record)" if rec_first_is_collab else ""),
                "record", category="metadata_mismatch")
    if bib_only and not rec_first_is_collab:
        rep.add(Severity.WARN, e, f"[{source}] author(s) in bib not in record: "
                + ", ".join(sorted(show(a) for a in bib_only)), "record",
                category="metadata_mismatch")
    if rec_only and not truncated:
        rep.add(Severity.WARN, e, f"[{source}] author(s) in record missing from bib: "
                + ", ".join(sorted(show(a) for a in rec_only)), "record",
                category="metadata_mismatch")
    # Order check: same set, different sequence. Compare position-by-position with
    # the same surname matching (particles + compound reconstruction) so a mis-split
    # surname that lines up in order is NOT misread as a re-ordering.
    def _same_position(b, r):
        if _surname_match(b, r):
            return True
        return any(_surname_match(b, x) for x in _reconstructed_surnames([r], given_full))
    in_order = len(bib_authors) == len(rec_authors) and \
        all(_same_position(b, r) for b, r in zip(bib_authors, rec_authors))
    if not truncated and not bib_only and not rec_only and not first_differs \
            and not in_order:
        rep.add(Severity.WARN, e, f"[{source}] same authors but in a different order "
                f"than the record", "record", category="metadata_mismatch")
    # A surname folded to a single letter looks like a mis-parsed initial. An EMPTY
    # surname ('' from a malformed '{}, A.') is excluded: it would render as an empty
    # '()' that names nothing, and the offline author-format rule already flags the
    # malformed token at its exact line.
    initials = [a for a in bib_authors if 0 < len(a) <= 1 and show(a).strip()]
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
            elif not _is_initial(bg) and _clean_name_key(bg) != _clean_name_key(rg):
                # Compare via _clean_name_key (the same normalizer the surname-deviation
                # check uses): deaccent + ligature + hyphen/punctuation folding, so an
                # ASCII vs Unicode hyphen ('Ida-Marie' vs 'Ida‐Marie' U+2010) or an
                # accent/ligature difference is not mistaken for a different given name.
                # Severity is INFO: most citation styles render only surnames or initials,
                # so a given-name discrepancy (even 'Minore' vs 'Minori') does not affect
                # how a citation renders or whether the paper is findable. It is a
                # completeness/accuracy nudge, not a rendering-affecting data problem.
                rep.add(Severity.INFO, e, f"[{source}] given name differs for "
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
    indexed on a depunctuated key so a lookup works whichever form the bib uses.
    Also builds a reverse index (canon_key -> frozenset of abbr_keys) used by
    _journal_near_match to detect near-typos of known abbreviations."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "journal_abbrev.json")
    pairs = {}
    try:
        with open(path, encoding="utf-8") as fh:
            pairs = json.load(fh).get("abbreviations", {})
    except (OSError, json.JSONDecodeError):
        pairs = {}
    canon = {}
    reverse = {}   # canon_key -> set of all abbr/full keys that point to it
    for abbr, full in pairs.items():
        key = _journal_key(full)
        ak = _journal_key(abbr)
        canon[ak] = key
        canon[key] = key
        reverse.setdefault(key, set()).add(ak)
        reverse.setdefault(key, set()).add(key)
    return canon, {k: frozenset(v) for k, v in reverse.items()}


def _edit_dist_le1(a, b):
    """True if strings a and b differ by exactly one edit (insertion, deletion,
    or substitution). Used for fuzzy journal-typo detection only."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    long, short = (a, b) if la > lb else (b, a)
    for i in range(len(long)):
        if long[:i] + long[i + 1:] == short:
            return True
    return False


def _journal_near_match(bib_key, rec_canon_key, reverse_index):
    """Whether bib_key is a single-character typo of any known curated
    abbreviation key for rec_canon_key. Returns the matching curated key,
    or None. Used to enrich the journal-mismatch message ('possible typo')."""
    for known_key in reverse_index.get(rec_canon_key, ()):
        if _edit_dist_le1(bib_key, known_key):
            return known_key
    return None


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
    must be accounted for, so 'Nature' does NOT abbreviate 'Nature Physics'.

    A valid ISO-4 abbreviation never contains stopword tokens ('of', 'and', 'the',
    ...) as standalone words -- stopwords are omitted entirely in ISO-4, never kept.
    An abbreviation like 'J. of Appl. Phys.' is therefore not a valid ISO-4 form
    and must be rejected, even though 'of' appears in the full title."""
    aw = _journal_words(abbrev)
    # Reject immediately if any multi-letter abbreviation token is itself a stopword.
    # ISO-4 drops stopwords entirely; an abbreviation that retains one (e.g. 'J. of
    # Appl. Phys.', 'Quantum Sci.and Technol.') is malformed and must not be accepted
    # as equivalent to the correctly-abbreviated form. Single-letter tokens are always
    # series designators ('J. Phys. A', 'Phys. Rev. B'), never function words, and
    # are excluded from this check so a legitimate series letter is not mistaken for
    # the stopword 'a'.
    if any(len(w) > 1 and w in _ISO4_STOPWORDS for w in aw):
        return False
    # A single letter the abbreviation keeps is a SERIES designator, not an article:
    # 'J. Phys. A' / 'Phys. Rev. B' keep the A/B that names the series, so 'Journal of
    # Physics A' must NOT drop that 'a' as the article 'a'. Don't stopword-strip a
    # token the abbreviation itself carries (an article is never a standalone abbrev
    # token), so the series letter survives and the word counts line up.
    kept = _ISO4_STOPWORDS - set(aw)
    fw = [w for w in _journal_words(full) if w not in kept]
    if not aw or len(aw) != len(fw):
        return False
    for a, f in zip(aw, fw):
        if not f.startswith(a):
            return False
        # ISO-4 never abbreviates a long word to fewer than 3 characters: 'ph' for
        # 'physics' is not a legitimate truncation. Single-letter tokens are series
        # designators ('Phys. Rev. B') and are exempt from this floor.
        if len(a) == 2 and len(f) >= 5:
            return False
    return True


_JOURNAL_CANON, _JOURNAL_REVERSE = _load_journal_abbrev()


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
    # If one side is unknown to the curated table but is a single-character typo of
    # a known curated entry for the OTHER journal, do not accept it as equivalent --
    # it is a typo of a known abbreviation, not a valid alternative form. Crucially,
    # this must run BEFORE the ISO-4 heuristic so that e.g. 'Nat. Phy.' (a typo of
    # the curated 'Nat. Phys.') is not silently accepted by the prefix check.
    other_canon = cb or kb
    if ca is None and _journal_near_match(ka, other_canon, _JOURNAL_REVERSE):
        return False
    if cb is None and _journal_near_match(kb, ca or ka, _JOURNAL_REVERSE):
        return False
    if _is_iso4_abbrev(a, b) or _is_iso4_abbrev(b, a):
        return True
    # A registry full name often carries a trailing ':'-subtitle and/or a parenthetical
    # former name ('Theoretical Chemistry Accounts: Theory, Computation, and Modeling
    # (Theoretica Chimica Acta)'), while the bib uses the standard ISO-4 abbreviation
    # of the CORE title ('Theor. Chem. Acc.'). Try the abbreviation against the title
    # core -- the part before any ':' subtitle or '(' parenthetical -- so a valid
    # abbreviation of the journal's name still matches. (ISO-4's word-by-word prefix
    # match keeps this strict; a different journal won't slip through.)
    core_a, core_b = _journal_core(a), _journal_core(b)
    if core_a != a or core_b != b:
        if _is_iso4_abbrev(a, core_b) or _is_iso4_abbrev(core_b, a) \
                or _is_iso4_abbrev(b, core_a) or _is_iso4_abbrev(core_a, b) \
                or _journal_key(core_a) == _journal_key(core_b):
            return True
    # The part BEFORE a ':' subtitle is the journal's common name, so when one side
    # equals the other's pre-colon head they denote the same journal. Restricted to a
    # COLON subtitle (not any prefix), so 'Nature'/'Nature Physics' and 'ApJ'/'ApJL'
    # -- which have no colon boundary -- are NOT equated.
    head_a, head_b = _journal_key(a.split(":", 1)[0]), _journal_key(b.split(":", 1)[0])
    if ((":" in a) != (":" in b)) and head_a and head_a == head_b and (ka == head_a or kb == head_b):
        return True
    return False


def _journal_core(name):
    """A journal name with a trailing ':'-subtitle and/or a '(...)' parenthetical
    (a former name / publisher note) removed -- the core title the standard
    abbreviation is built from. 'Theoretical Chemistry Accounts: Theory, ... (Theoretica
    Chimica Acta)' -> 'Theoretical Chemistry Accounts'."""
    core = name.split(":", 1)[0]
    core = re.sub(r"\([^)]*\)", "", core)
    return re.sub(r"\s+", " ", core).strip()


# --- record comparison + parity --------------------------------------------

# Soft bibliographic fields compared between two metadata sources, as
# (key, label, normalizer). The label names the .bib FIELD ('number'), not the
# bibliographic concept it holds (a journal issue) -- a message must point at
# what the user can find and edit in their .bib, not Crossref's JSON key name for
# the same concept. Pages are dash-normalized so 'pp. 10-20' and '10--20' compare
# equal. The normalizer maps a raw value (str or int, possibly None/"") to its
# comparable string form.
def _soft(v):
    return str(v or "").strip()


_SOFT_FIELDS = [
    ("year", "year", _soft),
    ("volume", "volume", _soft),
    ("number", "number", _soft),
    ("pages", "pages", lambda v: norm_pages(str(v or ""))),
]


# Embedded markup that a registry sometimes leaks into a title (Crossref serves
# math titles as MathML; some sources include stray XML/HTML entities). A title
# carrying any of these is unsafe to adopt verbatim into a .bib.
_MARKUP_RE = re.compile(r"<\s*/?\s*[a-z][\w:-]*[^>]*>|&[a-z]+;|&#\d+;", re.I)


def _has_markup(s):
    """True if a (title) string contains embedded XML/HTML/MathML markup or a stray
    control character -- a value that must NOT be suggested as a verbatim bib edit.

    Whitespace control chars (tab/newline/carriage return) are NOT markup: a .bib
    field value legitimately wraps across lines, so a multi-line bib title carries a
    '\\n'. Counting that as "markup" would (via the rec_has_markup guard) wrongly
    conclude the BIB is the markup-bearing side and let the RECORD's real markup
    ('<scp>', '<i>') leak into a suggested edit. Only genuinely corrupt control
    bytes count."""
    if not s:
        return False
    if _MARKUP_RE.search(s):
        return True
    return any(ord(c) < 32 and c not in "\t\n\r" for c in s)


def _safe_suggestion(value):
    """A registry value cleaned for use as a verbatim `suggested` edit, or None when
    it cannot be made safe. Crossref serves names with HTML entities ('Astronomy
    &amp; Astrophysics') and sometimes markup tags; pasting '&amp;' into a .bib is a
    corrupting edit. Decode entities first, then withhold entirely if any real markup
    survives -- never push a value the user would have to hand-fix."""
    if not value:
        return None
    decoded = html.unescape(value).strip()
    if not decoded or _has_markup(decoded):
        return None
    return decoded


def _strip_markup(s):
    """Remove embedded markup TAGS/entities from a title, keeping the text content,
    so the clean (non-math) parts remain comparable: '...in <mml:math>...171...Yb
    </mml:math> Atoms' -> '...in 171Yb Atoms'. The result is good enough to COMPARE
    against the bib (so deviations in the prose parts are still caught), but it has
    lost the math FORMATTING, so it must not be offered as a verbatim suggestion.

    A tag is replaced by a SPACE when it sits between two LETTERS, because Crossref
    drops the spaces around an inline tag and removing it with nothing would MERGE the
    surrounding words ('An<i>SIRTF</i>Legacy' -> 'AnSIRTFLegacy', deflating the title
    overlap into a false mismatch); with a space it becomes 'An SIRTF Legacy', which
    matches the bib. A tag adjacent to a digit (math/isotope like '<...>171</...>Yb')
    is removed with NOTHING so '171Yb' is not split into '171 Yb'."""
    if not s:
        return s
    # Tag between two letters -> space (un-merge words); any other tag -> removed.
    s = re.sub(r"(?<=[A-Za-z])<[^>]+>(?=[A-Za-z])", " ", s)
    s = re.sub(r"<[^>]+>", "", s)            # drop the remaining tags
    s = re.sub(r"&[a-z]+;|&#\d+;", "", s, flags=re.I)  # drop entities
    s = "".join(c for c in s if ord(c) >= 32 or c == "\t")
    return re.sub(r"\s+", " ", s).strip()


# LaTeX math/symbol constructs a .bib title may carry that a registry often drops or
# mangles: inline '$...$' math, '\ensuremath{...}', and bare symbol macros ('\lambda',
# '\alpha', ...). When the bib has one of these, the record is the side more likely to
# be degraded, so a record-derived title must NOT be pushed as a verbatim suggestion.
_BIB_MATH_RE = re.compile(r"\$[^$]*\$|\\ensuremath|\\[a-zA-Z]+")


def _bib_has_math(btitle):
    """True if the bib title carries LaTeX math/symbol markup ('$...$',
    '\\ensuremath{...}', a '\\lambda'-style macro). Such a title encodes a symbol the
    registry frequently drops (Crossref served 'He i 10830', the bib has 'He I
    \\ensuremath{\\lambda}10830'); the bib is then the more complete side, so we never
    offer the record's value as the title suggestion."""
    return bool(_BIB_MATH_RE.search(btitle or ""))


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
            # Pages: a registry often stores only the START page ('3543') while the bib
            # carries the full range ('3543-3546'). The bib is then MORE complete, not
            # wrong -- suggesting it drop the range to match the record would degrade it
            # (and contradicts the dash-style note that wants the range kept). So when
            # the record's pages is exactly the start of the bib's range, it is not a
            # difference. (The reverse -- bib has only the start, record the range -- is
            # still flagged, so the common 'add the missing end page' case is kept.)
            if key == "pages" and _record_pages_is_start_of_bib(norm(recraw), norm(bibraw)):
                continue
            if key == "pages" and _pages_same_single(norm(recraw), norm(bibraw)):
                continue
            # The proposed value is handed back in its biblatex-canonical written
            # form (a page range as '920--926', not the registry's '920-926'), so an
            # applied suggestion does not itself trip the dash-style check.
            to = biblatex_pages(recraw) if key == "pages" else recraw
            out.append((key, label, bibraw, to))
    return out


def _record_pages_is_start_of_bib(rec_pages, bib_pages):
    """True when the record's page value is exactly the START page of the bib's range
    (record '3543' vs bib '3543-3546') -- the bib is the fuller, correct form, so this
    is not a mismatch. Only fires when the bib is a range and the record is a single
    page equal to its start; the opposite direction stays a real finding."""
    if "-" not in bib_pages or "-" in rec_pages:
        return False
    return bib_pages.split("-", 1)[0] == rec_pages


def _is_degenerate_range(p):
    """True when p is a 'N-N' range whose start and end are identical -- a range in
    form only, referring to a single page."""
    if "-" not in p:
        return False
    start, end = p.split("-", 1)
    return start == end


def _pages_same_single(rec_pages, bib_pages):
    """True when one side is a plain single page and the other is a degenerate
    'N-N' range on that SAME page (e.g. bib='681', rec='681-681'). Crossref
    sometimes stores a single-page article as 'NNN-NNN'; suggesting '681--681' when
    the bib already has '681' is noise, not a correction. A genuine range on either
    side (start != end) is never matched here, so a real mismatch still flags."""
    rec_degenerate = _is_degenerate_range(rec_pages)
    bib_degenerate = _is_degenerate_range(bib_pages)
    if rec_degenerate and "-" not in bib_pages:
        return rec_pages.split("-", 1)[0] == bib_pages
    if bib_degenerate and "-" not in rec_pages:
        return bib_pages.split("-", 1)[0] == rec_pages
    return False

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


def _bib_matches_earlier_version(e, rec, btitle, atitle, timeout):
    """When the arXiv record's (latest) title disagrees strongly with the bib's, ask
    whether the bib title matches an EARLIER version of the same preprint -- the
    honest case where the author cited a version arXiv has since RENAMED. Returns
    (matched_version, latest_version, latest_title) if so, else None.

    Only the arXiv source carries versioned titles, and only when we have a bare id
    and a timeout (so the call sites that pass neither cannot probe). The bib title
    must match a NON-latest version: a match against the latest would not have
    reached this strong-mismatch branch, and there is nothing to report when the
    cited title is already current."""
    arxiv_id = rec.get("arxiv_id", "")
    if timeout is None or not arxiv_id:
        return None
    from .sources import arxiv_version_titles
    versions = arxiv_version_titles(arxiv_id, timeout)
    if len(versions) < 2:
        return None
    latest = max(versions)
    bkey = title_key(btitle)
    # The cited title must match an EARLIER version (not the latest) closely. Use the
    # same tolerant fold the rest of the title layer uses; require a near-exact match
    # so a different paper that merely shares words cannot pose as "an old version".
    for v in sorted(versions):
        if v == latest:
            continue
        if title_key(versions[v]) == bkey or title_overlap(versions[v], btitle) >= 0.9:
            return (v, latest, versions[latest])
    return None


def compare_against_record(e, rec, source, rep, timeout=None):
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
    # When the resolved record reports a document type that disagrees with the bib's
    # entry type, the entry is mis-typed -- the RECORD is authoritative. Two directions:
    #   * an @article (or @inproceedings) whose record is a thesis / proceedings / book
    #     chapter / book -> point at the book/thesis/proceedings type;
    #   * a @book / @incollection / @inbook whose record is a journal article (or a
    #     proceedings article) -> point at @article / @inproceedings. This catches an
    #     SEG/conference paper mistyped '@Book' (with a 'journal=' field), which would
    #     otherwise be mis-reported as 'a book should carry an ISBN'.
    # Withdraw the offline guess first (it may have said '@online'/'@misc') and emit the
    # record-grounded suggestion. The exact same-type case (already correct) says nothing.
    doc_type = (rec.get("document_type") or "").lower()
    bib_is_articlelike = e.etype in ("article", "inproceedings", "conference")
    bib_is_booklike = e.etype in ("book", "mvbook", "collection", "incollection", "inbook")
    # A non-article object (software/dataset, via DataCite's resourceTypeGeneral). The
    # classification is the RECORD's registered type, never the title -- a paper and its
    # companion dataset can share a title, so only the type tells them apart. When the
    # record is one of these, the article-only locators (volume/issue/pages/journal)
    # are skipped below: they do not exist for software/data, so comparing them would
    # manufacture false metadata_mismatch findings.
    nonarticle = doc_type in ("software", "dataset")
    if bib_is_articlelike and nonarticle:
        # The bib says @article but the DOI resolves to software/data. Identity still
        # holds (title/author are compared below), but the author likely cited the
        # accompanying dataset/software DOI instead of the paper's -- a wrong-object
        # mistake the matching title would otherwise hide. Flag it to verify.
        rep.withdraw(e.key, "entrytype_suggestion")
        kind = "dataset" if doc_type == "dataset" else "software"
        target = "@dataset" if doc_type == "dataset" else "@software"
        rep.add(Severity.WARN, e, f"[{source}] this DOI resolves to {kind}, not a "
                f"journal article -- you may have cited the accompanying {kind}'s DOI "
                f"rather than the paper's; verify, and use {target} if the {kind} is "
                f"what you mean", "record", category="entrytype_suggestion", field="doi")
    elif bib_is_articlelike and doc_type in ("thesis", "proceedings", "book chapter", "book"):
        rep.withdraw(e.key, "entrytype_suggestion")
        target = {"thesis": "@thesis", "proceedings": "@inproceedings/@proceedings",
                  "book chapter": "@incollection", "book": "@book"}[doc_type]
        rep.add(Severity.WARN, e, f"[{source}] the record is a {doc_type}, not a "
                f"journal article -- use {target} instead of @{e.etype}", "record",
                category="entrytype_suggestion", field="journal")
    elif bib_is_booklike and doc_type in ("journal article", "proceedings"):
        rep.withdraw(e.key, "entrytype_suggestion")
        target = "@article" if doc_type == "journal article" else "@inproceedings"
        rep.add(Severity.WARN, e, f"[{source}] the record is a {doc_type}, not a book "
                f"-- use {target} instead of @{e.etype}", "record",
                category="entrytype_suggestion", field="journal")

    # Software/dataset VERSION: a DataCite record carries the release version
    # (attributes.version). When the bib's `version` field disagrees with it, the
    # entry pins the wrong release -- a render-affecting metadata difference. Compare
    # only when BOTH sides have a version (the field is optional, and absence is a
    # completeness matter, not a mismatch); fold a leading 'v' so 'v0.1.2' == '0.1.2'.
    bib_ver = clean_tex(e.get("version", "")).strip()
    rec_ver = (rec.get("software_version") or "").strip()
    if nonarticle and bib_ver and rec_ver and \
            bib_ver.lstrip("vV") != rec_ver.lstrip("vV"):
        rep.add(Severity.WARN, e, f"[{source}] version differs", "record",
                category="metadata_mismatch", field="version",
                suggested={"field": "version", "from": bib_ver, "to": rec_ver})

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
    # A suggested 'to' is the record title only when it is safe to paste verbatim:
    # withheld when the record carried markup (stripped form lost formatting) OR when
    # the BIB title carries LaTeX math the record likely dropped (e.g. the bib's
    # '\ensuremath{\lambda}10830' vs Crossref's de-mathed '10830') -- conforming the
    # bib toward the record there would DELETE the symbol, a corrupting edit. In both
    # cases the finding stays (the difference is real) but without an auto-apply 'to'.
    bib_has_math = _bib_has_math(btitle)
    if rec_has_markup or bib_has_math:
        safe_to = None
    else:
        from .rules import add_title_brace_protection
        safe_to = add_title_brace_protection(atitle)
    mangle_note = " (record title contains markup; verify the exact form manually)" \
        if rec_has_markup else (" (bib title has math the record may have dropped; "
        "verify manually)" if bib_has_math else "")

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
        #
        # BUT do not push the bib toward an ALL-CAPS record: many journals (older
        # ApJ/IOP) store titles shouted in uppercase as their house style, which is
        # NOT the canonical .bib form -- conforming a nicely Title-Cased bib to it
        # would be a regression. When the record is itself miscased, withhold the
        # suggested edit (the note stays, since the punctuation does differ).
        rec_is_shouted = title_is_miscased(atitle)
        style_sug = None if rec_is_shouted else _title_sug()
        caps_note = " (record is ALL-CAPS house style; keep your title's casing)" \
            if rec_is_shouted else ""
        rep.add(Severity.INFO, e, f"[{source}] title matches the record but its "
                f"punctuation/wording differs from the canonical form{mangle_note}{caps_note}",
                "record", category="title_style", field="title", suggested=style_sug)
    title_style_emitted = False
    if bt and at and bt != at and bt.replace(" ", "") == at.replace(" ", ""):
        # The titles differ ONLY in where a space falls inside a token -- a catalog
        # designation written closed-up vs spaced ('NGC6334I' vs 'NGC 6334I'), or a
        # similar spacing slip. The word-overlap metric scores this as a strong
        # mismatch (one token split in two drops it well below 60%), but it is the same
        # title -- a spacing nudge, not a content difference. Emit it as a style NOTE
        # toward the record's spacing, never an overlap WARN. (safe_to already withheld
        # when the record carried markup.) Track that it fired so the elif branch (which
        # may also find a content mismatch on the same title) can suppress it if a
        # metadata_mismatch is about to state the same fact more usefully.
        rep.add(Severity.INFO, e, f"[{source}] title matches the record but its spacing "
                f"differs from the canonical form{mangle_note}", "record",
                category="title_style", field="title", suggested=_title_sug())
        title_style_emitted = True
    if bt and at and bt != at and not title_style_emitted:
        if title_is_shortened(btitle, atitle):
            # One title is a clean prefix of the other (a dropped subtitle), not a
            # different paper. Direction matters: only when the BIB is the shorter
            # side did the bib drop the subtitle -- an actionable note to add it. When
            # the bib is the LONGER side, the bib already has the full title and the
            # *record* is the truncated one (Crossref often clips A&A subtitles), so
            # there is nothing to fix -- stay silent rather than wrongly tell the user
            # their complete title "dropped a subtitle".
            if len(bt.split()) < len(at.split()):
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
                # Before calling this a mismatch, check the honest case: the bib may
                # faithfully cite an EARLIER arXiv version whose title arXiv later
                # changed. If the cited title matches a non-latest version, the bib is
                # NOT wrong -- suppress the mismatch, withhold the title overwrite (it
                # would push the new title over a correct one -- 'never push a bad
                # value'), and emit an informational 'renamed in a later version' note.
                # That note is itself superseded by preprint_superseded when a
                # published version exists (one fix -- cite the published DOI -- covers
                # both); record.py records that supersession.
                retitle = _bib_matches_earlier_version(e, rec, btitle, atitle, timeout)
                if retitle:
                    matched_v, latest_v, latest_title = retitle
                    rep.add(Severity.INFO, e, f"[{source}] the arXiv preprint was renamed "
                            f"in a later version: the cited title matches v{matched_v}, but "
                            f"the latest (v{latest_v}) is \"{latest_title[:80]}\" -- update the "
                            f"cited title if you mean the current version", "record",
                            category="preprint_retitled", field="title")
                else:
                    title_differs_strongly = True
                    rep.add(Severity.WARN, e, f"[{source}] title differs from record (overlap {overlap:.0%}){mangle_note}:\n"
                            f"        bib:    {btitle[:90]}\n"
                            f"        {source}: {atitle[:90]}", "record",
                            category="metadata_mismatch", field="title", suggested=_title_sug())
            else:
                # Even at high overlap, an arXiv preprint may have been retitled
                # between versions (e.g. v1 "computer" vs v2 "simulator" at 83%).
                # Check before emitting "differs slightly" — the bib may be correct.
                if source == "arxiv":
                    retitle = _bib_matches_earlier_version(e, rec, btitle, atitle, timeout)
                    if retitle:
                        matched_v, latest_v, latest_title = retitle
                        rep.add(Severity.INFO, e, f"[{source}] the arXiv preprint was renamed "
                                f"in a later version: the cited title matches v{matched_v}, but "
                                f"the latest (v{latest_v}) is \"{latest_title[:80]}\" -- update the "
                                f"cited title if you mean the current version", "record",
                                category="preprint_retitled", field="title")
                        retitle = True  # sentinel: skip the "differs slightly" below
                    else:
                        retitle = False
                else:
                    retitle = False
                if not retitle:
                    rep.add(Severity.INFO, e, f"[{source}] title differs slightly (overlap {overlap:.0%}){mangle_note}:\n"
                            f"        bib:    {btitle[:90]}\n"
                            f"        {source}: {atitle[:90]}",
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
        # A software/dataset record has no volume/issue/pages -- only `year` is a real,
        # comparable field. Skip the article-only locators so a record that legitimately
        # lacks them never produces a false 'volume differs' / 'pages differ' finding.
        if nonarticle and fld != "year":
            continue
        # arXiv preprints are VERSIONED: v1 and a later vN can carry different years
        # (<published> vs <updated>). When the bib's year matches SOME version in the
        # record's version span [year, updated_year] -- just not the one the record
        # reports -- the bib is not wrong, only under-specified. Best practice is to
        # cite the version explicitly (arXiv:ID vN) so the year is unambiguous. So
        # emit a version-pinning NOTE, not a corrective 'year 2024 -> 2023' warning
        # whose direction is the author's choice, not a fact.
        if fld == "year" and _bib_year_matches_a_version(bibval, rec):
            # The bib year matches a REAL arXiv version -- it is not wrong, so this is
            # never a corrective 'year differs' warning. We also do NOT prescribe a
            # year: v1 (the first-submission year) and the latest-version year are both
            # legitimate and mean DIFFERENT things -- the v1 year establishes
            # precedence/priority ("first shown"), the latest year reflects the current
            # revised content. Which is right is the author's editorial call, so the
            # note only states the span and leaves the choice to them (no "pin the
            # version" directive -- arXiv's own bib generator pins none).
            v1, vn = rec.get("year"), rec.get("updated_year")
            rep.add(Severity.INFO, e, f"[{source}] bib year {bibval} matches an arXiv "
                    f"version (v1 {v1}, latest {vn}); both are valid -- v1's year marks "
                    f"first-submission precedence, the latest year the revised content. "
                    f"Use whichever fits why you cite it.",
                    "record", category="preprint_version", field="year")
            continue
        # An entry cited AS an arXiv preprint whose year matches NO arXiv version (not
        # v1, not the latest) is a data error -- not an editorial version choice but a
        # year that does not correspond to the cited object at all (a typo, or the
        # published year stamped on a preprint citation). Flag it as such (WARN),
        # naming the real versions; no suggested year (we cannot know which is intended
        # -- never push a guessed value).
        if fld == "year" and source == "arxiv" and is_preprint(e):
            v1, vn = rec.get("year"), rec.get("updated_year")
            versions = f"v1 {v1}" + (f", latest {vn}" if vn and vn != v1 else "")
            rep.add(Severity.WARN, e, f"[{source}] bib year {bibval} matches no arXiv "
                    f"version of this preprint ({versions}) -- likely a wrong/typo year "
                    f"for the cited preprint", "record", category="metadata_mismatch",
                    field="year")
            continue
        # The before -> after lives in the suggested tail, so the prose stays terse
        # ('year differs') rather than repeating 'bib=X vs crossref=Y'.
        rep.add(Severity.WARN, e, f"[{source}] {label} differs",
                "record", category="metadata_mismatch",
                field=fld, suggested={"field": fld, "from": bibval, "to": recval})

    # The offline misplaced_field rule flags a value in 'number'/'issue' that looks
    # like a date (a month name, date string, or year) as a misplaced value. Once
    # the entry resolves, decide whether the misplaced_field finding is still useful:
    #
    #   * Record corroborates bib's value (bib_number == rec_issue): the offline guess
    #     was wrong -- withdraw it.
    #   * Record disagrees AND bib value is NOT year-shaped: the metadata_mismatch
    #     finding above already gives the correct number with a suggested fix, making
    #     "move to month field" redundant and contradictory -- withdraw.
    #   * Record disagrees AND bib value IS year-shaped (e.g. number={2018}): the
    #     misplaced_field finding still carries diagnostic value (it says the year is
    #     wrong field), and the metadata_mismatch gives the real issue number -- both
    #     are independently useful, so keep both.
    import re as _re
    bib_number, rec_issue = _soft(e.get("number")), _soft(rec.get("number"))
    numeric_journal = _soft(e.get("journal")).strip("{}").strip().isdigit()
    _year_shaped = bool(_re.fullmatch(r"(?:19|20)\d{2}", bib_number.strip()))
    if bib_number and rec_issue and not numeric_journal:
        if bib_number == rec_issue or not _year_shaped:
            rep.withdraw(e.key, "misplaced_field")

    # The journal comparison is article-only. For a software/dataset record the
    # record's "journal" is the repository name ("Zenodo") and the bib entry has no
    # journal at all -- comparing them would be meaningless, so skip the whole block.
    raw_journal = e.get("journal", "")
    bj, aj = clean_tex(raw_journal).lower(), clean_tex(rec.get("journal", "")).lower()
    rec_journal_safe = _safe_suggestion(rec.get("journal", ""))
    if nonarticle:
        pass
    elif bj and aj and "arxiv" not in bj and "arxiv" not in aj and not _journal_equiv(bj, aj):
        # Journal renders in the citation -> WARN, with the record's name suggested
        # (only when it is safe to paste verbatim -- an entity-laden name is withheld).
        # If the bib name is a single-character typo of a known curated abbreviation for
        # this journal, say so explicitly ("possible typo") to make the finding actionable.
        rec_canon_key = _JOURNAL_CANON.get(_journal_key(aj))
        _typo = rec_canon_key and _journal_near_match(
            _journal_key(bj), rec_canon_key, _JOURNAL_REVERSE)
        _jmsg = (f"[{source}] journal differs (possible typo of a known abbreviation)"
                 if _typo else f"[{source}] journal differs")
        rep.add(Severity.WARN, e, _jmsg, "record",
                category="metadata_mismatch", field="journal",
                suggested={"field": "journal", "from": raw_journal,
                           "to": rec_journal_safe} if rec_journal_safe else None)
    elif not bj and raw_journal.replace("{", "").replace("}", "").strip() and aj:
        # The journal field has content but de-TeXes to nothing: it is an unexpanded
        # macro (e.g. the AASTeX 'journal={\pra}', or any publisher's shorthand).
        # It is a real venue -- NOT missing -- but it only renders where that macro is
        # defined, so it is not portable and cannot be checked against the record. Now
        # that the entry resolved, the record gives us the canonical name to offer as a
        # grounded, ready-to-apply replacement (no guessing -- straight from Crossref,
        # entity-decoded and withheld if it still carries markup).
        rep.add(Severity.INFO, e, f"[{source}] journal is an unexpanded macro "
                f"('{raw_journal.strip()}'); it only renders where that macro is "
                f"defined -- use the journal's name for a portable record", "record",
                category="journal_macro", field="journal",
                suggested={"field": "journal", "from": raw_journal,
                           "to": rec_journal_safe} if rec_journal_safe else None)

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
            # Data conflicts: compare source records against each other. When the
            # bib agrees with at least one source, it is correct and nothing is
            # actionable -- suppress the finding entirely (the sources' internal
            # disagreement is not the user's problem). When neither source agrees
            # with the bib, the user should check, but since the sources disagree
            # we cannot confidently suggest a value, so we warn without one.
            bib_agrees, bib_conflicts = [], []
            for key, label, norm in _SOFT_FIELDS:
                if key in skip:
                    continue
                lv, rv = norm(ra.get(key)), norm(rb.get(key))
                if not (lv and rv and lv != rv):
                    continue
                if key == "pages" and (lv in rv or rv in lv):
                    continue
                bv = norm(e.get(key, "") or "")
                if bv and (bv == lv or bv == rv):
                    bib_agrees.append(f"{label} ({sa}={lv} vs {sb}={rv}; bib agrees with {sa if bv == lv else sb})")
                else:
                    bib_conflicts.append(f"{label} ({sa}={lv} vs {sb}={rv})")
            if bib_conflicts:
                rep.add(Severity.WARN, e, "sources disagree and bib matches neither: "
                        + "; ".join(bib_conflicts) + " -- verify against the record",
                        "record", category="source_conflict")
            # Journals: a full title vs its abbreviation (or any two forms
            # _journal_equiv accepts) is NOT a discrepancy -- both are valid, so it
            # is not flagged at all. Only journals that are genuinely different
            # (not equivalent) are a real cross-source conflict.
            # INSPIRE uses compressed house-format abbreviations (e.g.
            # 'IEEE J.Quant.Electron.' for 'IEEE Journal of Selected Topics in Quantum
            # Electronics') that are not strict ISO-4 word-by-word prefixes and will
            # fail _journal_equiv even though they denote the same journal. When one
            # source is INSPIRE and _journal_equiv says they're different, we give it
            # the benefit of the doubt: suppress the conflict unless the journals are
            # clearly unrelated (neither is a prefix/abbreviation of the other at all).
            # Two genuinely different journals between INSPIRE and Crossref still fire.
            ja, jb = ra.get("journal", ""), rb.get("journal", "")
            # An ISBN-resolved record for a chapter-in-a-volume describes the
            # CONTAINER book, not the chapter -- its 'journal' slot holds the book's
            # publisher/series, not a comparable venue name. Crossref (or another
            # source) instead resolves the cited CHAPTER and names the book as its
            # container title. The two are different granularities of the same work,
            # not a genuine cross-source conflict -- the container_granularity note
            # already points at the real issue (check the entry type), so do not
            # also raise a 'sources disagree on the journal' warning here.
            isbn_pair = "isbn" in (sa, sb)
            if isbn_pair and is_container_granularity(e):
                continue
            if ja and jb and "arxiv" not in ja.lower() and "arxiv" not in jb.lower() \
                    and not _journal_equiv(ja, jb):
                inspire_pair = "inspire" in (sa, sb)
                # For an INSPIRE pair, suppress only when the shorter name looks like
                # a plausible abbreviation of the longer (first word matches). Two
                # genuinely different journals (Nature Physics vs Phys Rev B) will not
                # share a first word and still fire.
                if inspire_pair:
                    shorter, longer = (ja, jb) if len(ja) <= len(jb) else (jb, ja)
                    first_short = _journal_words(shorter)[:1]
                    first_long = _journal_words(longer)[:1]
                    suppress = bool(first_short and first_long
                                    and first_long[0].startswith(first_short[0]))
                else:
                    suppress = False
                if not suppress:
                    rep.add(Severity.WARN, e, f"sources disagree on the journal: "
                            f"{sa}={ja!r} vs {sb}={jb!r}", "record",
                            category="source_conflict")


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
                # Special case: 'number' is absent but 'issue' carries the same value.
                # biblatex's 'issue' field holds an issue *label/title* (e.g. "Special
                # Issue on X"), not the numeric issue number -- that belongs in 'number'.
                # When the bib uses 'issue' for the issue number and it matches what the
                # record reports, suggest renaming the field rather than adding a new one.
                if fld == "number":
                    bib_issue = e.get("issue", "").strip()
                    if bib_issue and _soft(bib_issue) == _soft(str(rval)):
                        rep.add(Severity.INFO, e,
                                f"[{source}] issue number {rval!r} is in the 'issue' field; "
                                f"biblatex uses 'number' for the issue number -- rename the field",
                                "record", category="parity_suggestion",
                                field="number",
                                suggested={"field": "number", "from": "issue", "to": "number"})
                        continue
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
