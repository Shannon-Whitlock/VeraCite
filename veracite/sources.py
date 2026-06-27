"""External record sources: resolve an identifier (DOI / arXiv id / ISBN) to a
normalized metadata record, and find related works (errata/corrections/comments).

Each bibliographic fetcher returns a `Record` (models.Record) -- one documented
shape across Crossref, arXiv, INSPIRE and the ISBN lookups (arXiv additionally
fills published_doi / journal_ref). The OpenAlex lookup is a status payload
(retraction + abstract), not a bibliographic record, so it stays a plain dict. No
source is canonical: the comparison layer flags disagreement, it does not pick a
winner. All HTTP goes through the helpers in http.py.
"""

import re
import time

from .config import endpoint
from .http import http_get_json, http_get_text
from .models import Record
from .normalize import clean_tex, fold_surname, strip_math, strip_tags


# Crossref `relation`/`updated-by` types that signal a related work. Read straight
# from the work response, so the related-works check needs no second fetch.
_RELATION_TYPES = {
    "is-corrected-by": "correction", "has-correction": "correction",
    "is-erratum-of": "erratum", "has-erratum": "erratum",
    "is-addendum-to": "addendum", "has-addendum": "addendum",
    "is-comment-on": "comment", "has-comment": "comment",
    "is-reply-to": "reply", "has-reply": "reply",
}
_UPDATE_TYPES = {"correction", "erratum", "addendum", "corrigendum"}


def _extract_relations(msg):
    """(relationship_label, target_doi) pairs from a Crossref work's `relation`
    and `updated-by` blocks -- the machine-readable related-work links a publisher
    deposited (many do not)."""
    rels = []
    for rtype, items in (msg.get("relation") or {}).items():
        if rtype in _RELATION_TYPES:
            rels += [(_RELATION_TYPES[rtype], it.get("id", "")) for it in items]
    for upd in (msg.get("updated-by") or []):
        kind = (upd.get("type") or "").lower()
        if kind in _UPDATE_TYPES:
            # An `updated-by` entry carries the target DOI under the key `DOI`
            # (uppercase), not `id` -- reading `id` lost the target, so the
            # correction was parsed but silently dropped (its empty target failed
            # the truthiness filter in fetch_related). Accept either key.
            rels.append((kind, upd.get("DOI") or upd.get("id", "")))
    return rels


# --- record fetchers -------------------------------------------------------

def doi_registered_at_datacite(doi, timeout):
    """True if `doi` is registered with DataCite (HTTP 200 from its API). Crossref and
    DataCite are SEPARATE DOI registries: a Zenodo/Figshare/Dryad dataset or software
    DOI ('10.5281/zenodo.3937751') is a real, resolving DOI that Crossref returns 404
    for -- so a Crossref 404 alone does NOT mean a DOI is dead. The dead-DOI check
    consults this before declaring an error, so a valid DataCite DOI is never mis-
    reported as unresolvable. Network failure -> False (do not assert resolution we
    could not confirm; the caller stays conservative)."""
    if not doi:
        return False
    _data, code = http_get_json(endpoint("datacite_doi", doi=doi), timeout)
    return code == 200


def fetch_crossref(doi, timeout):
    """Resolve a DOI to a normalized record via Crossref. Returns (record, code);
    record is None on failure, with the HTTP status in `code`. The record also
    carries any related-work links from the same response (see `relations`), so the
    related-works check reuses this fetch instead of querying the work again."""
    data, code = http_get_json(endpoint("crossref_work", doi=doi), timeout)
    if code != 200 or not data:
        return None, code
    msg = data.get("message", {})
    authors, authors_display, given, given_full = [], [], {}, {}
    for a in (msg.get("author") or []):
        raw = clean_tex(a.get("family") or a.get("name") or "").strip()
        surname = fold_surname(raw)
        if not surname:
            continue
        authors.append(surname)
        authors_display.append(raw)          # original surname, for the message
        # keep the first given-name token for given-name verification; Crossref
        # carries structured names, unlike arXiv's last-token-only folding. Keep the
        # FULL given string too, so a mis-split compound surname can be reconstructed.
        given_raw = clean_tex(a.get("given") or "").strip()
        g = given_raw.split()
        if g:
            given[surname] = g[0]
            given_full[surname] = given_raw
    year = None
    for k in ("published-print", "published-online", "issued", "published"):
        parts = msg.get(k, {}).get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    return Record(
        authors=authors,
        authors_display=authors_display,
        given=given,
        given_full=given_full,
        year=year,
        volume=str(msg.get("volume", "") or ""),
        number=str(msg.get("issue", "") or ""),
        pages=str(msg.get("page", "") or ""),
        title=(msg.get("title") or [""])[0],
        journal=(msg.get("container-title") or [""])[0],
        abstract=strip_tags(msg.get("abstract", "")),
        # Crossref's work `type` (journal-article / book-chapter / proceedings-article
        # / book / ...) normalized to the labels the entry-type check understands, so a
        # bib type that disagrees with the record (e.g. a @book that is really a journal
        # article, or an @article that is really a book chapter) can be flagged.
        document_type=_crossref_doc_type(msg.get("type", "")),
        relations=_extract_relations(msg),
    ), code


