"""Per-entry online resolution: drive the source fetchers and comparison layer.

This module orchestrates one entry's RECORD + STATUS layers -- resolve it by
DOI/arXiv id/ISBN, compare against the record(s), cross-check authoritative
sources, and check retraction/preprint/related works -- returning a Resolution.
The transport (http.py), the source fetchers (sources.py) and the comparison
logic (compare.py) live in their own modules; they are imported here (and
re-exported) so `resolve_entry` reads as a single straight-line pipeline and so
existing callers/tests that reference them as `record.X` keep working.

The comparison layer flags *disagreement* for a human -- it never rewrites the bib.
But the authoritative record (Crossref/arXiv) is the canonical reference: a flagged
discrepancy carries a suggested edit that conforms the bib TO the record (e.g.
year 2009 -> 2010), and severity follows the discrepancy's effect on the rendered
citation (a render-affecting field warns; a purely stylistic one is a note). The bib
value is preferred over the record only in the rare case the record is clearly
broken.
"""

import re
from dataclasses import dataclass, field

from .compare import compare_against_record, compare_sources
from .normalize import (DOI_FULL_RE, bare_doi, clean_tex, extract_arxiv_id,
                        extract_doi_from_url, extract_inspire_recid, extract_isbn,
                        fold_surname, is_book, is_preprint, split_authors, strip_tags)
from .report import Severity
from .titles import title_overlap


def _arxiv_abstract_by_title(e, timeout):
    """Best-effort abstract for an entry that has no abstract from Crossref/S2/
    OpenAlex AND no arXiv id of its own: search arXiv by title and return the top
    hit's abstract ONLY when that hit is confirmed to be the same work (strong title
    overlap AND first-author surname match). The confirmation is the same identity
    gate the DOI search uses, so a wrong abstract is never fed to the LLM (a mismatched
    abstract would mislead the relevance/wrong-paper rating -- never push a bad value).
    Returns '' when no confirmed match is found."""
    title = clean_tex(e.get("title", "")).strip()
    if len(title.split()) < 3:
        return ""   # too generic to search/confirm safely
    bib_first = (split_authors(e.get("author", "")) or [""])[0]
    for _aid, rec in search_arxiv(title, timeout)[:5]:
        if not rec or not (rec.get("abstract") or "").strip():
            continue
        if title_overlap(title, rec.get("title", "")) < 0.8:
            continue
        fams = rec.get("authors") or []
        if bib_first and fams and not _surname_match(bib_first, fams[0]):
            continue
        return rec.get("abstract", "")
    return ""
from .sources import (arxiv_fetch_was_transient, doi_registered_at_datacite,
                      fetch_abstract_s2, fetch_arxiv, fetch_crossref, fetch_datacite,
                      fetch_inspire, fetch_isbn, fetch_openalex, fetch_related,
                      search_arxiv)

# Re-exported for callers/tests that reach these as `record.X` (their logic lives
# in compare.py now). Listed in __all__-style here so a linter sees them as used.
from .compare import (  # noqa: F401  (re-export)
    _given_abbreviates, _is_initial, _journal_equiv, _surname_match)


# A PLACEHOLDER published DOI a publisher deposits before the real one is assigned,
# which arXiv may capture in its <arxiv:doi>. The APS form uses a zero volume and a
# dummy article number, e.g. '10.1103/PhysRevA.00.002400' -- never a real DOI. Such a
# value must not be suggested as the version to cite (it does not resolve).
_PLACEHOLDER_DOI_RE = re.compile(r"10\.1103/PhysRev[A-Z]*\.0+\.", re.I)


def _is_placeholder_doi(doi):
    """True if `doi` is a known publisher PLACEHOLDER (a provisional value deposited
    before the real DOI exists), so it should not be presented as a citable DOI."""
    return bool(doi) and bool(_PLACEHOLDER_DOI_RE.search(doi))


def _clean_url(url):
    """De-escape a URL pulled from a .bib for use as a clickable verify link. BibTeX
    URLs routinely carry TeX-escaped specials ('\\_', '\\&', '\\%', '\\#', '\\~{}',
    '{}' grouping) that are literal in the actual address, so a raw value would print
    'paper\\_files' instead of 'paper_files'. Strips the backslash before a special,
    collapses '\\~{}'/'\\~' to '~', and drops empty TeX braces."""
    url = url.strip()
    url = re.sub(r"\\~\{\}|\\~", "~", url)
    url = re.sub(r"\\([_&%#${}])", r"\1", url)
    url = url.replace("{}", "")
    return url


