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
from .titles import title_similar

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

    if is_book(e):
        if not (res.isbn or has_doi):
            rep.add(Severity.WARN, e, "no ISBN or DOI -- a book should carry a "
                    "persistent identifier", category="pid_missing")
        return strongest

    if is_article_like(e):
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
        # pid_missing/pid_optional note is only for an entry with no DOI field at all.
        if has_doi:
            return strongest
        # Only a MODERN article is expected to have one -- a pre-2005 work is not
        # penalized for lacking a DOI.
        if modern:
            rep.add(Severity.WARN, e, "no DOI recorded for a post-2005 article "
                    "(none found in Crossref either)", category="pid_missing")
        else:
            # A pre-2005 work legitimately has no DOI: this is reassurance, not a
            # defect, so it is its own note-level category (NOT 'pid_missing', which
            # is a warning -- pinning that here would wrongly promote this to WARN).
            rep.add(Severity.INFO, e, f"DOI not required for publication year {year} "
                    "(< 2005) and none found", "verify", category="pid_optional")
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
    # A 1-2 word title is too generic to search on (the query returns noise and a
    # short title can collide with a different work). A SHORT (3-word) title is
    # allowed, but only an EXACT normalized-title match counts for it -- not the
    # tolerant title_similar overlap -- so 'Universal Quantum Simulators' resolves
    # while a generic 3-word title cannot ride the looser overlap into a wrong hit.
    if nwords < 3:
        return ""
    short_title = nwords < 4
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
        if short_title:
            if not exact_title:
                continue
        elif not (exact_title or title_similar(title, cand_title)):
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
    for keys in groups:
        ents = [by_key.get(k) for k in keys]
        if any(e is None for e in ents):
            continue
        years = [_year_of(e) for e in ents]
        if any(y is None for y in years):
            continue
        if years != sorted(years):
            order = ", ".join(f"{k} ({y})" for k, y in zip(keys, years))
            rep.add(Severity.INFO, ents[0], "co-cited group is not in chronological "
                    f"order ({order}); some styles cite the earliest work first",
                    "verify", category="citation_order")


# --- Layer 6: integrity score + coverage -----------------------------------

def integrity(entries, statuses, results, rep):
    """Compute the bibliography integrity summary from per-reference statuses and
    the collected findings. `statuses` is {key: (status, confidence)}, `results`
    is {key: Resolution}. Returns a dict of summary metrics (also used by --json).
    The score is a transparent weighted blend, documented in the README.

    All rates are computed over the *checked* entries only -- the ones actually
    resolved online (in --tex mode, the cited subset; otherwise every entry).
    Uncited entries are skipped by design, not verification failures, so counting
    them in the denominator would understate the score of the references that were
    examined. `statuses` holds exactly the checked keys (only the analysis pipeline
    writes it), so it defines the denominator."""
    checked = [e for e in entries if e.key in statuses]
    n = len(checked)
    verified = sum(1 for s, _ in statuses.values() if s == "VERIFIED")
    # Of the verified, how many carry a caveat (confidence < 1.0: a field differs,
    # sources disagree, or only arXiv confirms). Reported separately so the roll-up
    # still distinguishes a clean pass from a checked-the-detail pass.
    verified_with_caveat = sum(1 for s, c in statuses.values()
                               if s == "VERIFIED" and c < 1.0)
    unverified = sum(1 for s, _ in statuses.values() if s == "UNVERIFIED")
    mismatch = sum(1 for s, _ in statuses.values() if s == "MISMATCH")
    passed = verified

    # DOI coverage over *eligible* records (post-2005 article-likes) among checked.
    eligible = [e for e in checked if is_article_like(e)
                and (_entry_year(e, results.get(e.key, _Empty())) or 9999) >= 2005]
    eligible_with_doi = sum(1 for e in eligible
                            if (results.get(e.key) and results[e.key].doi))
    doi_cov = eligible_with_doi / len(eligible) if eligible else 1.0
    # PID coverage over checked entries (any strong id present).
    with_pid = sum(1 for e in checked
                   if results.get(e.key) and (results[e.key].doi or results[e.key].arxiv_id
                                              or results[e.key].isbn))
    pid_cov = with_pid / n if n else 1.0

    cat = lambda c: sum(1 for f in rep.live_findings() if f.category == c)
    duplicates = cat("duplicate")
    conflicts = cat("source_conflict")
    superseded = cat("preprint_superseded")

    # Integrity score (0-100): verification rate (50), PID coverage (20), DOI
    # coverage of eligible records (15), freedom from integrity defects (15).
    verify_rate = passed / n if n else 1.0
    defect_penalty = min(1.0, (duplicates + unverified + mismatch + conflicts) / n) if n else 0.0
    score = round(100 * (0.50 * verify_rate + 0.20 * pid_cov + 0.15 * doi_cov
                         + 0.15 * (1 - defect_penalty)))

    return {
        "checked": n, "verified": verified,
        "verified_with_caveat": verified_with_caveat,
        "unverified": unverified, "mismatch": mismatch,
        "doi_coverage": round(doi_cov, 3), "doi_eligible": len(eligible),
        "pid_coverage": round(pid_cov, 3),
        "duplicates": duplicates, "source_conflicts": conflicts,
        "preprints_with_published_version": superseded,
        "integrity_score": score,
    }


class _Empty:
    record = None
    doi = arxiv_id = isbn = ""
    sources = {}
