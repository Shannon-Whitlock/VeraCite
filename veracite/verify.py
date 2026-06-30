"""Verification roll-up: per-reference status/confidence (Layer 3), persistent-id
coverage and year-gated DOI awareness (Layer 5), and the bibliography integrity
score (Layer 6).

This layer interprets the evidence already gathered by record.py -- it does not
fetch records itself (except the optional Crossref title search for the year-gated
DOI check). Status and confidence are deterministic functions of which sources
resolved and whether they agreed, so they are explainable, not model outputs.
"""

import re

from .config import endpoint, SETTINGS
from .normalize import (clean_tex, is_article_like, is_book, is_collaboration,
                        split_authors)
from .report import Severity
from .titles import title_is_fragment, title_overlap, title_similar, title_words

# The three outcomes. VERIFIED means the identity resolved and the first author and
# title match -- the right paper. Its CONFIDENCE (0-1) carries the nuance that used
# to be a separate 'LIKELY_VERIFIED' status: 1.0 = clean multi-source match, ~0.95 =
# clean single source, 0.75 = right paper but a field disagrees, 0.70 = sources
# disagree or only arXiv (advisory) confirms. UNVERIFIED means we could not confirm
# the reference (no identifier, no record returned, or a DOI that did not resolve).
# MISMATCH means it DID resolve but the record's identity disagrees, so the id may
# point at a different paper. The status line is itself a finding.
STATUS_PASS = {"VERIFIED"}

# Identity-level finding categories: their presence means the resolved record may
# be a different paper, so the reference is a MISMATCH.
_IDENTITY_CATS = {"id_resolves_wrong_record"}


def _entry_findings(rep, key):
    return [f for f in rep.live_findings() if f.key == key]


def classify(e, res, rep):
    """Assign a verification status and confidence (0-1) to one entry from its
    resolution evidence, stash it on the report for the header, and return
    (status, confidence). The status is shown in the entry header, not as its own
    finding line; the cause/detail is carried alongside for the header to print."""
    fcats = {f.category for f in _entry_findings(rep, e.key)}
    n_sources = len(res.sources)

    if res.dead_doi and res.record is None:
        # A DOI that does not resolve AND nothing recovered it -- still unverified.
        # The dead_doi ERROR finding (record.py) carries the severity; here it just
        # sets the status. (When the title search DID recover a matching record, we
        # fall through to the resolved path below: the work is verified via the
        # corrected DOI, just at reduced confidence -- the recorded DOI is the defect,
        # flagged by dead_doi, not the paper's identity.)
        status, conf = "UNVERIFIED", 0.0
        detail = "the recorded DOI does not resolve"
    elif res.record is None and (res.doi or res.arxiv_id or res.isbn):
        # An id was present but no source returned a record -- failed to verify.
        status, conf = "UNVERIFIED", 0.1
        detail = "no authoritative source returned a record for its identifier"
    elif res.record is None:
        # No identifier at all and none found by search -- simply unverifiable
        # (not fabricated).
        status, conf = "UNVERIFIED", 0.2
        detail = "no persistent identifier to verify against"
    elif _IDENTITY_CATS & fcats:
        # Resolved, but the record's first author AND title both differ -- the id
        # may point at a different paper.
        status, conf = "MISMATCH", 0.3
        detail = "resolved record's identity disagrees (the id may be wrong)"
    else:
        # Resolved and identity consistent -> VERIFIED. Confidence (not a separate
        # status) carries the nuance: a soft metadata discrepancy, a cross-source
        # conflict, or only arXiv (advisory author data) confirming lowers it below
        # a clean multi-source match.
        status = "VERIFIED"
        soft = "metadata_mismatch" in fcats
        conflict = "source_conflict" in fcats
        arxiv_only = set(res.sources) <= {"arxiv"}
        if res.dead_doi:
            # Verified, but only after the recorded DOI 404'd and a title search
            # recovered the real one. The paper is confirmed; the identifier as
            # written is broken (the dead_doi ERROR + the doi_available fix say how),
            # so confidence is capped low to reflect "right paper, wrong DOI on file".
            conf, detail = 0.6, "verified via a corrected DOI; the recorded one is dead"
        elif soft:
            conf, detail = 0.75, "right paper, but a field differs from the record"
        elif conflict:
            conf, detail = 0.70, "right paper, but authoritative sources disagree on a field"
        elif arxiv_only:
            conf, detail = 0.70, "confirmed by arXiv only (author-submitted, advisory)"
        elif getattr(res, "found_by_search", False):
            # Recovered by a title+author SEARCH -- the bib carried no usable
            # identifier, so we found a candidate and confirmed it is self-consistent
            # (title+author+year agree). That is a weaker basis than an entry that
            # arrived with its own id and resolved cleanly: the corroboration partly
            # echoes the query, and the missing PID is itself the defect (flagged by
            # doi_available). So it verifies, but capped below a clean id-resolved
            # match -- never the 0.95/1.0 reserved for an entry whose own identifier
            # checked out.
            conf = 0.85
            detail = "recovered by title/author search (no identifier in the entry)"
        else:
            # Clean match: 1.00 with 2+ agreeing authoritative sources, ~0.95 with
            # a single source (consistent, but uncorroborated).
            conf = 1.0 if n_sources >= 2 else 0.95
            srcs = ", ".join(sorted(res.sources)) or res.source
            detail = f"resolved and consistent ({srcs})"

    # The status no longer prints as its own finding line -- it lives in the entry
    # HEADER (key @type line N  STATUS (conf) -- detail), which is the single
    # identifying line per record. A clean VERIFIED (1.0) shows just 'VERIFIED'; a
    # caveat shows the confidence and the short reason; UNVERIFIED/MISMATCH show the
    # cause there too. The per-field findings (metadata_mismatch, source_conflict)
    # remain the detailed explanation beneath the header, so the old status line was
    # pure restatement. `detail` is stashed for the header; severity rides on the
    # findings that actually explain the issue, not on the status itself.
    rep.set_status(e.key, status, conf, detail)
    return status, conf


