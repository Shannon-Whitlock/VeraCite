"""Per-entry online resolution: drive the source fetchers and comparison layer.

This module orchestrates one entry's RECORD + STATUS layers -- resolve it by
DOI/arXiv id/ISBN, compare against the record(s), cross-check authoritative
sources, and check retraction/preprint/related works -- returning a Resolution.
The transport (http.py), the source fetchers (sources.py) and the comparison
logic (compare.py) live in their own modules; they are imported here (and
re-exported) so `resolve_entry` reads as a single straight-line pipeline and so
existing callers/tests that reference them as `record.X` keep working.

No registry is treated as canonical truth -- the bib and the registry are
independent transcriptions, and the comparison layer flags *disagreement* for a
human rather than asserting which side is right.
"""

import re
import time
from dataclasses import dataclass, field

from .compare import compare_against_record, compare_sources
from .normalize import (DOI_FULL_RE, bare_doi, extract_arxiv_id, extract_isbn, is_book,
                        is_preprint)
from .report import Severity
from .sources import (fetch_abstract_s2, fetch_arxiv, fetch_crossref,
                      fetch_inspire, fetch_isbn, fetch_openalex, fetch_related)

# Re-exported for callers/tests that reach these as `record.X` (their logic lives
# in compare.py now). Listed in __all__-style here so a linter sees them as used.
from .compare import (  # noqa: F401  (re-export)
    _given_abbreviates, _is_initial, _journal_equiv, _surname_match)


def verify_url(crossref_doi, arxiv_id, entry):
    """A URL a human can open to check an entry against the source of record: the
    resolvable DOI (redirects to the publisher's page) when there is a real DOI,
    else the arXiv abstract page, else any explicit url field."""
    if crossref_doi:
        # The DOI may already be a full doi.org URL; don't prepend a second one.
        return f"https://doi.org/{bare_doi(crossref_doi)}"
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    return entry.get("url", "").strip()


@dataclass
class Resolution:
    """The outcome of resolving one entry online, consumed by the verification
    layer (verify.py) for status/score and the LLM layer for the abstract."""
    record: dict = None                  # primary record (None if unresolved)
    source: str = ""                     # which source the primary came from
    sources: dict = field(default_factory=dict)  # {source_name: record} resolved
    doi: str = ""                        # the resolvable Crossref DOI, if any
    arxiv_id: str = ""                   # the arXiv id, if any
    isbn: str = ""                       # the ISBN (books), if any
    dead_doi: bool = False               # a DOI that 404'd
    retracted: bool = False
    no_id: bool = False                  # had no DOI/arXiv id to start (a DOI may
                                         # still be found later by search)


