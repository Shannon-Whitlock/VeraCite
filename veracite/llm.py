"""Optional LLM relevance/placement rating, and citation-context extraction.

The rating is off by default (enabled with --llm) and advisory only. A provider
is a callable (prompt, model, timeout) -> reply_text, or a falsy value / an
{"error": reason} dict on failure (the dict lets the cause, e.g. a retired model
id, reach the user). Register one in LLM_PROVIDERS to add a backend. Everything
else -- prompting, parsing, scoring, reporting -- is provider-agnostic.
"""

import bisect
import json
import os
import re
import shutil
import subprocess
import tempfile

from .config import SETTINGS
from .report import Severity

CITE_RE = re.compile(r"\\(?:cite|citep|citet|textcite|parencite|footcite)\*?"
                     r"(?:\[[^\]]*\])*\s*\{([^}]*)\}")

# A TeX macro parameter token (#1, #2, ...). Such a token appears as the argument
# of a \cite inside a \newcommand/\providecommand body (e.g.
# '\providecommand{\autocite}[1]{\cite{#1}}'); it is not a real citation key and
# must not be mined as one (it would otherwise be reported as a cited key with no
# bib entry).
_PARAM_TOKEN_RE = re.compile(r"^#\d+$")


def _cite_keys(group):
    """The real citation keys in one '\\cite{...}' argument: comma-split, trimmed,
    with empty tokens and TeX parameter tokens ('#1') dropped."""
    return [k for k in (k.strip() for k in group.split(",")) if k and not _PARAM_TOKEN_RE.match(k)]


# --- citation-context extraction (from the .tex sources) -------------------

def gather_tex_paths(targets):
    """Expand file/directory targets to a sorted list of .tex paths (recursive)."""
    paths = []
    for t in targets:
        t = os.path.abspath(os.path.expanduser(t))
        if os.path.isdir(t):
            for root, _dirs, names in os.walk(t):
                # Case-insensitive so a '.TEX' on Windows/macOS is still found.
                paths += [os.path.join(root, n) for n in names
                          if n.lower().endswith(".tex")]
        elif os.path.isfile(t):
            paths.append(t)
    return sorted(set(paths))


def collect_tex(targets):
    """Return [(path, text)] for every .tex file under the given targets."""
    out = []
    for p in gather_tex_paths(targets):
        try:
            with open(p, encoding="utf-8") as fh:
                out.append((p, fh.read()))
        except OSError:
            pass
    return out


# Candidate sentence end: '.', '!' or '?' (+ optional close bracket) then space
# and a capital/opening. Abbreviations and initials are rejected afterwards
# (Python re has no variable-width lookbehind). Used to trim the LLM context to
# the sentence(s) immediately around a citation.
_SENT_END = re.compile(r"[.!?]+[)\]]?\s+(?=[A-Z(\\])")
_ABBREVS = {"e.g.", "i.e.", "cf.", "al.", "fig.", "eq.", "ref.", "refs.", "vs.",
            "dr.", "mr.", "ms.", "prof.", "approx.", "no.", "vol.", "ca."}


# The token immediately before a candidate sentence-ender: the last run of
# non-space characters ending at the ender. Anchored to the END of the searched
# slice and matched against only a short look-back window, so this never scans
# (or backtracks across) the whole document.
_PREV_WORD = re.compile(r"(\S+)\s*$")
_INITIAL = re.compile(r"[a-z]\.")


def _is_real_boundary(text, end):
    """Whether the sentence-ender ending at index `end` is a true boundary and not
    an abbreviation ('e.g.') or a single-letter initial ('J.'). Only a bounded
    look-back window is inspected, so cost is independent of document length."""
    word = _PREV_WORD.search(text, max(0, end - 32), end)
    if not word:
        return True
    w = word.group(1).lower()
    if w in _ABBREVS:
        return False
    # A trailing single-letter initial like 'J.' (one letter + dot).
    return not _INITIAL.fullmatch(w)


