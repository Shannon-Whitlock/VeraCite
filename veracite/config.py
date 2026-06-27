"""Configuration: defaults, settings-file loading, and API endpoint building.

Settings are read from the first available file (see SETTINGS_PATHS or an
explicit path), then environment overrides, falling back to DEFAULT_SETTINGS so
the tool runs with no configuration. Nothing here carries personal data.
"""

import json
import os
import sys

try:
    import requests
    HTTP_BACKEND = "requests"
except ImportError:  # pragma: no cover
    from urllib.parse import quote as _urlquote
    HTTP_BACKEND = "urllib"

VERSION = "0.1.2"

# Fallbacks used when no settings file or environment override is present.
DEFAULT_SETTINGS = {
    # Optional contact appended to the User-Agent. Crossref/OpenAlex give the
    # "polite pool" (more reliable service) to requests that identify a mailto.
    # Set your own address here or in the settings file; left blank by default.
    "contact_email": "",
    # Default provider for the optional --llm relevance sweep.
    "llm_provider": "claude",
    # Per-provider model ids. Override in the settings file to use another model.
    "llm_models": {"claude": "claude-haiku-4-5-20251001"},
    # Short description of the document, used to ground the LLM's judgement.
    # Generic by default; set it to your paper's topic for sharper ratings.
    "document_context": "a research paper",
    # Network pacing defaults (a CLI flag, when given, overrides these).
    "request_delay": 0.2,     # min seconds between requests to the SAME service
                              # (per-host, time-based; arXiv is paced at 3s, see http.py)
    "request_timeout": 20,    # per-request HTTP timeout, seconds
    # Project-specific proper nouns / package names that must stay capitalized
    # in titles; an unprotected occurrence is flagged. Extend per project.
    "protected_terms": [
        "Rydberg", "Julia", "Python", "QuTiP", "QuantumOptics", "ARC", "MQT",
        "Yb", "Rb", "Cs", "Sr", "AtomECS", "PyLCP", "Pulser", "Bloqade", "Atomique",
    ],
    # Severity per finding category: "error", "warning", or "note". Lets you
    # re-rank a whole class of findings without touching code. Categories not
    # listed here keep the severity the individual check assigns. Defaults put
    # findings that change which work you cite (retracted, superseded preprint,
    # wrong paper) above cosmetic/style ones (biblatex fields, encoding).
    # 'author_format' is deliberately ABSENT: its checks emit mixed severities (a
    # note for ALL-CAPS surnames, a warning for an 'and' glued to a name), and
    # pinning a category here flattens all its findings to one level. Leaving it
    # unlisted lets each check keep its own severity. The catalog (--list-rules)
    # still documents it via CATEGORY_DOC. (Kept in sync with
    # catalog.INTENTIONALLY_UNPINNED, enforced by a test.)
    "severity": {
        "syntax": "error",                # structural / does-not-parse BibTeX error
        "missing_entry_header": "error",  # an entry's '@type{key,' header line is missing
        "dead_doi": "error",              # the recorded DOI does not resolve (Crossref 404)
        "retraction": "error",            # cited work is retracted
        "wrong_paper": "warning",         # LLM opinion (abstract-only) -- verify, never an error (no model gates CI)
        "id_resolves_wrong_record": "error",  # doi/arXiv id resolves to a different paper
        "metadata_mismatch": "warning",   # author/title/year/vol/pages/journal differ from record
        "record_unresolved": "warning",   # no authoritative source returned a record for the id
        "author_completeness": "warning", # malformed truncation: a literal 'et al.' / bare 'al.'
        "author_truncated_marker": "note", # valid 'and others' marker; dropped names not stored
        "source_conflict": "warning",     # two authoritative sources disagree on data
        "doi_available": "warning",       # a DOI exists in Crossref but the entry omits it
        "pid_missing": "warning",         # no persistent identifier where one is expected
        "identifier_format": "warning",   # malformed DOI/arXiv/ISBN/ISSN/ORCID
        "llm_relevance": "warning",       # LLM rated the citation weakly relevant
        "llm_ok": "note",                 # LLM rated the citation relevant (4-5/5) -- clean-pass note
        "llm_unavailable": "note",        # LLM could not rate (no abstract / provider error) -- not actionable
        "llm_config": "warning",          # LLM run misconfigured (e.g. unknown provider)
        "preprint_superseded": "warning", # a published version now exists
        "preprint_version": "note",       # bib year matches an earlier arXiv version
        "related_work": "warning",        # erratum/correction/comment/reply linked
        "duplicate": "error",             # duplicate citation key or DOI (two entries collide)
        "duplicate_field": "note",        # a field repeated within ONE entry, values agree (benign)
        "duplicate_field_conflict": "warning",  # repeated field with DIFFERING values (data silently dropped)
        "dropped_field": "warning",       # a field outside the entry, silently dropped
        "misplaced_field": "warning",     # a value in the wrong field (e.g. a year in 'journal')
        "missing_field": "error",         # biber-mandatory field absent (e.g. title, journal)
        "missing_locator": "note",        # omits volume/pages -- NOT mandatory for @article (advisory)
        "identifier_placement": "note",   # an id is in the url, not a structured doi/eprint field
        "entrytype_suggestion": "warning",# the @type looks wrong for the entry's data
        "datamodel_recommended": "note",  # mandatory in biblatex's datamodel but biber tolerates absent
        "missing_recommended": "warning", # field biber doesn't require but we advise (year)
        "biblatex_validity": "note",      # field invalid under biblatex datamodel
        "title_case": "note",             # title looks miscased (mostly UPPERCASE)
        "title_style": "note",            # title matches record but punctuation/wording deviates
        "style": "note",                  # casing, punctuation, dashes, month, etc.
        "citation_order": "note",         # a \cite{} group is not in chronological order
        "encoding": "note",               # non-ASCII / mojibake
        "journal_macro": "note",          # journal is an unexpanded LaTeX macro (\pra)
        "container_granularity": "note",  # id resolved to the containing volume, not the item
        "parity_suggestion": "note",      # record has data the bib could adopt
    },
    # External API endpoints. Centralized so they can be repointed from the
    # settings file if a service moves, without editing the code. {id}, {doi}
    # and {query} are substituted per request.
    "endpoints": {
        "crossref_work": "https://api.crossref.org/works/{doi}",
        "crossref_search": "https://api.crossref.org/works?query.bibliographic={query}&rows=12&select=DOI,title,author,issued,container-title,type",
        "arxiv": "http://export.arxiv.org/api/query?id_list={id}",
        "arxiv_search": "http://export.arxiv.org/api/query?search_query={query}&max_results=5",
        "openalex_work": "https://api.openalex.org/works/https://doi.org/{doi}",
        "semanticscholar_paper": "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract",
        "datacite_doi": "https://api.datacite.org/dois/{doi}",
        "inspire_doi": "https://inspirehep.net/api/doi/{doi}",
        "inspire_arxiv": "https://inspirehep.net/api/arxiv/{id}",
        "inspire_recid": "https://inspirehep.net/api/literature/{recid}",
        "openlibrary_isbn": "https://openlibrary.org/isbn/{isbn}.json",
        "googlebooks_isbn": "https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}",
    },
}

