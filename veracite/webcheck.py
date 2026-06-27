"""A single callable for the online, no-LLM check -- the path the web demo drives.

This factors the CLI's bibliography-only online run (no `--tex`, no `--llm`, no
checkpointing) into one importable function, so the web front end and the CLI share
exactly one orchestration of the layers (static -> record/status -> verification ->
integrity). It returns the same machine-readable payload as `Report.to_json`
(`findings`, `summary`, `references`) wrapped in a small envelope.

It is deliberately self-contained and stdlib-only at the edges so a CGI script on a
shared host can `import veracite` and call it with no extra dependencies.

`fast=True` (the web default) suppresses only the sources that are both SLOW and
low-value for a quick demo, while keeping the ones that pull their weight. Measured
per-call latencies drove the split:

  KEPT (fast, high-value, or only-when-needed):
    * Crossref / arXiv id lookup -- sub-second; the core resolution.
    * OpenAlex (~0.5-2 s) -- adds RETRACTION detection, a real error worth catching;
      runs only for an entry that has a DOI.
    * OpenLibrary/ISBN (~2-11 s, but only for @book entries with an ISBN, which are
      rare in a typical bibliography) -- the only way a book verifies.
    * Crossref/arXiv TITLE SEARCH -- the fallback that recovers a missing OR dead DOI.
      It runs on a NEED-TO basis only: pid_check invokes it solely for an entry with
      no usable DOI (none recorded, or the recorded one returned 404), so a clean-DOI
      entry never pays for it. Worth keeping because "found the real DOI" is exactly
      what a user expects after a dead-DOI error.

  SUPPRESSED (slow and/or useless without the LLM):
    * INSPIRE-HEP -- ~10 s EVERY call and effectively always times out for our
      queries (no usable record returned); pure dead weight.
    * Crossref related-works (errata) lookup -- a ~7 s title search, miss-heavy.
    * Semantic Scholar abstract -- only feeds the LLM relevance sweep, which the demo
      never runs, so it is cost with no benefit here.

  All KEPT-but-slower sources (OpenAlex, ISBN, the title searches) are bounded by a
  short AUX_TIMEOUT, so one slow host abandons just its own check rather than dragging
  the request toward the CGI time limit.

Without this split a 5-entry bibliography that fans out to every source took over a
minute -- past the ~120 s hard limit a CGI request gets on shared hosting (the 504
the demo hit). With it, a typical run is a few seconds. The kept auxiliary sources
are additionally bounded by `aux_timeout` (see below) so one slow host can't drag
the whole request: a slow OpenAlex/ISBN call is abandoned and that one check simply
does not run, rather than blowing the time budget.
"""

from .config import SETTINGS, VERSION
from .parser import parse_bib
from .pipeline import analyze_entry
from .report import Report, Severity
from .rules import run_entry_rules, run_file_rules, syntax_pass

# The public demo bounds work per request: at most this many entries are checked
# online (the rest are dropped and flagged via the `truncated` envelope field).
DEFAULT_MAX_ENTRIES = 10

# Per-request HTTP timeout for the kept core sources (Crossref/arXiv id lookups),
# which answer in well under a second -- so a longer wait only ever means a hung host
# we would rather report as unreachable.
DEFAULT_WEB_TIMEOUT = 10

# Per-call timeout for the KEPT-but-slower auxiliary sources (OpenAlex, OpenLibrary,
# and the no-identifier/dead-DOI title search). It must be generous enough to let a
# real call SUCCEED -- the Crossref bibliographic title search legitimately takes ~7 s,
# so a tighter cap would kill the very DOI recovery we want. The safety margin is the
# OVERALL budget, not this single cap: these run on a need-to basis (only for an entry
# with no usable DOI), so even a worst case of ~10 such entries x ~10 s stays under the
# ~120 s CGI ceiling, while a typical bibliography triggers only a few.
AUX_TIMEOUT = 10