def sentence_bounds(text):
    """Sorted sentence-boundary offsets for `text` (0 and len(text) included),
    computed ONCE per document. `_sentence_window` binary-searches this instead of
    re-scanning the whole text for every citation."""
    return [0] + [m.end() for m in _SENT_END.finditer(text)
                  if _is_real_boundary(text, m.start() + 1)] + [len(text)]


def _sentence_window(text, start, end, bounds, radius=1):
    """The sentence containing [start, end) plus `radius` sentences on each side,
    from `text`, using the precomputed `bounds`. Keeps only the immediate citation
    context (2-3 sentences) so the LLM sees the claim being supported, not a large
    slice of the manuscript."""
    lo = bisect.bisect_right(bounds, start) - 1     # last boundary at/before start
    hi = bisect.bisect_left(bounds, end)            # first boundary at/after end
    a = bounds[max(0, lo - radius)]
    b = bounds[min(len(bounds) - 1, hi + radius)]
    return re.sub(r"\s+", " ", text[a:b]).strip()


def find_citation_contexts(tex_files, base):
    """Map each cited key to [{file, context, group}] -- the 2-3 sentences around
    each \\cite (including the preceding sentence) plus the OTHER keys cited in the
    same \\cite{...} group. Used to flag uncited entries and to feed the LLM sweep,
    which judges a reference's fit relative to the works it is co-cited with so an
    inappropriate citation hidden in a group can be surfaced. Only this minimal
    window of the manuscript is ever sent to the LLM provider."""
    contexts = {}
    for path, text in tex_files:
        rel = os.path.relpath(path, base)
        bounds = sentence_bounds(text)              # computed once per file
        for m in CITE_RE.finditer(text):
            snippet = _sentence_window(text, m.start(), m.end(), bounds)
            keys = _cite_keys(m.group(1))
            for k in keys:
                siblings = [s for s in keys if s != k]
                contexts.setdefault(k, []).append(
                    {"file": rel, "context": snippet, "group": siblings})
    return contexts


def find_citation_groups(tex_files):
    """Every multi-key \\cite{...} group, as an ordered list of keys (the order the
    author wrote them). Used to advise when a group is not in chronological order.
    Deduplicated so an identical group cited repeatedly is reported once."""
    groups, seen = [], set()
    for _path, text in tex_files:
        for m in CITE_RE.finditer(text):
            keys = tuple(_cite_keys(m.group(1)))
            if len(keys) > 1 and keys not in seen:
                seen.add(keys)
                groups.append(list(keys))
    return groups


# --- providers -------------------------------------------------------------