# --- Layer 5: persistent identifier coverage + year-gated DOI awareness ----

def _entry_year(e, res):
    y = e.get("year", "").strip()
    if y[:4].isdigit():
        return int(y[:4])
    return res.record.get("year") if res.record else None


def pid_check(e, res, rep, delay, timeout, offline):
    """Persistent-identifier coverage for one entry (Layer 5). Flags a modern
    article that omits a DOI which actually exists, never penalizes pre-2005
    works, and accepts arXiv/ISBN as sufficient for preprints/books. Returns the
    name of the strongest present identifier ('doi'/'arxiv'/'isbn') or ''."""
    has_doi = bool(res.doi)
    # A recorded DOI that did NOT resolve (Crossref 404) is no better than a missing
    # one for verification: the entry is still UNVERIFIED. So treat a dead DOI as "no
    # usable DOI" here and let the title search below try to recover the real one --
    # the same fallback an entry with no DOI field gets. (The dead_doi error is still
    # reported by the record layer; this only adds a 'found the real DOI' suggestion.)
    usable_doi = has_doi and not res.dead_doi
    strongest = "doi" if usable_doi else "arxiv" if res.arxiv_id else "isbn" if res.isbn else ""

    # A book-TYPED entry that carries a 'journal' field is not really a whole book (a
    # book is not published in a journal) -- it is a journal article mis-typed @book,
    # very common for SEG/IEEE entries. Don't treat it as a book here: that would emit
    # a misdirected 'a book should carry an ISBN' AND skip the DOI search below. Let it
    # fall through to the article-like path so its real DOI is recovered; the record
    # layer separately suggests the correct entry type (entrytype_suggestion).
    book_typed = is_book(e) and not e.get("journal", "").strip()
    if book_typed:
        if not (res.isbn or has_doi):
            res.pid_missing = True
            rep.add(Severity.WARN, e, "no ISBN or DOI -- a book should carry a "
                    "persistent identifier", category="pid_missing")
        return strongest

    # Article-like, OR a book-typed entry with a journal field (handled here as the
    # mis-typed article it is, so its DOI is searched/recovered like any article).
    if is_article_like(e) or is_book(e):
        if usable_doi:
            # The DOI resolved, but if it was MINED FROM THE URL (no 'doi' field),
            # nudge the author to record it as a proper field -- the url path is not
            # where a tool or style expects the identifier.
            if res.doi_from_url:
                rep.add(Severity.WARN, e, f"the DOI {res.doi_from_url} is in the url "
                        "but not recorded as a 'doi' field; add it so the identifier "
                        "is machine-readable", category="doi_available", field="doi",
                        suggested={"field": "doi", "to": res.doi_from_url})
                # This is the SAME fact as the offline identifier_placement nudge, but
                # richer (the DOI is confirmed resolved). Withdraw the nudge so the
                # entry shows one finding, not two, for the url DOI.
                rep.withdraw(e.key, "identifier_placement")
            return strongest
        if res.arxiv_id:
            # arXiv-only: the arXiv id is a sufficient PID. (A linked published
            # version is reported separately by the preprint check.)
            return "arxiv"
        year = _entry_year(e, res)
        modern = year is None or year >= 2005
        # Always try to find a DOI (any year) so the entry can be verified even
        # though the bib omitted it. A strong match resolves the record.
        if not offline:
            found = _search_doi(e, timeout)
            if found:
                from .record import resolve_by_found_doi
                # Capture the recorded (dead) DOI BEFORE resolve_by_found_doi overwrites
                # res.doi with the one just found, so the suggested edit's `from` is the
                # value being replaced, not the replacement.
                old_doi = res.doi
                resolved = resolve_by_found_doi(e, found, res, rep, 0, timeout)
                state = "verified" if resolved else "unverified"
                if res.dead_doi:
                    # The entry HAS a DOI, but it 404'd (the dead_doi error above). So
                    # this is a REPLACEMENT, not an addition -- word it as the fix for
                    # that error and carry the old->new edit so a tool can apply it.
                    msg = (f"the correct DOI appears to be {found} ({state}) -- "
                           f"replace the dead one above with it")
                    suggested = {"field": "doi", "from": old_doi, "to": found}
                else:
                    # No DOI was recorded; this is something to ADD.
                    msg = (f"no DOI in the entry; the correct one appears to be "
                           f"{found} ({state}) -- add it")
                    suggested = {"field": "doi", "to": found}
                rep.add(Severity.WARN, e, msg, category="doi_available", field="doi",
                        suggested=suggested)
                return "doi" if resolved else strongest
            # No DOI -- many works (esp. ML/physics) live on arXiv with no DOI. Try
            # to resolve the entry by an arXiv TITLE search so it can still be
            # verified and a citable identifier suggested. If arXiv links a PUBLISHED
            # version (its <arxiv:doi>), that DOI is preferred and suggested instead
            # of the bare preprint id.
            arxiv_id = _search_arxiv_id(e, timeout)
            if arxiv_id:
                from .record import resolve_by_found_arxiv
                kind, ident = resolve_by_found_arxiv(e, arxiv_id, res, rep, timeout)
                if kind == "doi":
                    rep.add(Severity.WARN, e, f"no identifier in the entry, but the "
                            f"work is on arXiv ({arxiv_id}) with a published DOI "
                            f"{ident}; record the DOI (add it)", category="doi_available",
                            field="doi")
                    return "doi"
                if kind == "arxiv":
                    rep.add(Severity.WARN, e, f"no DOI, but the work is on arXiv "
                            f"({ident}); record its eprint/arXiv id to make it "
                            f"verifiable", category="doi_available", field="eprint")
                    return "arxiv"
        # No DOI found by search. A dead-DOI entry already carries the dead_doi
        # error and DOES have a DOI recorded (just unresolvable), so don't also tell
        # it "no DOI recorded" -- that would be a false, duplicate finding. The
        # pid_missing warning is only for an entry with no DOI field at all.
        if has_doi:
            return strongest
        # Only a MODERN article is expected to have one -- a pre-2005 work is not
        # penalized for lacking a DOI, and we say nothing about it: a "DOI not
        # required, none found" note suggests no action, so it is noise, not a
        # finding. Every emitted message must point at something to fix.
        if modern:
            res.pid_missing = True
            rep.add(Severity.WARN, e, "no DOI recorded for a post-2005 article "
                    "(none found in Crossref either)", category="pid_missing")
    return strongest