# Crossref work `type` -> the normalized label the entry-type check keys on. Only the
# types that map to a clear biblatex entry class are translated; anything else (a
# 'dataset', 'report', 'posted-content' preprint, ...) yields '' (no type claim).
_CROSSREF_TYPE_LABELS = {
    "journal-article": "journal article",
    "proceedings-article": "proceedings",
    "book-chapter": "book chapter",
    "reference-entry": "book chapter",
    "book": "book",
    "monograph": "book",
    "reference-book": "book",
    "edited-book": "book",
}


def _crossref_doc_type(t):
    return _CROSSREF_TYPE_LABELS.get((t or "").lower(), "")


# DataCite `resourceTypeGeneral` (its controlled vocabulary) -> a normalized
# document_type. Classification keys on this field, NEVER on the title: a journal
# article and its accompanying Zenodo dataset can share a title, so only the
# registered TYPE distinguishes them. Two buckets matter to the comparison layer:
#   * article/book-like types resolve to the SAME labels Crossref uses, so a
#     DataCite-registered article gets the normal full comparison and the existing
#     entry-type check works unchanged;
#   * the data/software-like types map to their own labels, which the compare layer
#     treats as non-article (title+author+year only) and which let an @article that
#     resolved to data be flagged as a likely wrong-object citation.
# Anything unlisted yields '' (no type claim) -- conservative, never a false flag.
_DATACITE_TYPE_LABELS = {
    # article / book-like -> Crossref-compatible labels (normal comparison)
    "journalarticle": "journal article",
    "conferencepaper": "proceedings",
    "conferenceproceeding": "proceedings",
    "datapaper": "journal article",
    "preprint": "journal article",
    "book": "book",
    "bookchapter": "book chapter",
    "dissertation": "thesis",
    # data / software-like -> own labels (non-article comparison + wrong-object guard)
    "software": "software",
    "dataset": "dataset",
    "model": "dataset",
    "workflow": "software",
    "computationalnotebook": "software",
    "collection": "dataset",
    "image": "dataset",
    "physicalobject": "dataset",
    "service": "software",
    "sound": "dataset",
    "audiovisual": "dataset",
}

# The normalized labels that denote a NON-article object (data/software/etc.). The
# compare layer reads this to (a) skip the article-only locators (volume/issue/pages/
# journal) and (b) flag an @article/@inproceedings that resolved to one of these.
NONARTICLE_DOC_TYPES = {"software", "dataset"}


def _datacite_doc_type(resource_type_general):
    return _DATACITE_TYPE_LABELS.get((resource_type_general or "").lower(), "")


def fetch_datacite(doi, timeout):
    """Resolve a DOI to a normalized record via DataCite (the registry behind Zenodo,
    figshare, Dryad, OSF -- software and datasets, but also some articles/books).
    Returns (record, code); record is None on failure with the HTTP status in `code`.

    DataCite and Crossref are separate registries, so this is the fallback when
    Crossref 404s but the DOI still resolves (record.py). The record's `document_type`
    comes from DataCite's `resourceTypeGeneral` (Software/Dataset/JournalArticle/...),
    which -- not the title -- is what classifies the object: a paper and its companion
    dataset may share a title, so only the registered type tells them apart."""
    data, code = http_get_json(endpoint("datacite_doi", doi=doi), timeout)
    if code != 200 or not data:
        return None, code
    attr = (data.get("data") or {}).get("attributes") or {}
    authors, authors_display, given, given_full = [], [], {}, {}
    for c in (attr.get("creators") or []):
        # Prefer the structured familyName; fall back to a "Family, Given" or plain
        # `name`. nameType "Organizational" (a lab/consortium) has no surname to fold
        # -- keep it as a display name only, never as a matchable author key.
        fam = clean_tex(c.get("familyName") or "").strip()
        giv = clean_tex(c.get("givenName") or "").strip()
        if not fam:
            name = clean_tex(c.get("name") or "").strip()
            if (c.get("nameType") or "").lower().startswith("organ") or not name:
                continue
            if "," in name:
                fam, _, giv = (p.strip() for p in (name.split(",", 1) + [""])[:3])
            else:
                parts = name.split()
                fam, giv = parts[-1], " ".join(parts[:-1])
        surname = fold_surname(fam)
        if not surname:
            continue
        authors.append(surname)
        authors_display.append(fam)
        if giv:
            given[surname] = giv.split()[0]
            given_full[surname] = giv
    titles = attr.get("titles") or []
    title = (titles[0].get("title") if titles else "") or ""
    year = attr.get("publicationYear")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    rtg = ((attr.get("types") or {}).get("resourceTypeGeneral")) or ""
    return Record(
        authors=authors,
        authors_display=authors_display,
        given=given,
        given_full=given_full,
        year=year,
        title=title,
        # The repository ("Zenodo"/"Dryad") is the nearest analog to a journal, but it
        # is NOT compared for non-article records (the compare layer skips it), so it is
        # carried for completeness only.
        journal=(attr.get("publisher") or ""),
        document_type=_datacite_doc_type(rtg),
        software_version=str(attr.get("version") or "").strip(),
    ), code