def _fast_source_patches(aux_timeout):
    """The (module, attribute, replacement) patches that put the online layer into
    fast/web mode: no-op the slow, low-value sources, and force a short timeout on the
    kept-but-slow ones. Applied for one call and restored after (see check_bib_text).

    The kept core sources (fetch_crossref, fetch_arxiv) are untouched. OpenAlex and
    ISBN are wrapped to clamp their per-call timeout to `aux_timeout`, so retraction
    and book checks still run when the host is responsive but cannot blow the budget.
    """
    from . import record, verify

    def capped(fn):
        # The fetchers take timeout as the LAST positional arg; clamp it down.
        def inner(*args, **kwargs):
            if "timeout" in kwargs:
                kwargs["timeout"] = min(kwargs["timeout"], aux_timeout)
                return fn(*args, **kwargs)
            if args:
                args = args[:-1] + (min(args[-1], aux_timeout),)
            return fn(*args, **kwargs)
        return inner

    return [
        # Suppressed entirely: slow and/or no value without the LLM.
        (record, "fetch_inspire", lambda *a, **k: None),
        (record, "fetch_related", lambda *a, **k: []),
        (record, "fetch_abstract_s2", lambda *a, **k: ""),
        # Kept but time-capped: retraction (OpenAlex), book/ISBN resolution, and the
        # no-identifier / dead-DOI title search that recovers a missing or wrong DOI.
        # The searches run on a need-to basis only -- pid_check invokes them solely for
        # an entry that has no usable DOI (none recorded, or the recorded one is dead),
        # so a clean-DOI entry never pays for them.
        (verify, "_search_doi", capped(verify._search_doi)),
        (verify, "_search_arxiv_id", capped(verify._search_arxiv_id)),
        (record, "fetch_openalex", capped(record.fetch_openalex)),
        (record, "fetch_isbn", capped(record.fetch_isbn)),
    ]


def check_bib_text(raw, *, max_entries=DEFAULT_MAX_ENTRIES, timeout=None, fast=True):
    """Run the online check (no LLM) over a .bib given as text.

    Mirrors the CLI's bibliography-only online path: parse, syntax pass, then per
    entry run the offline static rules and the online record/status/verification
    layers (`provider=None` keeps the LLM sweep off), then file-wide rules and the
    integrity roll-up. Returns a JSON-serializable dict:

        {"veracite_version", "n_entries", "truncated", "max_entries", "fast",
         "findings": [...], "summary": {...}, "references": [...]}

    `n_entries` is the parse count before capping; `truncated` is True when more
    than `max_entries` were present (only the first `max_entries` are checked).
    `fast` (default True) limits the online layer to Crossref + arXiv so the run
    finishes quickly enough for a CGI request (see the module docstring); pass
    `fast=False` for the full multi-source check.
    """
    delay = SETTINGS.get("request_delay", 0.2)
    if timeout is None:
        timeout = DEFAULT_WEB_TIMEOUT if fast else SETTINGS.get("request_timeout", 20)

    # In fast mode, retarget the source layer for this call only (suppress the slow
    # low-value sources, time-cap the kept-but-slow ones), restoring everything in
    # `finally` so the CLI/tests sharing this process are unaffected. `capped` wraps
    # the CURRENT fetcher, so this must read the live attribute at apply time.
    patches = _fast_source_patches(AUX_TIMEOUT) if fast else []
    saved = [(mod, attr, getattr(mod, attr)) for mod, attr, _ in patches]
    for mod, attr, fn in patches:
        setattr(mod, attr, fn)
    try:
        return _run(raw, max_entries, delay, timeout, fast)
    finally:
        for mod, attr, orig in saved:
            setattr(mod, attr, orig)


def _run(raw, max_entries, delay, timeout, fast):
    entries, problems = parse_bib(raw)
    n_entries = len(entries)
    truncated = n_entries > max_entries
    entries = entries[:max_entries]

    rep = Report(color=False)
    # File-level + structural findings; `broken` is the set of keys whose fields
    # parsed wrong, so the online comparison would only yield spurious mismatches.
    broken = syntax_pass(raw, entries, problems, rep)

    results, statuses = {}, {}          # key -> Resolution / (status, confidence)
    phases_by_key = {}                  # key -> set of phases computed
    by_key = {e.key: e for e in entries}

    for e in entries:
        run_entry_rules(e, rep)                       # offline static checks
        phases_by_key[e.key] = {"offline"}
        # A structurally broken entry parsed wrong; comparing its garbled fields
        # against a record yields false mismatches, so skip its online layer and
        # point at the syntax error as the fix (same stance as the CLI).
        if e.key in broken:
            rep.add(Severity.WARN, e, "has a structural syntax error (see above); "
                    "the rest of this entry's checks (record, status, cross-source) "
                    "are skipped until it parses cleanly", "syntax", category="syntax")
            continue
        # provider=None / contexts=None -> record/status/verification only, no LLM.
        analyze_entry(e, results, statuses, rep, delay=delay, timeout=timeout,
                      provider=None, contexts=None, by_key=by_key)
        phases_by_key[e.key].add("online")

    run_file_rules(entries, rep)
    # to_json builds the per-entry records and DERIVES the summary from them (the
    # single source of truth) plus the live file-level findings -- no separate
    # integrity() call, so the web payload matches the CLI's parse path exactly.
    out = rep.to_json(results=results, statuses=statuses,
                      phases_by_key=phases_by_key, entries=entries)
    out["veracite_version"] = VERSION
    out["n_entries"] = n_entries
    out["truncated"] = truncated
    out["max_entries"] = max_entries
    out["fast"] = fast
    return out
