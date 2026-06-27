"""Per-entry analysis pipeline: the one path that resolves an entry online and
runs the verification + optional LLM layers over it.

The CLI loop and the tests both drive `analyze_entry`, so there is a single
orchestration of the online layers (record/status -> PID coverage -> verification
status -> LLM relevance) rather than a separate shadow path. The CLI owns only
the surrounding presentation (static rules, skip/emit, progress printing).
"""

from .llm import rate_one
from .record import resolve_entry
from .report import Severity
from .verify import classify, pid_check


def analyze_entry(e, res_store, status_store, rep, *, delay, timeout,
                  provider=None, model=None, contexts=None, by_key=None):
    """Run the online layers for one entry and record its results.

    Resolves the entry against its identifiers (record/status/cross-source), checks
    persistent-identifier coverage, assigns a verification status, and -- when a
    `provider` and in-text `contexts` are supplied -- rates its relevance. Mutates
    `res_store[e.key]` (a Resolution) and `status_store[e.key]` ((status, conf)).
    Returns the Resolution."""
    res = resolve_entry(e, rep, delay, timeout)
    res_store[e.key] = res
    pid_check(e, res, rep, delay, timeout, offline=False)            # Layer 5
    # The 'no id to verify against' note was deferred so pid_check's DOI search
    # could resolve the entry first; emit it only if it is still unresolved AND
    # pid_check did not already warn 'no PID' for it -- those two findings share one
    # root cause (the entry has no identifier) and one fix (add a DOI/ISBN), so emitting
    # both is redundant. pid_missing is the more specific, actionable message, so it
    # wins; record_unresolved still stands alone (a dead/unresolvable id, no pid_missing).
    if res.no_id and res.record is None and not res.pid_missing:
        rep.add(Severity.INFO, e, "no DOI/arXiv id to verify against", "record",
                category="record_unresolved")
    status_store[e.key] = classify(e, res, rep)                     # Layer 3
    if res.record is not None and provider is not None \
            and contexts is not None and e.key in contexts:
        rate_one(e, res.record, contexts[e.key], rep, provider, model, by_key)
    return res