_ARXIV_CACHE = {}


def fetch_arxiv(arxiv_id, timeout):
    """Resolve an arXiv id to a normalized record (incl. the published-version
    DOI / journal_ref when arXiv has them), or None. Memoized per run: the same
    id serves the record, the published-version check and the abstract fallback,
    so it is fetched only once."""
    if arxiv_id not in _ARXIV_CACHE:
        _ARXIV_CACHE[arxiv_id] = _fetch_arxiv(arxiv_id, timeout)
    return _ARXIV_CACHE[arxiv_id]


def _parse_arxiv_entry(entry_xml):
    """Parse one Atom <entry> block from the arXiv API into a Record. Returns None if
    it has neither a title nor authors. Shared by the id fetch and the title search."""
    title_m = re.search(r"<title>(.*?)</title>", entry_xml, re.S)
    summary_m = re.search(r"<summary>(.*?)</summary>", entry_xml, re.S)
    authors = re.findall(r"<author>\s*<name>(.*?)</name>", entry_xml, re.S)
    year_m = re.search(r"<published>(\d{4})", entry_xml)
    # <updated> is the date of the LATEST version (vN); <published> is v1. When a
    # later version appeared in a different year, the bib may cite either -- so the
    # version year span lets the comparison treat a bib year that matches ANY
    # version as a 'pin the version', not a wrong year.
    updated_m = re.search(r"<updated>(\d{4})", entry_xml)
    # arXiv records the published version once it is linked: a DOI in
    # <arxiv:doi> and/or a citation string in <arxiv:journal_ref>.
    pub_doi_m = re.search(r"<arxiv:doi[^>]*>(.*?)</arxiv:doi>", entry_xml, re.S)
    jref_m = re.search(r"<arxiv:journal_ref[^>]*>(.*?)</arxiv:journal_ref>", entry_xml, re.S)
    if not title_m and not authors:
        return None
    arx_surnames = [a.split()[-1] for a in authors if a.split()]
    return Record(
        authors=[fold_surname(s) for s in arx_surnames],
        authors_display=arx_surnames,
        year=int(year_m.group(1)) if year_m else None,
        updated_year=int(updated_m.group(1)) if updated_m else None,
        title=re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "",
        journal="arXiv",
        abstract=re.sub(r"\s+", " ", summary_m.group(1)).strip() if summary_m else "",
        published_doi=pub_doi_m.group(1).strip() if pub_doi_m else "",
        journal_ref=re.sub(r"\s+", " ", jref_m.group(1)).strip() if jref_m else "",
    )


# arXiv id as it appears in an <id> URL, e.g. http://arxiv.org/abs/2210.03347v2.
_ARXIV_ID_IN_URL = re.compile(r"arxiv\.org/abs/([\w.\-/]+?)(v\d+)?$", re.I)


def search_arxiv(title, timeout):
    """Search arXiv by title and return [(arxiv_id, Record), ...] for the top hits,
    so a missing-id entry can be resolved when the work lives on arXiv (common for
    ML/physics). Recall is bounded by arXiv's title index; the CALLER is responsible
    for confirming each candidate against the bib (title + first author) before
    trusting it -- this just returns candidates."""
    # arXiv's query parser wants 'ti:word+word' -- field-prefixed, words joined by
    # '+', and NOT percent-quoted (an encoded ':' or space breaks the search). Reduce
    # the title to bare word tokens so punctuation (e.g. the ':' in 'Pix2Struct:')
    # cannot be mistaken for a field separator.
    words = re.findall(r"[A-Za-z0-9]+", clean_tex(title))
    if len(words) < 3:
        return []
    q = "ti:" + "+".join(words)
    txt = http_get_text(endpoint("arxiv_search", query=q), timeout)
    if not txt:
        return []
    out = []
    for entry_xml in re.findall(r"<entry>(.*?)</entry>", txt, re.S):
        id_m = re.search(r"<id>\s*(\S+?)\s*</id>", entry_xml)
        rec = _parse_arxiv_entry(entry_xml)
        if not id_m or rec is None:
            continue
        m = _ARXIV_ID_IN_URL.search(id_m.group(1))
        if m:
            out.append((m.group(1), rec))
    return out


