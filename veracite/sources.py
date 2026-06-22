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
            rels.append((kind, upd.get("id", "")))
    return rels


# --- record fetchers -------------------------------------------------------

def fetch_crossref(doi, timeout):
    """Resolve a DOI to a normalized record via Crossref. Returns (record, code);
    record is None on failure, with the HTTP status in `code`. The record also
    carries any related-work links from the same response (see `relations`), so the
    related-works check reuses this fetch instead of querying the work again."""
    data, code = http_get_json(endpoint("crossref_work", doi=doi), timeout)
    if code != 200 or not data:
        return None, code
    msg = data.get("message", {})
    authors, given = [], {}
    for a in (msg.get("author") or []):
        surname = fold_surname(a.get("family") or a.get("name") or "")
        if not surname:
            continue
        authors.append(surname)
        # keep the first given-name token for given-name verification; Crossref
        # carries structured names, unlike arXiv's last-token-only folding.
        g = clean_tex(a.get("given") or "").strip().split()
        if g:
            given[surname] = g[0]
    year = None
    for k in ("published-print", "published-online", "issued", "published"):
        parts = msg.get(k, {}).get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    return Record(
        authors=authors,
        given=given,
        year=year,
        volume=str(msg.get("volume", "") or ""),
        number=str(msg.get("issue", "") or ""),
        pages=str(msg.get("page", "") or ""),
        title=(msg.get("title") or [""])[0],
        journal=(msg.get("container-title") or [""])[0],
        abstract=strip_tags(msg.get("abstract", "")),
        relations=_extract_relations(msg),
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
    title_m = re.search(r"<entry>.*?<title>(.*?)</title>", txt, re.S)
    summary_m = re.search(r"<summary>(.*?)</summary>", txt, re.S)
    authors = re.findall(r"<author>\s*<name>(.*?)</name>", txt, re.S)
    year_m = re.search(r"<published>(\d{4})", txt)
    # arXiv records the published version once it is linked: a DOI in
    # <arxiv:doi> and/or a citation string in <arxiv:journal_ref>.
    pub_doi_m = re.search(r"<arxiv:doi[^>]*>(.*?)</arxiv:doi>", txt, re.S)
    jref_m = re.search(r"<arxiv:journal_ref[^>]*>(.*?)</arxiv:journal_ref>", txt, re.S)
    if not title_m and not authors:
        return None
    return Record(
        authors=[fold_surname(a.split()[-1]) for a in authors if a.split()],
        year=int(year_m.group(1)) if year_m else None,
        title=re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "",
        journal="arXiv",
        abstract=re.sub(r"\s+", " ", summary_m.group(1)).strip() if summary_m else "",
        published_doi=pub_doi_m.group(1).strip() if pub_doi_m else "",
        journal_ref=re.sub(r"\s+", " ", jref_m.group(1)).strip() if jref_m else "",
    )


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


def fetch_inspire(doi=None, arxiv_id=None, timeout=20):
    """Resolve a physics reference against INSPIRE-HEP by DOI or arXiv id. Returns
    a normalized record (same shape as fetch_crossref) or None. INSPIRE is the
    authoritative database for high-energy/condensed-matter physics, used as a
    second authoritative source for cross-source consistency."""
    if doi:
        url = endpoint("inspire_doi", doi=doi)
    elif arxiv_id:
        url = endpoint("inspire_arxiv", id=arxiv_id)
    else:
        return None
    data, code = http_get_json(url, timeout)
    if code != 200 or not data:
        return None
    md = data.get("metadata", {}) if isinstance(data, dict) else {}
    if not md:
        return None
    authors, given = [], {}
    for a in (md.get("authors") or []):
        # INSPIRE stores 'full_name' as 'Last, First'.
        full = a.get("full_name") or a.get("name") or ""
        surname = fold_surname(full.split(",")[0] if "," in full else full.split()[-1] if full.split() else "")
        if not surname:
            continue
        authors.append(surname)
        if "," in full:
            g = clean_tex(full.split(",", 1)[1]).strip().split()
            if g:
                given[surname] = g[0]
    pub = (md.get("publication_info") or [{}])[0]
    year = pub.get("year") or (md.get("earliest_date") or "")[:4]
    titles = md.get("titles") or [{}]
    return Record(
        authors=authors,
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
        authors = [fold_surname(a.split()[-1]) for a in v.get("authors", []) if a.split()]
        year = (v.get("publishedDate", "") or "")[:4]
        return Record(authors=authors, year=int(year) if year.isdigit() else None,
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