def verify_url(crossref_doi, arxiv_id, entry):
    """A URL a human can open to check an entry against the source of record: the
    resolvable DOI (redirects to the publisher's page) when there is a real DOI,
    else the arXiv abstract page, else any explicit url field (TeX-de-escaped)."""
    if crossref_doi:
        # The DOI may already be a full doi.org URL; don't prepend a second one.
        return f"https://doi.org/{bare_doi(crossref_doi)}"
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    return _clean_url(entry.get("url", ""))


@dataclass
class Resolution:
    """The outcome of resolving one entry online, consumed by the verification
    layer (verify.py) for status/score and the LLM layer for the abstract."""
    record: dict = None                  # primary record (None if unresolved)
    source: str = ""                     # which source the primary came from
    sources: dict = field(default_factory=dict)  # {source_name: record} resolved
    doi: str = ""                        # the resolvable Crossref DOI, if any
    doi_from_url: str = ""               # set when `doi` was mined from the url, not
                                         # a 'doi' field (the author should add it)
    arxiv_id: str = ""                   # the arXiv id, if any
    isbn: str = ""                       # the ISBN (books), if any
    dead_doi: bool = False               # a DOI that 404'd
    found_by_search: bool = False        # the record was recovered by a title+author
                                         # SEARCH (the bib carried no usable id), so
                                         # the match is weaker than an id-resolved one
    retracted: bool = False
    no_id: bool = False                  # had no DOI/arXiv id to start (a DOI may
                                         # still be found later by search)
    pid_missing: bool = False            # pid_check emitted a 'no PID' warning (an
                                         # entry with no findable identifier); the
                                         # deferred 'record_unresolved' note is then
                                         # redundant -- same root cause, same fix
    online_error: bool = False           # resolution failed on a TRANSIENT API error
                                         # (429/5xx/network), not a real miss -- so a
                                         # re-run should RETRY this entry's online phase
                                         # rather than treat it as settled
    llm_error: bool = False              # the LLM rating call FAILED (provider/CLI/
                                         # connection error), as opposed to a legitimate
                                         # 'no abstract' skip -- so a re-run should RETRY
                                         # the llm phase rather than treat it as settled


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
    # No usable 'doi' field, but publisher landing-page URLs carry the DOI in the
    # path (e.g. iopscience.iop.org/article/10.1088/2515-7647/acb57b). A DOI in the
    # url IS the canonical identifier, so mine it and resolve against THAT directly
    # -- rather than falling through to the fuzzy title search, which can match a
    # different arXiv version and invent a spurious year mismatch.
    doi_from_url = ""
    if not crossref_doi and "arxiv" not in e.get("url", "").lower():
        doi_from_url = extract_doi_from_url(e.get("url"), e.get("note"))
        if doi_from_url:
            crossref_doi = doi_from_url
    arxiv_id = extract_arxiv_id(e.get("eprint"), e.get("journal"), doi,
                                e.get("url"), e.get("note"))
    res.doi, res.arxiv_id = crossref_doi, arxiv_id or ""
    res.doi_from_url = doi_from_url
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
    # No DOI/arXiv id, but the entry's only locator is an INSPIRE-HEP page: the recid
    # in 'inspirehep.net/literature/<recid>' resolves the full record (incl. its
    # document_type, so a thesis/proceedings is typed correctly). This is the only way
    # to verify a thesis cited by its INSPIRE page alone.
    if rec is None and not crossref_doi and not arxiv_id:
        recid = extract_inspire_recid(e.get("url"), e.get("note"))
        if recid:
            insp = fetch_inspire(recid=recid, timeout=timeout)
            if insp:
                rec, source = insp, "inspire"
                res.found_by_search = True   # recovered from the page, no id in entry

    if rec is None and res.record is None:
        if crossref_doi and code == 404:
            # A Crossref 404 means Crossref has no record -- NOT that the DOI is dead.
            # Crossref and DataCite are separate registries; a Zenodo/Figshare/Dryad
            # software or dataset DOI (and some articles/books) resolves via DataCite.
            # Fetch the DataCite record and verify against it like any other source;
            # only call a DOI dead when it is absent from BOTH.
            dc_rec, dc_code = fetch_datacite(crossref_doi, timeout)
            if dc_rec is not None:
                rec, source = dc_rec, "datacite"
                # fall through to the shared "rec is not None" path below, which sets
                # res.record/source and runs compare_against_record.
            elif dc_code == 200 or doi_registered_at_datacite(crossref_doi, timeout):
                # Registered at DataCite but the metadata could not be parsed into a
                # record -- the DOI is valid (never a dead-DOI error), but there is
                # nothing to compare against.
                rep.add(Severity.WARN, e, f"DOI is registered with DataCite, not "
                        f"Crossref; its metadata could not be parsed to verify "
                        f"against: {doi}", "record", category="record_unresolved")
            else:
                res.dead_doi = True
                rep.add(Severity.ERROR, e, f"DOI does not resolve (404 at Crossref and "
                        f"DataCite): {doi}", "record", category="dead_doi")
        elif crossref_doi or arxiv_id:
            # Distinguish a TRANSIENT API failure (rate-limit/5xx/network) from a real
            # miss: the former is a VeraCite-side hiccup, not a problem with the
            # citation, so mark it retryable and say so -- a re-run will retry it.
            transient = bool(arxiv_id) and arxiv_fetch_was_transient(arxiv_id)
            if transient:
                res.online_error = True
                rep.add(Severity.WARN, e, f"could not retrieve record "
                        f"(doi={crossref_doi or '-'}, arxiv={arxiv_id or '-'}) -- the "
                        f"source was rate-limited or unreachable (transient); re-run to "
                        f"retry", "record", category="record_unresolved")
            else:
                rep.add(Severity.WARN, e, f"could not retrieve record "
                        f"(doi={crossref_doi or '-'}, arxiv={arxiv_id or '-'})", "record",
                        category="record_unresolved")
        elif not doi and not isbn and not (e.etype == "misc" and e.get("url").strip()):
            # Defer the 'no id to verify against' note: a DOI may still be found by
            # search in pid_check. The driver emits it only if the entry stays
            # unresolved (see emit of res.no_id).
            res.no_id = True
        # The DataCite branch above may have recovered a record; if so, fall through to
        # the shared compare path. Only return early when nothing was resolved.
        if rec is None:
            return res

    if rec is not None:
        res.record, res.source = rec, source
        res.sources[source] = rec
        compare_against_record(e, rec, source, rep, timeout=timeout)

    # INSPIRE (physics): a second authoritative source, used for cross-source
    # consistency. Resolved by DOI or arXiv id when one is present.
    if crossref_doi or arxiv_id:
        insp = fetch_inspire(doi=crossref_doi or None, arxiv_id=arxiv_id or None, timeout=timeout)
        if insp:
            res.sources["inspire"] = insp

    # PREPRINT -> published version: for an entry cited as a preprint, ask arXiv
    # whether a journal version is now linked (reusing the record if it already
    # came from arXiv). Resolved BEFORE the cross-source compare so the year gap a
    # superseded preprint creates (preprint year vs the later journal year) does not
    # also surface as a source_conflict -- the entry deliberately cites the preprint.
    #
    # "Cited as a preprint" means the entry points at the arXiv version, NOT the
    # journal: an entry that ALREADY records the published DOI (and just keeps the
    # arXiv id in eprint, as good practice) has nothing to supersede -- it cites the
    # version of record. So this whole check is skipped when the entry already
    # carries a resolvable journal DOI; otherwise it would tell a correctly-cited
    # entry to make a change it has already made.
    superseded = False
    already_cites_published = bool(crossref_doi)
    if is_preprint(e) and arxiv_id and not already_cites_published:
        arx = rec if source == "arxiv" else fetch_arxiv(arxiv_id, timeout)
        pub_doi = (arx or {}).get("published_doi", "")
        jref = (arx or {}).get("journal_ref", "")
        # arXiv sometimes links a PLACEHOLDER published DOI -- a provisional value a
        # publisher deposited before the real one was assigned (e.g. APS
        # '10.1103/PhysRevA.00.002400', volume '00'). It does not resolve, so it must
        # NOT be presented as the version to cite. Drop it; the arXiv `journal_ref`
        # usually carries the correct citation string ('Phys. Rev. A 109, 052425').
        if pub_doi and _is_placeholder_doi(pub_doi):
            pub_doi = ""
        if pub_doi or jref:
            superseded = True
            # Show the published version's TITLE (and journal/year) so a human can
            # confirm at a glance it is the SAME work -- a bare DOI forces them to
            # leave the report to check it is not, say, an erratum. The Crossref call
            # on the linked DOI also VALIDATES it: if it does not resolve, the DOI is
            # bad (another placeholder form), so fall back to the journal_ref.
            where = f"doi {pub_doi}" if pub_doi else jref
            # When the linked record's title/author DIVERGE from the bib's, the link may
            # be wrong (or the paper was retitled at publication) -- soften the claim so
            # a human verifies rather than blindly adopting a possibly-wrong DOI.
            diverges = False
            if pub_doi:
                pub, _ = fetch_crossref(pub_doi, timeout)
                if pub:
                    # Strip markup so a MathML-mangled registry title ('Fast collisional
                    # <mml:math>...') shows as clean text, not raw tags.
                    ptitle = strip_tags((pub.get("title") or "").strip())
                    pjournal = (pub.get("journal") or "").strip()
                    pyear = pub.get("year")
                    venue = ", ".join(x for x in (pjournal, str(pyear) if pyear else "") if x)
                    if ptitle:
                        where = f'"{ptitle[:80]}"' + (f" ({venue}, " if venue else " (") \
                                + f"doi {pub_doi})"
                    # Identity check the linked version against the bib: a low title
                    # overlap OR a first-author surname that does not match signals the
                    # link points elsewhere (the Jang2025 case: arXiv linked a same-author
                    # but differently-titled proceedings paper). Compare on the markup-
                    # stripped title so MathML noise alone never trips it.
                    btitle = clean_tex(e.get("title", "")).strip()
                    if btitle and ptitle and title_overlap(btitle, ptitle) < 0.6:
                        diverges = True
                    pub_first = (pub.get("authors") or [""])[0]
                    bib_first = (split_authors(e.get("author", "")) or [""])[0]
                    if bib_first and pub_first \
                            and not _surname_match(bib_first, pub_first):
                        diverges = True
                else:
                    # The linked DOI did not resolve -- do not present it. Use the
                    # journal_ref text if we have one; otherwise drop the finding.
                    pub_doi = ""
                    if jref:
                        where = jref
                    else:
                        superseded = False
            if superseded:
                # When arXiv links a RESOLVING published DOI, carry it as a structured
                # patch so a consumer can adopt it. When only a free-text journal_ref
                # is available (no usable DOI), DELIBERATELY carry NO suggestion -- it
                # is not machine-applicable, and guessing a DOI from the ref text risks
                # resolving a different paper (the Tarruell2019 failure mode).
                sug = {"field": "doi", "to": pub_doi} if pub_doi else None
                if diverges:
                    msg = (f"a published version MAY exist ({where}); its title or author "
                           f"differs from the entry -- verify it is the same work before "
                           f"citing it instead of the arXiv preprint")
                else:
                    msg = (f"a published version exists ({where}); consider citing it "
                           f"instead of the arXiv preprint")
                rep.add(Severity.WARN, e, msg, "preprint",
                        category="preprint_superseded",
                        field="doi" if pub_doi else "", suggested=sug)
        else:
            # arXiv has not back-linked a published version yet (its <arxiv:doi> is
            # empty), but the journal version may already exist in Crossref. Search
            # by title+first-author for a published journal-article of the same work
            # -- the case a recently-published preprint is in Crossref before arXiv
            # records the link. A strong match is suggested as the version to cite.
            from .verify import search_published_version
            pub_doi, pub_journal, pub_year, pub_title = search_published_version(e, timeout)
            if pub_doi:
                superseded = True
                # Show the published TITLE (+ journal/year/doi) so the human can
                # confirm the match at a glance -- the same affordance the arXiv-
                # linked path has. Falls back to 'doi <x>' if no title came back.
                venue = ", ".join(x for x in (pub_journal, pub_year) if x)
                if pub_title:
                    where = f'"{pub_title[:80]}"' + (f" ({venue}, " if venue else " (") \
                            + f"doi {pub_doi})"
                else:
                    where = f"doi {pub_doi}" + (f", {venue}" if venue else "")
                rep.add(Severity.WARN, e, f"a published version appears to exist "
                        f"({where}); consider citing it instead of the arXiv preprint",
                        "preprint", category="preprint_superseded",
                        field="doi", suggested={"field": "doi", "to": pub_doi})
        # A published version of record now exists, so citing it (the suggestion above)
        # also resolves any 'renamed in a later version' note the title compare emitted
        # -- the published title is the one to use. Suppress the dependent note so one
        # fix is not described twice (the SUPERSEDES table documents the relationship).
        if superseded:
            rep.supersede(e.key, "preprint_retitled")

    # CROSS-SOURCE (Layer 4): compare the authoritative records against each other.
    # When the entry is cited as an arXiv PREPRINT, the cited document is the arXiv
    # version, so its posting year is authoritative -- another source (INSPIRE,
    # Crossref) reporting the LATER journal year is the normal preprint->journal gap,
    # not a data conflict. Skip the year cross-source diff for any arXiv-cited
    # preprint (superseded or not); other fields still compare.
    cited_as_preprint = is_preprint(e) and bool(arxiv_id)
    if len(res.sources) > 1:
        compare_sources(e, res.sources, rep, skip_year=superseded or cited_as_preprint)

    rec = res.record
    # STATUS: retraction + abstract from one OpenAlex call, then chain S2 and
    # arXiv for any abstract still missing (for the LLM layer).
    oa = fetch_openalex(crossref_doi, timeout) if crossref_doi else None
    if oa and oa["is_retracted"]:
        res.retracted = True
        rep.add(Severity.ERROR, e, "marked RETRACTED in OpenAlex / Retraction Watch",
                "retract", category="retraction")
    if not rec.get("abstract") and oa and oa["abstract"]:
        rec["abstract"] = oa["abstract"]

    # RELATED WORKS: errata/corrections (action needed) and comments/replies
    # (informational). Read from whatever DOI resolved (the entry's own here; the
    # search path calls _check_related_works separately so a found-by-search entry
    # is also checked -- a correction was being missed for DOI-less entries).
    _check_related_works(e, res, crossref_doi, timeout, rep)

    if not rec.get("abstract"):
        rec["abstract"] = fetch_abstract_s2(crossref_doi, timeout)
    if not rec.get("abstract") and arxiv_id and source != "arxiv":
        arx = fetch_arxiv(arxiv_id, timeout)
        rec["abstract"] = (arx or {}).get("abstract", "")
    # Last resort: the work has no arXiv id of its own, but it may still be on arXiv
    # (e.g. an astro paper Crossref/S2 carry without an abstract). Search by title and
    # use the confirmed hit's abstract, so the LLM has real evidence to rate against.
    if not rec.get("abstract"):
        rec["abstract"] = _arxiv_abstract_by_title(e, timeout)
    return res