def resolve_entry(e, rep, delay, timeout):
    """RECORD + STATUS layers for ONE entry: resolve it by DOI/arXiv id/ISBN,
    compare against the record(s), cross-check authoritative sources, and check
    retraction/preprint/related works. Returns a Resolution. This is the per-entry
    unit the CLI driver calls so every layer for an entry runs before the next,
    keeping the printed report in bibtex order."""
    res = Resolution()
    doi = e.get("doi", "").strip()
    # Strip a leading doi.org URL so the bare DOI is used for resolution, the
    # verify link and the JSON (the entry may write 'https://doi.org/10.x/...').
    doi = bare_doi(doi)
    # A 'doi' field that is not actually DOI-shaped (a pasted citation key, an
    # arXiv id, or a DOI garbled with extra text) cannot be resolved on Crossref --
    # say so rather than 404, and do not build a doi.org verify link from the junk.
    # Anchored (DOI_FULL_RE), so a DOI merely embedded in a mangled value does NOT
    # qualify; this matches the doi_format rule's notion of a valid DOI.
    doi_shaped = bool(DOI_FULL_RE.match(doi))
    crossref_doi = doi if doi_shaped and "arxiv" not in doi.lower() else ""
    arxiv_id = extract_arxiv_id(e.get("eprint"), e.get("journal"), doi,
                                e.get("url"), e.get("note"))
    res.doi, res.arxiv_id = crossref_doi, arxiv_id or ""
    rep.set_link(e.key, verify_url(crossref_doi, arxiv_id, e))

    # A malformed 'doi' value is ALREADY reported, with the exact value, by the
    # offline doi_format rule (an 'identifier_format' finding that fires for any doi
    # not matching DOI_FULL_RE). Re-stating it here as 'not a DOI; cannot verify'
    # was a duplicate -- and read as a contradiction when the PID search below then
    # found and verified the real DOI ('cannot verify' next to 'found and verified').
    # So this layer no longer emits its own malformed-doi finding.

    # BOOK: resolve by ISBN before the article path. The ISBN may be an explicit
    # field or embedded in the url/note (publisher URLs often carry it).
    book_entry = is_book(e)
    isbn = e.get("isbn", "").strip() or (extract_isbn(e.get("url"), e.get("note"))
                                         if book_entry else "")
    if isbn and book_entry:
        res.isbn = isbn
        book = fetch_isbn(isbn, timeout)
        time.sleep(delay)
        if book:
            res.sources["isbn"] = book
            if res.record is None:
                res.record, res.source = book, "isbn"
            compare_against_record(e, book, "isbn", rep)
        else:
            rep.add(Severity.WARN, e, f"ISBN did not resolve to a book record: {isbn}",
                    "record", category="metadata_mismatch")

    rec, source, code = None, None, None
    if crossref_doi:
        rec, code = fetch_crossref(crossref_doi, timeout)
        source = "crossref"
    if rec is None and arxiv_id:
        rec, source = fetch_arxiv(arxiv_id, timeout), "arxiv"

    if rec is None and res.record is None:
        if crossref_doi and code == 404:
            res.dead_doi = True
            rep.add(Severity.ERROR, e, f"DOI does not resolve on Crossref (404): {doi}",
                    "record", category="dead_doi")
        elif crossref_doi or arxiv_id:
            rep.add(Severity.WARN, e, f"could not retrieve record "
                    f"(doi={crossref_doi or '-'}, arxiv={arxiv_id or '-'})", "record",
                    category="record_unresolved")
        elif not doi and not isbn and not (e.etype == "misc" and e.get("url").strip()):
            # Defer the 'no id to verify against' note: a DOI may still be found by
            # search in pid_check. The driver emits it only if the entry stays
            # unresolved (see emit of res.no_id).
            res.no_id = True
        return res

    if rec is not None:
        res.record, res.source = rec, source
        res.sources[source] = rec
        compare_against_record(e, rec, source, rep)

    # INSPIRE (physics): a second authoritative source, used for cross-source
    # consistency. Resolved by DOI or arXiv id when one is present.
    if crossref_doi or arxiv_id:
        insp = fetch_inspire(doi=crossref_doi or None, arxiv_id=arxiv_id or None, timeout=timeout)
        time.sleep(delay)
        if insp:
            res.sources["inspire"] = insp
    # CROSS-SOURCE (Layer 4): compare the authoritative records against each other.
    if len(res.sources) > 1:
        compare_sources(e, res.sources, rep)

    rec = res.record
    # STATUS: retraction + abstract from one OpenAlex call, then chain S2 and
    # arXiv for any abstract still missing (for the LLM layer).
    oa = fetch_openalex(crossref_doi, timeout) if crossref_doi else None
    time.sleep(delay)
    if oa and oa["is_retracted"]:
        res.retracted = True
        rep.add(Severity.ERROR, e, "marked RETRACTED in OpenAlex / Retraction Watch",
                "retract", category="retraction")
    if not rec.get("abstract") and oa and oa["abstract"]:
        rec["abstract"] = oa["abstract"]

    # PREPRINT -> published version: for an entry cited as a preprint, ask arXiv
    # whether a journal version is now linked (reusing the record if it already
    # came from arXiv).
    if is_preprint(e) and arxiv_id:
        arx = rec if source == "arxiv" else fetch_arxiv(arxiv_id, timeout)
        if source != "arxiv":
            time.sleep(delay)
        pub_doi = (arx or {}).get("published_doi", "")
        jref = (arx or {}).get("journal_ref", "")
        if pub_doi or jref:
            where = f"doi {pub_doi}" if pub_doi else jref
            rep.add(Severity.WARN, e, f"a published version exists ({where}); "
                    f"consider citing it instead of the arXiv preprint",
                    "preprint", category="preprint_superseded")

    # RELATED WORKS: errata/corrections (action needed) and comments/replies
    # (informational). The relation graph is read from the Crossref record already
    # fetched (no second /works call); the title search is the only extra request.
    cr = res.sources.get("crossref")
    relations = cr.get("relations") if cr else None
    for label, target, ct in fetch_related(crossref_doi, e.get("title", ""), timeout,
                                            relations=relations):
        note = f" -- {ct[:70]}" if ct else ""
        if label in ("correction", "erratum", "addendum", "retraction", "publisher-note"):
            rep.add(Severity.WARN, e, f"linked {label} ({target}); verify the entry "
                    f"is still accurate{note}", "related", category="related_work")
        else:   # comment, reply, response
            rep.add(Severity.INFO, e, f"related {label} exists ({target}){note}",
                    "related", category="related_work")
    time.sleep(delay)

    if not rec.get("abstract"):
        rec["abstract"] = fetch_abstract_s2(crossref_doi, timeout)
        time.sleep(delay)
    if not rec.get("abstract") and arxiv_id and source != "arxiv":
        arx = fetch_arxiv(arxiv_id, timeout)
        rec["abstract"] = (arx or {}).get("abstract", "")
        time.sleep(delay)
    time.sleep(delay)
    return res


def resolve_by_found_doi(e, doi, res, rep, delay, timeout):
    """Resolve an entry against a DOI discovered by search (the bib omitted it).
    Fetches the record, compares the entry to it, and updates `res` so the entry
    is treated as resolved (verification status, abstract for the LLM). Only called
    with a strongly-matched DOI (see verify._search_doi), so we are not verifying
    against an arbitrary record. Returns True if the record resolved."""
    rec, code = fetch_crossref(doi, timeout)
    time.sleep(delay)
    if rec is None:
        return False
    res.record, res.source, res.doi = rec, "crossref", doi
    res.sources["crossref"] = rec
    compare_against_record(e, rec, "crossref", rep)
    oa = fetch_openalex(doi, timeout)
    time.sleep(delay)
    if oa and oa.get("is_retracted"):
        res.retracted = True
        rep.add(Severity.ERROR, e, "marked RETRACTED in OpenAlex / Retraction Watch",
                "retract", category="retraction")
    if not rec.get("abstract") and oa and oa.get("abstract"):
        rec["abstract"] = oa["abstract"]
    # Chain the same abstract fallback as resolve_entry (Crossref rarely carries an
    # abstract, especially for older papers) so the LLM can rate this entry too.
    if not rec.get("abstract"):
        rec["abstract"] = fetch_abstract_s2(doi, timeout)
        time.sleep(delay)
    return True