def _fetch_arxiv(arxiv_id, timeout):
    """Uncached arXiv fetch. arXiv throttles rapid requests, so a single timeout
    is retried once before giving up."""
    url = endpoint("arxiv", id=arxiv_id)
    txt = http_get_text(url, timeout)
    if not txt:
        time.sleep(3)
        txt = http_get_text(url, timeout)
    if not txt:
        return None
    entry_m = re.search(r"<entry>(.*?)</entry>", txt, re.S)
    return _parse_arxiv_entry(entry_m.group(1) if entry_m else txt)


def fetch_openalex(doi, timeout):
    """OpenAlex work record: carries `is_retracted` (from Retraction Watch) and
    an inverted-index abstract."""
    if not doi:
        return None
    data, code = http_get_json(endpoint("openalex_work", doi=doi), timeout)
    if code != 200 or not data:
        return None
    inv = data.get("abstract_inverted_index") or {}
    abstract = ""
    if inv:
        positions = sorted((i, w) for w, idxs in inv.items() for i in idxs)
        abstract = strip_tags(" ".join(w for _, w in positions))
    return {"is_retracted": bool(data.get("is_retracted")), "abstract": abstract}


def fetch_abstract_s2(doi, timeout):
    """Abstract for a DOI from Semantic Scholar (fallback source), or ''."""
    if not doi:
        return ""
    data, code = http_get_json(endpoint("semanticscholar_paper", doi=doi), timeout)
    return strip_tags(data.get("abstract")) if code == 200 and data else ""


def fetch_inspire(doi=None, arxiv_id=None, recid=None, timeout=20):
    """Resolve a physics reference against INSPIRE-HEP by DOI, arXiv id, or INSPIRE
    record id (the 'literature/<recid>' in an inspirehep.net URL). Returns a
    normalized record (same shape as fetch_crossref) or None. INSPIRE is the
    authoritative database for high-energy/condensed-matter physics: a second source
    for cross-source consistency, and the only one that resolves a thesis/proceedings
    cited by its INSPIRE recid alone (no DOI/arXiv id)."""
    if doi:
        url = endpoint("inspire_doi", doi=doi)
    elif arxiv_id:
        url = endpoint("inspire_arxiv", id=arxiv_id)
    elif recid:
        url = endpoint("inspire_recid", recid=recid)
    else:
        return None
    data, code = http_get_json(url, timeout)
    if code != 200 or not data:
        return None
    md = data.get("metadata", {}) if isinstance(data, dict) else {}
    if not md:
        return None
    authors, authors_display, given = [], [], {}
    for a in (md.get("authors") or []):
        # INSPIRE stores 'full_name' as 'Last, First'.
        full = a.get("full_name") or a.get("name") or ""
        raw = clean_tex(full.split(",")[0] if "," in full
                        else full.split()[-1] if full.split() else "").strip()
        surname = fold_surname(raw)
        if not surname:
            continue
        authors.append(surname)
        authors_display.append(raw)
        if "," in full:
            g = clean_tex(full.split(",", 1)[1]).strip().split()
            if g:
                given[surname] = g[0]
    pub = (md.get("publication_info") or [{}])[0]
    year = pub.get("year") or (md.get("earliest_date") or "")[:4]
    titles = md.get("titles") or [{}]
    doc_types = md.get("document_type") or []
    return Record(
        document_type=(doc_types[0] if doc_types else ""),
        authors=authors,
        authors_display=authors_display,
        given=given,
        year=int(year) if str(year).isdigit() else None,
        volume=str(pub.get("journal_volume", "") or ""),
        number=str(pub.get("journal_issue", "") or ""),
        pages=str(pub.get("page_start", "") or ""),
        title=(titles[0].get("title") or "") if titles else "",
        journal=pub.get("journal_title", "") or "",
        abstract=strip_tags((md.get("abstracts") or [{}])[0].get("value", "")),
    )


