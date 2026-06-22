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

    if res.dead_doi:
        # A DOI that does not resolve -- the dead_doi ERROR finding (record.py)
        # already carries the severity; here it just sets the status.
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
        if soft:
            conf, detail = 0.75, "right paper, but a field differs from the record"
        elif conflict:
            conf, detail = 0.70, "right paper, but authoritative sources disagree on a field"
        elif arxiv_only:
            conf, detail = 0.70, "confirmed by arXiv only (author-submitted, advisory)"
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
    strongest = "doi" if has_doi else "arxiv" if res.arxiv_id else "isbn" if res.isbn else ""

    if is_book(e):
        if not (res.isbn or has_doi):
            rep.add(Severity.WARN, e, "no ISBN or DOI -- a book should carry a "
                    "persistent identifier", category="pid_missing")
        return strongest

    if is_article_like(e):
        if has_doi:
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
            time_sleep(delay)
            if found:
                from .record import resolve_by_found_doi
                resolved = resolve_by_found_doi(e, found, res, rep, 0, timeout)
                verb = "found and verified" if resolved else "found"
                rep.add(Severity.WARN, e, f"DOI not recorded in the entry but {verb}: "
                        f"{found} (add it)", category="doi_available", field="doi")
                return "doi" if resolved else strongest
        # No DOI found. Only a MODERN article is expected to have one -- a pre-2005
        # work is not penalized for lacking a DOI.
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
    title = clean_tex(e.get("title", "")).strip()
    if len(title.split()) < 4:
        return ""
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
        if not title_similar(title, (item.get("title") or [""])[0]):
            continue
        # First-author surname must match (particle-aware). Skipped for a
        # collaboration author, which has no surname to compare.
        fams = [a.get("family") or a.get("name") or "" for a in (item.get("author") or [])]
        from .normalize import fold_surname
        if not collab and bib_first and fams \
                and not _surname_match(bib_first, fold_surname(fams[0])):
            continue
        # Type class must agree: a journal article should match a journal-article,
        # not a posted-content/report/dataset reprint of the same title.
        ctype = item.get("type", "")
        if want_journal and ctype not in ("journal-article", "proceedings-article", ""):
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
        # Require corroboration beyond title+author: the journal or the year.
        if journal_ok or year_ok:
            return item.get("DOI", "")
    return ""


def time_sleep(seconds):
    import time
    if seconds:
        time.sleep(seconds)


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