def _check_related_works(e, res, doi, timeout, rep):
    """Emit related-work findings (correction/erratum/addendum -> WARN; comment/
    reply -> INFO) for the entry's resolved record. The relation graph comes from
    the Crossref record already fetched (its `relations`, parsed from `updated-by`/
    `relation`); a title search is the only extra request. Called from BOTH the
    normal path and the found-by-search path, so an entry whose DOI was discovered
    by search (no `doi` field, common in this corpus) is checked too -- otherwise a
    published correction is silently missed."""
    cr = res.sources.get("crossref")
    relations = cr.get("relations") if cr else None
    for label, target, ct in fetch_related(doi, e.get("title", ""), timeout,
                                           relations=relations):
        note = f" -- {ct[:70]}" if ct else ""
        if label in ("correction", "erratum", "addendum", "retraction", "publisher-note"):
            # Capitalize the label so an action-needed change to the cited work
            # (a CORRECTION/ERRATUM/RETRACTION) stands out from routine metadata
            # findings in the entry's block -- it can affect what is being cited.
            rep.add(Severity.WARN, e, f"linked {label.upper()} ({target}); verify the "
                    f"entry is still accurate{note}", "related", category="related_work")
        else:   # comment, reply, response
            rep.add(Severity.INFO, e, f"related {label} exists ({target}){note}",
                    "related", category="related_work")