def fetch_isbn(isbn, timeout):
    """Resolve an ISBN to book metadata via Open Library, falling back to Google
    Books. Returns a normalized record (title/authors/year/journal=publisher) or
    None. Establishes that a @book actually exists and lets its title be compared."""
    digits = re.sub(r"[\s-]", "", isbn)
    data, code = http_get_json(endpoint("openlibrary_isbn", isbn=digits), timeout)
    if code == 200 and data and data.get("title"):
        year = ""
        m = re.search(r"\d{4}", str(data.get("publish_date", "")))
        if m:
            year = m.group(0)
        # Open Library returns author refs, not names, on the edition record; the
        # title + year are enough to confirm existence without a second call.
        return Record(year=int(year) if year else None,
                      title=data.get("title", ""),
                      journal=(data.get("publishers") or [""])[0] if data.get("publishers") else "")
    gb, code = http_get_json(endpoint("googlebooks_isbn", isbn=digits), timeout)
    items = (gb or {}).get("items") or []
    if code == 200 and items:
        v = items[0].get("volumeInfo", {})
        gb_surnames = [a.split()[-1] for a in v.get("authors", []) if a.split()]
        year = (v.get("publishedDate", "") or "")[:4]
        return Record(authors=[fold_surname(s) for s in gb_surnames],
                      authors_display=gb_surnames,
                      year=int(year) if year.isdigit() else None,
                      title=v.get("title", ""), journal=v.get("publisher", ""))
    return None


# --- related works (erratum / correction / comment / reply) ----------------

# Leading title phrases that mark a related work, mapped to a relationship label.
# Grounded in real Crossref titles across APS, Nature/Springer, Elsevier, ACS,
# AMS, and generic forms. Order matters: more specific patterns first.
_RELATED_TITLE_RULES = [
    (re.compile(r"^\s*(author|publisher)\s+correction\b", re.I), "correction"),
    (re.compile(r"^\s*publisher['’]?s\s+note\b", re.I), "publisher-note"),
    (re.compile(r"^\s*(erratum|corrigend\w+)\b", re.I), "erratum"),
    (re.compile(r"^\s*correction\b", re.I), "correction"),
    (re.compile(r"^\s*addendum\b", re.I), "addendum"),
    (re.compile(r"^\s*retraction\b", re.I), "retraction"),
    (re.compile(r"^\s*reply\s+to\s+comment\b", re.I), "reply"),
    (re.compile(r"^\s*(reply|response)\s+to\b", re.I), "reply"),
    (re.compile(r"^\s*comment\s+on\b", re.I), "comment"),
]


def _title_words(t):
    return set(re.sub(r"[^a-z0-9 ]", " ", clean_tex(t).lower()).split())


def _classify_related_title(title):
    for pat, label in _RELATED_TITLE_RULES:
        if pat.match(title):
            return label
    return None


def fetch_related(doi, title, timeout, relations=None):
    """Works related to this entry by an erratum/correction/comment/reply
    relationship. Returns (relationship, doi, title) tuples. Two methods:

    1. Crossref `relation`/`updated-by` -- only when the publisher deposited the
       machine-readable link (many, e.g. APS, do not). These are passed in as
       `relations` (already parsed from the work record fetch_crossref made), so
       this function does NOT re-fetch the work.
    2. A title search -- a related work's title embeds the original ('Erratum:
       <title> [...]', 'Comment on <title>', 'Reply to ...'), which is how most
       publishers record the link. We search Crossref by the entry's title and
       keep results whose title starts with a relationship phrase and shares most
       words with the entry.

    Coverage is best-effort: recall depends on the publisher depositing the link
    or the related work ranking in the title-search results."""
    found = [(label, target, "") for label, target in (relations or [])]

    base_words = _title_words(strip_math(title))
    if len(base_words) >= 4:   # too-short titles give noisy searches
        res, code = http_get_json(
            endpoint("crossref_search", query=clean_tex(title)), timeout)
        items = (res or {}).get("message", {})
        items = items.get("items", []) if isinstance(items, dict) else (items or [])
        for w in items:
            ct = (w.get("title") or [""])[0]
            label = _classify_related_title(ct)
            if not label or not w.get("DOI"):
                continue
            # the related work embeds the original title; require strong overlap
            body = re.sub(r"\[.*?\]", "", re.sub(r"^[^:]*:", "", ct))
            if len(_title_words(body) & base_words) >= 0.6 * len(base_words):
                found.append((label, w["DOI"], ct))

    seen, out = set(), []
    for label, target, ct in found:
        if target and target.lower() != (doi or "").lower() and target not in seen:
            seen.add(target)
            out.append((label, target, ct))
    return out