SETTINGS = dict(DEFAULT_SETTINGS)

# Searched in order; the first that exists is used. None is committed, so the
# tool ships with no personal data.
SETTINGS_PATHS = [
    "veracite.json",
    os.path.expanduser("~/.config/veracite/settings.json"),
    os.path.expanduser("~/.veracite.json"),
]

CONTACT_ENV = "VERACITE_CONTACT_EMAIL"


def load_settings(explicit_path=None):
    """Populate SETTINGS from the first available settings file, then from
    environment overrides. Missing keys keep their DEFAULT_SETTINGS value."""
    SETTINGS.clear()
    SETTINGS.update(DEFAULT_SETTINGS)
    for path in ([explicit_path] if explicit_path else []) + SETTINGS_PATHS:
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    SETTINGS.update(json.load(fh))
            except (OSError, json.JSONDecodeError) as ex:
                print(f"warning: could not read settings {path}: {ex}", file=sys.stderr)
            break
    if os.environ.get(CONTACT_ENV):
        SETTINGS["contact_email"] = os.environ[CONTACT_ENV]
    return SETTINGS


def user_agent():
    """User-Agent header, with an optional contact mailto if one is configured."""
    email = SETTINGS.get("contact_email", "")
    return {"User-Agent": f"veracite/{VERSION}"
            + (f" (mailto:{email})" if email else "")}


def url_quote(value, safe="/"):
    """URL-escape a value. `safe` keeps the given characters unescaped; the
    default keeps '/' so DOIs and arXiv ids stay intact in path segments."""
    quote = requests.utils.quote if HTTP_BACKEND == "requests" else _urlquote
    return quote(str(value), safe=safe)


def endpoint(name, **params):
    """Build an API URL from the configured endpoint template. A `query` value
    is fully escaped (free-text search term); all other values keep '/' so DOIs
    and arXiv ids survive in path segments. Falls back to the default template
    when the settings file overrides `endpoints` only partially.

    Exception: arXiv's search wants a fielded query `ti:word+word` with the ':' and
    '+' kept LITERAL -- percent-encoding them ('ti%3A...%2B...') makes arXiv's parser
    return zero results. So for the arxiv_search endpoint the query keeps ':+' safe.
    (arxiv_search builds its own query from bare word tokens, so nothing else in it
    needs escaping.)"""
    template = SETTINGS.get("endpoints", {}).get(name) \
        or DEFAULT_SETTINGS["endpoints"][name]
    query_safe = ":+" if name == "arxiv_search" else ""
    escaped = {k: url_quote(v, safe=query_safe if k == "query" else "/")
               for k, v in params.items()}
    return template.format(**escaped)