def _search_doi(e, timeout):
    """Crossref bibliographic search for an entry that omits a DOI; return a DOI
    only when a hit matches on title AND first author AND (journal OR year). The
    matching is hardened against stylistic variation so a correct DOI is not
    missed: titles compare after de-TeX/deaccent/math-strip and tolerate a dropped
    subtitle or one stray word; surnames compare particle-aware; the year tolerates
    a +-1 online-first/issue offset. The identity requirements are NOT loosened --
    the same paper is often reprinted as a technical report (a DTIC '10.21236/...'
    DOI) with an identical title and author, so we still require a matching work
    type AND journal-or-year corroboration before recommending a DOI."""
    from .compare import _journal_equiv, _surname_match  # avoid cycle
    from .http import http_get_json
    from .titles import title_key
    title = clean_tex(e.get("title", "")).strip()
    nwords = len(title.split())
    # A 1-word title is too generic to search on at all. A 2-word title ('Cavity
    # optomechanics') is allowed ONLY under maximal corroboration -- exact title AND
    # first author AND journal AND year must all agree (the `very_short` gate below) --
    # so it cannot ride a generic collision into a wrong hit, yet a famous short-title
    # paper is still recovered. A 3-word title needs an exact title match (no fuzzy
    # overlap); a longer title may match fuzzily.
    if nwords < 2:
        return ""
    very_short = nwords < 3      # 2-word title: requires journal AND year AND exact
    short_title = nwords < 4     # 3-word title: requires exact title (no fuzzy)
    query = f"{title} {clean_tex(e.get('author', ''))}".strip()
    data, code = http_get_json(endpoint("crossref_search", query=query), timeout)
    if code != 200 or not data:
        return ""
    bib_first = (split_authors(e.get("author", "")) or [""])[0]
    collab = is_collaboration(e.get("author", ""))
    bib_journal = clean_tex(e.get("journal", "")).strip()
    bib_year = e.get("year", "").strip()[:4]
    want_journal = e.etype in ("article",)
    for item in (data.get("message", {}).get("items") or [])[:8]:
        cand_title = (item.get("title") or [""])[0]
        # An EXACT normalized title match (not just the tolerant overlap) is a much
        # stronger identity signal than a fuzzy one -- it carries its own
        # corroboration and lets a published BOOK CHAPTER of a work mistyped as
        # @article resolve (e.g. a Seminaire Poincare review). A short title ALWAYS
        # requires exact; a long title may match fuzzily but only the exact case gets
        # the relaxed type/corroboration treatment below.
        exact_title = title_key(title) == title_key(cand_title)
        fragment_title = not exact_title and title_is_fragment(title, cand_title)
        # A long title (>=8 unique words) near 0.80 Jaccard overlap is a near-
        # match: extra/missing words vs a core title that is otherwise the same
        # paper (e.g. the bib adds 'Precision' and 'terahertz' to a title whose
        # Crossref form omits them). Accepted only with full corroboration.
        overlap = title_overlap(title, cand_title) if not exact_title else 1.0
        near_match = (not exact_title and not fragment_title
                      and overlap >= 0.80
                      and len(title_words(title) | title_words(cand_title)) >= 8)
        if short_title:
            if not exact_title:
                continue
        elif not (exact_title or title_similar(title, cand_title)
                  or fragment_title or near_match):
            continue
        # First-author surname must match (particle-aware). Skipped for a
        # collaboration author, which has no surname to compare.
        fams = [a.get("family") or a.get("name") or "" for a in (item.get("author") or [])]
        from .normalize import fold_surname
        author_ok = collab or not (bib_first and fams) \
            or _surname_match(bib_first, fold_surname(fams[0]))
        if not author_ok:
            continue
        # Type class must agree: a journal article should match a journal-article,
        # not a posted-content/report/dataset reprint of the same title. A
        # book-chapter is allowed ONLY for an exact-title match -- it is the
        # published-book version of a work the bib mistyped as @article, not a
        # same-title reprint (the entry-type rule separately suggests @incollection).
        ctype = item.get("type", "")
        allowed = ("journal-article", "proceedings-article", "")
        if exact_title:
            allowed = allowed + ("book-chapter",)
        if want_journal and ctype not in allowed:
            continue
        cand_journal = (item.get("container-title") or [""])
        cand_journal = cand_journal[0] if cand_journal else ""
        cand_year = ""
        parts = item.get("issued", {}).get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            cand_year = str(parts[0][0])
        journal_ok = bool(bib_journal) and bool(cand_journal) \
            and _journal_equiv(bib_journal, cand_journal)
        # Year corroboration tolerates +-1 (online-first vs print/issue year).
        year_ok = bool(bib_year) and bool(cand_year) and cand_year.isdigit() \
            and bib_year.isdigit() and abs(int(bib_year) - int(cand_year)) <= 1
        # An EXACT title match (+ first author) is its own corroboration when the
        # bib gives nothing else to check against -- it lets a work the bib left
        # journal-less and year-shifted resolve (a preprint-year @article that is
        # really a later book chapter, e.g. Browaeys 'Interacting Cold Rydberg
        # Atoms'). But it must NOT override a CONTRADICTION: if both sides carry a
        # year and they disagree by more than the book/preprint gap, two different
        # same-title works by the same author are possible, so exact-title alone is
        # not enough -- fall back to requiring real journal/year corroboration.
        years_known = bool(bib_year) and bool(cand_year) and cand_year.isdigit() \
            and bib_year.isdigit()
        year_conflict = years_known and abs(int(bib_year) - int(cand_year)) > 3
        exact_ok = exact_title and not year_conflict
        # Require corroboration beyond title+author: journal, year (+-1), or an
        # exact-title match with no year contradiction. Recovered entries stay capped
        # at 0.85 (found_by_search), so the caution rides in the confidence.
        #
        # A VERY SHORT (2-word) title is the exception: title+author alone is too weak
        # for so generic a title, so demand FULL corroboration -- exact title (already
        # enforced above), first author (already enforced), AND journal AND year. All
        # four agreeing makes a wrong hit effectively impossible, while a famous
        # short-title work ('Cavity optomechanics', Rev. Mod. Phys. 2014) resolves.
        if very_short:
            if exact_title and journal_ok and year_ok:
                return item.get("DOI", "")
            continue
        # A fragment or near-match (0.80 <= overlap < 0.90 on a long title) is
        # accepted only when BOTH journal and year corroborate -- a partial title
        # match alone is too weak, since plausible phrases can appear in many titles.
        if (fragment_title or near_match) and not (journal_ok and year_ok):
            continue
        if journal_ok or year_ok or exact_ok:
            return item.get("DOI", "")
    return ""