def resolve_by_found_doi(e, doi, res, rep, delay, timeout):
    """Resolve an entry against a DOI discovered by search (the bib omitted it).
    Fetches the record, compares the entry to it, and updates `res` so the entry
    is treated as resolved (verification status, abstract for the LLM). Only called
    with a strongly-matched DOI (see verify._search_doi), so we are not verifying
    against an arbitrary record. Returns True if the record resolved."""
    rec, code = fetch_crossref(doi, timeout)
    if rec is None:
        return False
    res.record, res.source, res.doi = rec, "crossref", doi
    res.found_by_search = True
    res.sources["crossref"] = rec
    compare_against_record(e, rec, "crossref", rep)
    _check_related_works(e, res, doi, timeout, rep)
    oa = fetch_openalex(doi, timeout)
    if oa and oa.get("is_retracted"):
        res.retracted = True
        rep.add(Severity.ERROR, e, "marked RETRACTED in OpenAlex / Retraction Watch",
                "retract", category="retraction")
    if not rec.get("abstract") and oa and oa.get("abstract"):
        rec["abstract"] = oa["abstract"]
    # Chain the same abstract fallback as resolve_entry (Crossref rarely carries an
    # abstract, especially for older papers) so the LLM can rate this entry too,
    # including a title-confirmed arXiv abstract when S2/OpenAlex have none.
    if not rec.get("abstract"):
        rec["abstract"] = fetch_abstract_s2(doi, timeout)
    if not rec.get("abstract"):
        rec["abstract"] = _arxiv_abstract_by_title(e, timeout)
    return True