def _provider_claude_cli(prompt, model, timeout):
    """Provider backed by the local `claude` CLI (uses existing Claude auth).

    Returns the reply text on success, or an {"error": reason} dict on failure so
    the cause -- a missing CLI, a timeout, or (commonly) a retired/unknown model id
    in the pinned default -- reaches the user instead of a bare "no response"."""
    # Resolve the executable so a Windows shim (claude.cmd / claude.exe) is found
    # too -- subprocess.run does not consult PATHEXT for a bare 'claude' on Windows.
    exe = shutil.which("claude")
    if not exe:
        return {"error": "claude CLI not found on PATH", "fatal": True}
    # Run from a neutral temp directory (portable; not the hardcoded POSIX '/tmp')
    # so the CLI does not pick up project-local config from the current directory.
    try:
        proc = subprocess.run(
            [exe, "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout,
            cwd=tempfile.gettempdir())
    except (FileNotFoundError, OSError) as ex:
        return {"error": f"could not run claude CLI: {ex}", "fatal": True}
    except subprocess.TimeoutExpired:
        return {"error": f"claude CLI timed out after {timeout}s"}
    # The CLI reports an error two ways: a non-zero exit, OR exit 0 with a JSON
    # body carrying "is_error": true (this is how "Not logged in" arrives -- the
    # process succeeds, the request did not). Detect both, and surface the CLI's
    # own human message (its JSON "result", e.g. 'Not logged in - Please run
    # /login') rather than a raw exit code or the whole JSON blob.
    payload = None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("is_error"):
        msg = str(payload.get("result") or "").strip() or "the CLI reported an error"
        return {"error": msg, "fatal": _is_auth_error(msg)}
    if proc.returncode != 0:
        # Surface the CLI's own message (e.g. an unknown/retired model id), trimmed
        # to one line so a stale pinned default is diagnosable, not a silent failure.
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit code {proc.returncode}"
        return {"error": f"claude CLI failed (model {model!r}): {reason}",
                "fatal": _is_auth_error(reason)}
    if isinstance(payload, dict):
        return payload.get("result", proc.stdout)
    return proc.stdout


def _is_auth_error(message):
    """Whether a provider error message means 'cannot authenticate' -- the user is
    not logged in or has no account/credentials. These are not worth retrying per
    entry: the run should stop and tell the user how to fix it once."""
    m = message.lower()
    return any(s in m for s in ("not logged in", "/login", "please run /login",
                                "log in", "unauthorized", "authentication",
                                "not authenticated", "no api key", "api key",
                                "invalid api key", "credit balance"))


# name -> provider callable. Add another LLM backend by registering it here.
LLM_PROVIDERS = {
    "claude": _provider_claude_cli,
}


# --- rating ----------------------------------------------------------------

def _group_titles(contexts, by_key, limit=8):
    """The titles of the references this entry is co-cited with (across all of its
    citation occurrences), so the model can judge whether it fits the group."""
    sibs = []
    for c in contexts:
        for s in c.get("group", []):
            if s not in sibs:
                sibs.append(s)
    lines = []
    for s in sibs[:limit]:
        e = by_key.get(s) if by_key else None
        title = (e.get("title") if e else "") or ""
        lines.append(f"  - {s}: {title[:90]}" if title else f"  - {s}")
    return "\n".join(lines)


def build_rating_prompt(entry, rec, contexts, by_key=None):
    """Prompt asking the model to rate one citation's relevance, flag a clear
    wrong paper, and -- when the reference is cited in a GROUP -- judge whether it
    fits among its co-cited references or is an odd one out. The preceding
    sentence is part of the supplied context window."""
    ctx_block = "\n".join(f"  [{i + 1}] ({c['file']}) ...{c['context']}..."
                          for i, c in enumerate(contexts[:3]))
    abstract = (rec.get("abstract") or "").strip()[:1800] or "(no abstract available)"
    document = SETTINGS.get("document_context") or "a research paper"
    group = _group_titles(contexts, by_key)
    group_block = (f"""
This reference is cited together with the following other references (judge \
whether it belongs in this group or is an odd one out -- a citation that does not \
fit a group its companions do fit is exactly what to catch):
{group}
""" if group else "")
    return f"""You are auditing the bibliography of {document}.

From the cited reference's abstract, the sentence(s) where the paper cites it \
(including the preceding sentence for context), and any co-cited references:
1. relevance: how appropriate is this reference for the claim it is cited for, \
considering the surrounding sentences and the other works it is grouped with?
   5 = clearly appropriate, 1 = clearly irrelevant/mismatched.
2. wrong_paper: true ONLY if the cited paper is clearly not the work the sentence \
is about -- a different paper, dataset, method or topic (e.g. the MATH dataset \
cited where the APPS benchmark is meant). Be conservative.
3. group_misfit: true ONLY if this reference is cited in a group AND it clearly \
does not fit the claim the group supports while its companions do (an off-topic \
citation hidden among relevant ones). Otherwise false.

Cited entry:
  key:     {entry.key}
  title:   {entry.get('title')}
  authors: {entry.get('author')[:200]}
  year:    {entry.get('year')}

Abstract of the referenced paper:
  {abstract}
{group_block}
Where the paper cites it:
{ctx_block or '  (no in-text citation found)'}

Reply with ONLY a JSON object, no prose, in exactly this form:
{{"relevance": <1-5>, "wrong_paper": <true|false>, "group_misfit": <true|false>, \
"verdict": "<=12 words", "issue": "<short note or empty string>"}}"""


def rate_citation(provider, model, prompt, timeout=120):
    """Run one rating prompt through `provider` and parse the JSON reply. A provider
    returns the reply text, or an {"error": reason} dict it wants surfaced (e.g. a
    retired model id), which is passed straight through."""
    reply = provider(prompt, model, timeout)
    if isinstance(reply, dict) and "error" in reply:
        return reply
    if not reply:
        return {"error": "no response from LLM provider"}
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return {"error": "no JSON in model reply"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"error": "could not parse model JSON"}


def resolve_provider(provider_name, rep):
    """Look up an LLM provider by name, reporting an error finding if unknown.
    Returns the provider callable or None."""
    provider = LLM_PROVIDERS.get(provider_name)
    if provider is None:
        rep.add_file(Severity.WARN, f"unknown LLM provider {provider_name!r}; "
                     f"known: {', '.join(sorted(LLM_PROVIDERS))}", "llm",
                     category="llm_config")
    return provider


def preflight_provider(provider, model, timeout=30):
    """Cheaply probe the provider once before the run, so a fatal setup problem --
    most commonly 'not logged in' / no account -- is reported up front instead of as
    a confusing per-entry warning repeated for every cited reference. Returns None
    when the provider looks usable, or a short, actionable error string when it is
    clearly unusable (a 'fatal' error: missing CLI, or an auth failure). A transient
    or ambiguous failure returns None so the run still proceeds and per-entry
    handling applies -- we only block on errors we are confident are fatal."""
    reply = provider("ping: reply with the single character 1", model, timeout)
    if isinstance(reply, dict) and reply.get("error"):
        if reply.get("fatal"):
            return reply["error"]
    return None


def rate_one(entry, rec, ctx, rep, provider, model, by_key=None):
    """Rate a single cited entry. Severity policy (advisory): a clear wrong-paper
    flag is an ERROR; a group-misfit (the reference does not fit the group it is
    co-cited in) is a WARN even if its standalone relevance is high; otherwise
    relevance <=3 is a WARN; relevance 4-5 with no group issue is silent. `by_key`
    lets the prompt name the co-cited references. This is the per-entry unit the
    interleaved run loop calls."""
    if not rec or not (rec.get("abstract") or "").strip():
        rep.add(Severity.INFO, entry, "[llm] skipped: no abstract available for rating",
                "llm", category="llm_relevance")
        return
    result = rate_citation(provider, model, build_rating_prompt(entry, rec, ctx, by_key))
    if "error" in result:
        rep.add(Severity.WARN, entry, f"[llm] rating unavailable: {result['error']}",
                "llm", category="llm_relevance")
        return
    rel = result.get("relevance")
    wrong = result.get("wrong_paper") is True
    misfit = result.get("group_misfit") is True
    verdict = (result.get("verdict") or "").strip()
    issue = (result.get("issue") or "").strip()
    tail = (f": {verdict}" if verdict else "") + (f" ({issue})" if issue else "")
    if wrong:
        # A wrong-paper flag is the one assertive case; still hedged as "possible".
        rep.add(Severity.ERROR, entry, f"[llm] possible wrong paper (model, abstract "
                f"only){tail}", "llm", category="wrong_paper")
        return
    if not isinstance(rel, int):
        return
    # When the standalone relevance is already weak (<=3), a group anomaly (the
    # reference does not fit the works it is co-cited with) lowers the score by a
    # further point -- a low-relevance citation hidden in a group is the worst case.
    adjusted = rel
    penalised = False
    if rel <= 3 and misfit:
        adjusted = max(1, rel - 1)
        penalised = True
    if adjusted <= 3:
        note = (f" [dropped from {rel} to {adjusted}: appears to be an odd one out "
                f"among its co-cited references]" if penalised else "")
        rep.add(Severity.WARN, entry, f"[llm] relevance rated {adjusted}/5 (model "
                f"opinion from the abstract and cited context only -- verify, do not "
                f"treat as authoritative){note}{tail}", "llm", category="llm_relevance")
    # relevance 4-5 with no penalty: no finding (silent, by design).