def search_published_version(e, timeout):
    """For an entry cited as an arXiv PREPRINT, ask Crossref whether a PUBLISHED
    journal version now exists -- the case arXiv has not yet back-linked in its
    <arxiv:doi>. Returns (doi, journal, year, title) of a strong journal-article
    match, or ('', '', '', ''). The title lets the caller show the published title
    (so the human can confirm the match) -- the same affordance the arXiv-linked
    path already has.

    The identity gate is title + first-author surname (the same hardened comparison
    _search_doi uses), but the corroboration differs from the no-DOI search: here
    the bib's 'journal' is 'arXiv' and its year is the PREPRINT year, so neither
    helps confirm the PUBLISHED record. Instead we require the hit to be a
    'journal-article' (NOT 'posted-content', which is the preprint itself) -- a
    published, citable version of the same title+author is exactly what we are
    looking for. A near-future year is fine (a preprint published the next year)."""
    from .compare import _surname_match  # avoid cycle
    from .http import http_get_json
    from .normalize import fold_surname
    title = clean_tex(e.get("title", "")).strip()
    if len(title.split()) < 4:
        return "", "", "", ""
    query = f"{title} {clean_tex(e.get('author', ''))}".strip()
    data, code = http_get_json(endpoint("crossref_search", query=query), timeout)
    if code != 200 or not data:
        return "", "", "", ""
    bib_first = (split_authors(e.get("author", "")) or [""])[0]
    collab = is_collaboration(e.get("author", ""))
    for item in (data.get("message", {}).get("items") or [])[:8]:
        # Must be a real published article, not the arXiv preprint reposted as
        # posted-content, and not a report/dataset reprint.
        if item.get("type") != "journal-article":
            continue
        if not title_similar(title, (item.get("title") or [""])[0]):
            continue
        fams = [a.get("family") or a.get("name") or "" for a in (item.get("author") or [])]
        if not collab and bib_first and fams \
                and not _surname_match(bib_first, fold_surname(fams[0])):
            continue
        doi = item.get("DOI", "")
        journal = (item.get("container-title") or [""])
        journal = journal[0] if journal else ""
        year = ""
        parts = item.get("issued", {}).get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
        pub_title = (item.get("title") or [""])[0]
        if doi:
            return doi, journal, year, pub_title
    return "", "", "", ""