def resolve_by_found_arxiv(e, arxiv_id, res, rep, timeout):
    """Resolve an entry against an arXiv id discovered by title search (the bib
    omitted both a DOI and an arXiv id). Fetches the arXiv record and updates `res`
    so the entry is VERIFIED (and its abstract is available to the LLM).

    arXiv records the PUBLISHED version once it is linked (<arxiv:doi>). When that
    DOI is present it is the stronger, citable identifier, so we resolve the entry
    against THAT (Crossref -- real venue, proper verify link) and keep the arXiv id
    alongside. Returns ('doi', published_doi) when a published DOI was used, else
    ('arxiv', arxiv_id) for an arXiv-only preprint, or (None, '') if nothing
    resolved -- so the caller can suggest the best identifier to record."""
    arx = fetch_arxiv(arxiv_id, timeout)
    if arx is None:
        return None, ""
    res.arxiv_id = arxiv_id
    res.no_id = False
    res.found_by_search = True   # the bib carried no id; this arXiv hit came from a
                                 # title search, so the match is weaker than id-resolved
    res.sources["arxiv"] = arx

    pub_doi = bare_doi((arx.get("published_doi") or "").strip())
    if pub_doi and DOI_FULL_RE.match(pub_doi):
        # Prefer the published version: resolve it on Crossref. If that succeeds the
        # entry verifies against the real venue record (the arXiv hit was the bridge).
        cr, code = fetch_crossref(pub_doi, timeout)
        if cr is not None:
            res.record, res.source, res.doi = cr, "crossref", pub_doi
            res.sources["crossref"] = cr
            rep.set_link(e.key, f"https://doi.org/{pub_doi}")
            compare_against_record(e, cr, "crossref", rep)
            # Check the published version for a linked correction/erratum -- this
            # path resolves DOI-less entries (e.g. Acharya2024), which otherwise
            # never had their corrections checked.
            _check_related_works(e, res, pub_doi, timeout, rep)
            if not cr.get("abstract"):
                cr["abstract"] = arx.get("abstract", "")
            return "doi", pub_doi

    # arXiv-only (no published DOI, or it did not resolve): verify against arXiv.
    res.record, res.source = arx, "arxiv"
    rep.set_link(e.key, f"https://arxiv.org/abs/{arxiv_id}")
    compare_against_record(e, arx, "arxiv", rep, timeout=timeout)
    return "arxiv", arxiv_id