def _search_arxiv_id(e, timeout):
    """arXiv title search for an entry that omits a DOI AND an arXiv id; return an
    arXiv id only on a strong match -- the title must be similar AND the first-author
    surname must match (particle-aware). No year/journal gate: an arXiv preprint
    predates publication and carries no journal, so title+author is the identity
    here (the same hardening as _search_doi, minus the venue corroboration that does
    not apply to a preprint). Returns '' when no confident match is found."""
    from .compare import _surname_match               # avoid import cycle
    from .sources import search_arxiv
    title = clean_tex(e.get("title", "")).strip()
    if len(title.split()) < 4:
        return ""
    bib_first = (split_authors(e.get("author", "")) or [""])[0]
    collab = is_collaboration(e.get("author", ""))
    for arxiv_id, rec in search_arxiv(title, timeout):
        if not title_similar(title, rec.get("title", "")):
            continue
        # First-author surname must match (skipped for a collaboration author).
        rec_first = rec.authors[0] if rec.authors else ""
        if not collab and bib_first and rec_first \
                and not _surname_match(bib_first, rec_first):
            continue
        return arxiv_id
    return ""


# --- advisory: chronological order within a \cite{} group ------------------

def _year_of(e):
    y = e.get("year", "").strip()
    return int(y[:4]) if y[:4].isdigit() else None


def chronological_order(groups, by_key, rep):
    """Advisory note (Layer-style): a multi-key \\cite{...} group whose members are
    not in non-decreasing year order. Citing the earliest work first is the closest
    thing to a real grouped-citation convention (and what most author-year styles'
    sort options produce), but it is not mandatory -- many numeric styles sort by
    appearance -- so this is only a note, attached to the group's first entry.
    Skipped when any member's year is unknown (can't judge)."""
    import os
    for keys, tex_path, tex_line in groups:
        ents = [by_key.get(k) for k in keys]
        if any(e is None for e in ents):
            continue
        years = [_year_of(e) for e in ents]
        if any(y is None for y in years):
            continue
        if years != sorted(years):
            order = ", ".join(f"{k} ({y})" for k, y in zip(keys, years))
            rep.add(Severity.INFO, (ents[0].key, tex_line),
                    "co-cited group is not in chronological "
                    f"order ({order}); some styles cite the earliest work first",
                    "verify", category="citation_order",
                    source_file=os.path.basename(tex_path))


# --- Layer 6: integrity + confidence scores --------------------------------
#
# Two orthogonal 0-100 metrics, because they answer different questions and drive
# different action:
#   INTEGRITY  -- is the bibliography sound? Only AUTHOR-FIXABLE defects dent it,
#                 weighted by how badly each compromises the reference. A clean entry
#                 is 1.0; a transcription/completeness slip costs a little; a likely
#                 wrong or unverifiable reference costs a lot. NOT lowered by how many
#                 sources corroborated (that is outside the author's control).
#   CONFIDENCE -- how much VeraCite trusts the verifications it made. High whenever a
#                 TRUSTED source confirmed the entry; we do NOT dock for "only one
#                 source" (a DOI resolving at Crossref is gold) nor for a field
#                 disagreeing (that is an integrity matter, not a sign we are unsure).
#                 Read as a quality signal, so it stays high for a sound check.
# So a clean-but-thinly-corroborated bib is integrity 100 / confidence lower, and a
# bib with a title typo on a DOI-resolved entry is integrity < 100 / confidence 100.

# Per-entry INTEGRITY credit, keyed by the worst author-fixable defect on the entry.
_INTEGRITY_CREDIT = {
    "clean":             1.00,
    "missing_pid":       0.85,   # a modern article omits an available DOI/PID
    "metadata_mismatch": 0.80,   # a field disagrees with the record (fixable)
    "dead_doi":          0.65,   # the recorded DOI is broken (wrong id on file)
    "unverified":        0.30,   # no record returned -- may not exist as written
    "mismatch":          0.20,   # the id resolves to a DIFFERENT paper
}
_DUP_PENALTY = 10                # flat points per duplicate (a file-level defect)
# A structurally-broken file (BibTeX cannot parse it: a stray '}', a brace imbalance,
# a missing '@type{key,' header, an entry with no key) is not a sound bibliography no
# matter how clean the entries that DID parse are -- so its integrity is CAPPED below
# the healthy band rather than reading 100/100 off the survivors (the 2212 'atomic'
# case: 23 structural errors, integrity 100). The cap is categorical (does the file
# parse?), so it is independent of the cascade COUNT -- one brace imbalance that drops
# 20 entries caps the same as one stray brace, avoiding double-counting the cascade.
_STRUCTURAL_ERROR_CATEGORIES = {"syntax", "missing_entry_header"}
_STRUCTURAL_CAP = 60             # < the 70 'yellow' band, so a broken file renders red

# Per-entry CONFIDENCE, keyed by HOW the entry resolved (trust in the source), NOT by
# whether a field disagreed. metadata_mismatch / source_conflict do not appear here.
_CONFIDENCE = {
    "trusted":        1.00,      # resolved by its own id at a trusted source
    "arxiv":          0.90,      # arXiv only (author-submitted metadata)
    "search":         0.85,      # recovered by title/author search (no id in entry)
    "dead_recovered": 0.80,      # recorded DOI broken, paper recovered by search
    "mismatch":       0.30,
    "unverified":     0.10,
}


def _integrity_defect(status, fcats, has_pid):
    """The worst author-fixable defect on one entry -> its integrity-credit key."""
    if status == "MISMATCH":
        return "mismatch"
    if status == "UNVERIFIED":
        return "unverified"
    if "dead_doi" in fcats:
        return "dead_doi"
    if "metadata_mismatch" in fcats:
        return "metadata_mismatch"
    if ("doi_available" in fcats or "pid_missing" in fcats) and not has_pid:
        return "missing_pid"
    return "clean"


def _confidence_kind(status, res):
    """How one entry resolved -> its confidence key (trust in the source)."""
    if status == "MISMATCH":
        return "mismatch"
    if status == "UNVERIFIED":
        return "unverified"
    if getattr(res, "dead_doi", False):
        return "dead_recovered"
    if getattr(res, "found_by_search", False):
        return "search"
    if set(getattr(res, "sources", {}) or {}) <= {"arxiv"}:
        return "arxiv"
    return "trusted"


# Mirror normalize.is_article_like (which keys off Entry.etype) for the record-dict
# path: the entry's persisted `entry_type`. Kept in sync with normalize._ARTICLE_LIKE_TYPES.
_ARTICLE_LIKE_TYPES_LOWER = {"article", "inproceedings", "conference"}


def _rec_confidence_kind(status, rec):
    """`_confidence_kind` for a record dict -- reads the persisted score inputs
    (dead_doi/found_by_search/sources) so a reprint buckets identically to the run
    that produced it."""
    if status == "MISMATCH":
        return "mismatch"
    if status == "UNVERIFIED":
        return "unverified"
    if rec.get("dead_doi"):
        return "dead_recovered"
    if rec.get("found_by_search"):
        return "search"
    if set(rec.get("sources") or []) <= {"arxiv"}:
        return "arxiv"
    return "trusted"


def _rec_has_pid(rec):
    ids = rec.get("identifiers") or {}
    return bool(ids.get("doi") or ids.get("arxiv") or ids.get("isbn"))


def integrity(records, rep):
    """Compute the bibliography integrity summary by PARSING the per-entry records --
    the single source of truth -- never from live Resolution objects. `records` is the
    list of entry-record dicts (`checkpoint.entry_record` shape) the run just built;
    `rep` supplies the live file-level findings (duplicates/conflicts/superseded),
    which are recomputed every run and not stored. Returns the summary dict.

    Deriving from records (not objects) is what makes a fresh run and a resumed run
    take the IDENTICAL path: the summary is a parse of the records, so a saved report
    reprints byte-stable. The score weighting is a transparent blend, documented in
    the README.

    All rates are over the *checked* entries: those actually resolved online (a record
    with a 'online' phase). Uncited entries and never-resolved ones are skipped by
    design -- not failures -- so they do not drag the denominator."""
    def status_of(rec):
        return rec.get("status")
    # 'checked' = records that carry an online resolution (status set by the pipeline).
    # An uncited or offline-only record has status None and is excluded, matching the
    # old "key in statuses" denominator.
    checked = [r for r in records if not r.get("uncited") and status_of(r) is not None]
    n = len(checked)
    verified = sum(1 for r in checked if status_of(r) == "VERIFIED")
    # Of the verified, how many carry a caveat (confidence < 1.0).
    verified_with_caveat = sum(1 for r in checked if status_of(r) == "VERIFIED"
                               and float(r.get("confidence") or 0.0) < 1.0)
    unverified = sum(1 for r in checked if status_of(r) == "UNVERIFIED")
    mismatch = sum(1 for r in checked if status_of(r) == "MISMATCH")

    def rec_year(rec):
        y = str(rec.get("bib_year") or "").strip()
        if y[:4].isdigit():
            return int(y[:4])
        cy = (rec.get("canonical_record") or {}).get("year")
        return cy if isinstance(cy, int) else None

    # DOI coverage over *eligible* records (post-2005 article-likes) among checked.
    eligible = [r for r in checked
                if (r.get("entry_type") or "").lower() in _ARTICLE_LIKE_TYPES_LOWER
                and (rec_year(r) or 9999) >= 2005]
    eligible_with_doi = sum(1 for r in eligible
                            if (r.get("identifiers") or {}).get("doi"))
    doi_cov = eligible_with_doi / len(eligible) if eligible else 1.0
    # PID coverage over checked entries (any strong id present).
    with_pid = sum(1 for r in checked if _rec_has_pid(r))
    pid_cov = with_pid / n if n else 1.0

    cat = lambda c: sum(1 for f in rep.live_findings() if f.category == c)
    duplicates = cat("duplicate")
    conflicts = cat("source_conflict")
    superseded = cat("preprint_superseded")
    # Any structural parse error (a stray brace, brace imbalance, missing entry
    # header, no citation key) means the FILE itself is broken -- recomputed live each
    # run like duplicates, so it stays out of the stored aggregate.
    has_structural_error = any(f.category in _STRUCTURAL_ERROR_CATEGORIES
                               for f in rep.live_findings())

    # Per-entry finding categories: from the record's own issues (the single source),
    # so the per-entry defect lookup matches what is persisted/printed.
    def fcats(rec):
        # SUPPRESSED issues are excluded -- they were retracted as false positives, so
        # they must not dent integrity (the record persists them, stamped, for audit;
        # aggregates ignore them, keeping scores byte-identical to pre-persist-all).
        return {(i.get("category") or "") for i in (rec.get("issues") or [])
                if not i.get("suppressed_by")}

    # INTEGRITY (0-100): mean per-entry credit by the worst author-fixable defect,
    # minus a flat penalty per duplicate. Clean bib = 100; a wrong/unverifiable
    # reference costs far more than a transcription slip. Corroboration depth excluded.
    credit_sum = sum(
        _INTEGRITY_CREDIT[_integrity_defect(status_of(r), fcats(r), _rec_has_pid(r))]
        for r in checked)
    integrity_score = (max(0, round(100 * credit_sum / n - _DUP_PENALTY * duplicates))
                       if n else None)
    # A structurally-broken file is capped below the healthy band: it cannot read as
    # sound off its surviving entries. Applied after the per-entry/duplicate maths so
    # a file that is ALSO bad on content scores lower still (min, never raises).
    if integrity_score is not None and has_structural_error:
        integrity_score = min(integrity_score, _STRUCTURAL_CAP)

    # CONFIDENCE (0-100): mean trust in the verification source.
    conf_sum = sum(_CONFIDENCE[_rec_confidence_kind(status_of(r), r)] for r in checked)
    confidence_score = round(100 * conf_sum / n) if n else None

    return {
        "checked": n, "verified": verified,
        "verified_with_caveat": verified_with_caveat,
        "unverified": unverified, "mismatch": mismatch,
        "doi_coverage": round(doi_cov, 3), "doi_eligible": len(eligible),
        "pid_coverage": round(pid_cov, 3),
        "duplicates": duplicates, "source_conflicts": conflicts,
        "preprints_with_published_version": superseded,
        "integrity_score": integrity_score,
        "confidence_score": confidence_score,
    }


class _Empty:
    record = None
    doi = arxiv_id = isbn = ""
    sources = {}
