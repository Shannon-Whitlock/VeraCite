"""Checkpointing: persist a (possibly partial) run to the --json report as an
append-only NDJSON log, and rebuild it on a later run -- so a large or interrupted
job is resumable and can be completed in phases (offline -> online -> llm).

The on-disk format is **NDJSON**: one self-contained JSON record per line. Most
lines are one bibliography ENTRY, keyed by its citation key and carrying everything
about it -- which `phases` it has, its verification `status`/`confidence`,
`identifiers`, the matched `canonical_record`, the `sources`, and its `issues`
(findings):

    {"key": "k0", "phases": {...}, "status": "VERIFIED", "issues": [...], ...}
    {"key": "k1", ...}
    {"key": "<file>", "issues": [...]}      # file-level findings (duplicates, ...)
    {"key": "<summary>", "summary": {...}}  # the integrity roll-up / offline stub

Why NDJSON: a new entry is a single O(1) APPEND (no rewrite of the whole growing
report), so checkpointing after every entry stays cheap even at 10k references, and
a crash mid-run leaves every prior line intact (a torn final line is just skipped on
load). Re-running an entry simply APPENDS a fresh record for that key; on load, the
LAST record per key wins, so an updated entry supersedes its earlier state with no
in-place edit. At the end of a clean run the file is COMPACTED -- rewritten once,
atomically, with exactly one line per key in bibliography order.

This module owns all of that format knowledge (the per-key record shape, append,
load with last-wins, compaction) so the CLI driver and report.py stay agnostic.
"""

import json
import os

from .models import Record
from .record import Resolution
from .report import Finding, Severity

# The phase order, weakest first. A run "requests" a set of phases; an entry is
# (re)processed for a requested phase it does not already have.
PHASES = ("offline", "online", "llm")

# Reserved keys for the non-entry records.
FILE_KEY = "<file>"
SUMMARY_KEY = "<summary>"

# Which phase produces a finding, by its category. Anything not listed is treated
# as 'offline' (the static/syntax rules), the conservative default -- those
# findings are cheap to recompute and never depend on the network. Only the
# online/llm categories must be named, since those are the ones we must NOT throw
# away when resuming a job that already paid for them.
_ONLINE_CATEGORIES = {
    "metadata_mismatch", "source_conflict", "record_unresolved", "dead_doi",
    "retraction", "related_work", "preprint_superseded",
    "id_resolves_wrong_record", "doi_available", "pid_missing", "pid_optional",
    "container_granularity",
}
_LLM_CATEGORIES = {"llm_relevance", "wrong_paper", "llm_config"}


def finding_phase(f):
    """The phase that produced finding `f`. LLM and online categories are named
    explicitly; everything else is an offline static/syntax finding."""
    from .report import Report   # lazy: avoid an import cycle at module load
    if f.category in _LLM_CATEGORIES or f.layer == "llm":
        return "llm"
    if f.category in _ONLINE_CATEGORIES or f.layer in Report._ONLINE_LAYERS:
        return "online"
    return "offline"


def requested_phases(online, llm):
    """The phases this invocation is asking to (re)compute. Offline always runs;
    online unless --offline; llm only with --llm (which implies online)."""
    req = {"offline"}
    if online:
        req.add("online")
    if llm:
        req.add("llm")
    return req


# --- per-entry record (the NDJSON line) ------------------------------------

def entry_record(key, res, status, conf, phases, issues, verify=None):
    """Build the persisted record for one bib entry: a self-contained dict with its
    phases, verification status, identifiers, canonical record, sources and issues.
    `res` is a Resolution (or None for an offline-only entry); `issues` is a list of
    finding dicts (Report._finding_dict shape). This is the inverse of the loader's
    `_resolution_from_record`, so a round trip reproduces the same report."""
    rec = (res.record if res else None) or {}
    return {
        "key": key,
        "phases": {p: (p in phases) for p in PHASES},
        "status": status,
        "confidence": conf,
        "verify": verify,
        "identifiers": {"doi": (res.doi if res else "") or None,
                        "arxiv": (res.arxiv_id if res else "") or None,
                        "isbn": (res.isbn if res else "") or None},
        "sources": sorted(res.sources) if res else [],
        "canonical_record": {k: rec.get(k) for k in
                             ("title", "year", "journal", "volume",
                              "number", "pages")} if rec else None,
        "issues": issues,
    }


def file_record(issues):
    """The reserved file-level record: findings not tied to one entry (duplicates,
    brace balance, a cited key with no entry)."""
    return {"key": FILE_KEY, "issues": issues}


def summary_record(summary):
    """The reserved summary record: the integrity roll-up (or offline stub), stamped
    with the VeraCite version that produced the report so a saved/shared report is
    traceable to the exact tool revision (checks and scoring can change between
    versions)."""
    from .config import VERSION
    return {"key": SUMMARY_KEY, "veracite_version": VERSION, "summary": summary}


def append_record(path, record):
    """Append one NDJSON record (a single line) to the checkpoint. O(1): no rewrite
    of the existing file. Each line is a complete JSON value terminated by '\\n', so
    a crash after a full line leaves a loadable file and a crash mid-line is skipped
    on load. Returns True on success; an OSError is reported, never raised."""
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError as ex:
        import sys
        print(f"\nwarning: could not write checkpoint to {path}: {ex}", file=sys.stderr)
        return False


def compact(path, ordered_keys):
    """Rewrite the checkpoint with exactly one line per key, in `ordered_keys` order
    (entry keys in bib order), followed by the <file> and <summary> records. Done
    once at the end of a clean run so a finished report has no superseded duplicate
    lines. Atomic (temp + os.replace), so an interruption during compaction cannot
    corrupt the still-valid append log it replaces. Returns True on success."""
    records, _ = _read_records(path)        # last-wins map: key -> record
    if records is None:
        return False
    order = list(ordered_keys) + [FILE_KEY, SUMMARY_KEY]
    seen = set()
    lines = []
    for k in order:
        if k in records and k not in seen:
            seen.add(k)
            lines.append(json.dumps(records[k], ensure_ascii=False))
    # Any record whose key was not in `ordered_keys` (shouldn't happen, but be safe)
    # is appended at the end so nothing is silently dropped.
    for k, rec in records.items():
        if k not in seen:
            lines.append(json.dumps(rec, ensure_ascii=False))
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, path)
        return True
    except OSError as ex:
        import sys
        print(f"\nwarning: could not compact checkpoint {path}: {ex}", file=sys.stderr)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _read_records(path):
    """Read the NDJSON checkpoint into an ORDERED last-wins map {key: record}. A
    later line for a key supersedes an earlier one (an updated entry), keeping the
    key's first-seen position so bib order is stable. A blank or unparseable line
    (e.g. a torn final line from a crash) is skipped. Returns (records, n_lines) or
    (None, 0) if the file is absent/unreadable."""
    if not path or not os.path.isfile(path):
        return None, 0
    records = {}
    n = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue            # torn / partial line -- skip it
                if not isinstance(rec, dict) or "key" not in rec:
                    continue
                n += 1
                records[rec["key"]] = rec   # last wins; insertion order preserved
    except OSError:
        return None, 0
    return records, n


class Checkpoint:
    """A loaded prior run rebuilt from an NDJSON checkpoint. `phases_by_key[key]` is
    the set of phases already computed for an entry; `needs(key, requested)` says
    which requested phases are still missing. The replayed findings, resolutions,
    statuses and verify links are exposed so the driver can reproduce prior work
    without recomputing it."""

    def __init__(self, path):
        self.path = path
        self.findings = []                  # replayed Finding objects (all keys)
        self.results = {}                   # key -> Resolution
        self.statuses = {}                  # key -> (status, confidence)
        self.links = {}                     # key -> verify url
        self.phases_by_key = {}             # key -> set(phases done)
        self.summary = None                 # the saved summary record, if any
        self._findings_by_key = {}
        self.loaded = False

    @classmethod
    def load(cls, path):
        """Load an NDJSON checkpoint if it exists and parses. Returns a Checkpoint
        (with .loaded True) or None if the file is absent or holds no usable record
        (so a stray/empty/foreign path just starts fresh)."""
        records, _ = _read_records(path)
        if not records:
            return None
        cp = cls(path)
        cp._replay(records)
        cp.loaded = True
        return cp

    def _replay(self, records):
        for key, rec in records.items():
            if key == SUMMARY_KEY:
                self.summary = rec.get("summary")
                continue
            for fd in rec.get("issues", []):
                f = _finding_from_dict(fd, key)
                if f is not None:
                    self.findings.append(f)
                    self._findings_by_key.setdefault(key, []).append(f)
            if key == FILE_KEY:
                continue
            self.phases_by_key[key] = {p for p, on in (rec.get("phases") or {}).items() if on}
            self.results[key] = _resolution_from_record(rec)
            self.statuses[key] = (rec.get("status"),
                                  float(rec.get("confidence") or 0.0))
            if rec.get("verify"):
                self.links[key] = rec["verify"]

    # -- phase coverage -----------------------------------------------------

    def has(self, key, phase):
        return phase in self.phases_by_key.get(key, set())

    def needs(self, key, requested):
        """The requested phases this key has NOT already computed -- the work left
        to do for it. Empty means the saved entry already satisfies the request.

        The LLM rating needs the work's abstract, which only the online layer
        fetches and which is NOT persisted (it is an LLM input, not a result). So
        when the llm phase must run, the online phase is run with it -- otherwise a
        resumed --llm pass would have no abstract to rate. This is the one phase
        coupling; offline is always independent."""
        todo = {p for p in requested if not self.has(key, phase=p)}
        if "llm" in todo:
            todo.add("online")
        return todo

    def seed_findings_for(self, key, keep_phases):
        """The saved findings for `key` that belong to a phase in `keep_phases` --
        i.e. the prior findings to replay because this run is NOT recomputing their
        phase. Offline findings are always recomputed live, so they are never in
        `keep_phases` and never replayed (avoids duplicating a static finding)."""
        return [f for f in self._findings_by_key.get(key, [])
                if finding_phase(f) in keep_phases]

    def file_findings(self):
        """The replayed file-level (<file>) findings, so a resumed run that does not
        recompute them (it always does, but defensively) does not lose them."""
        return list(self._findings_by_key.get(FILE_KEY, []))


def _finding_from_dict(fd, key):
    """Rebuild a Finding from a persisted issue dict. The key comes from the record
    (issues no longer carry their own key). Tolerates a missing/odd severity by
    defaulting to a note rather than crashing on a hand-edited report."""
    try:
        sev = Severity[fd.get("severity", "INFO")]
    except KeyError:
        sev = Severity.INFO
    return Finding(severity=sev, key=key,
                   line=int(fd.get("line") or 0), message=fd.get("message", ""),
                   layer=fd.get("layer", "static"), category=fd.get("category", ""),
                   suggested=fd.get("suggested"))


def _resolution_from_record(rec):
    """Rebuild a Resolution from a saved entry record -- enough for the integrity
    score (ids, sources) and the report to regenerate identically. Abstracts are not
    persisted (they are an LLM input, not a result), so a resumed LLM phase re-fetches
    the abstract via the online layer."""
    ids = rec.get("identifiers") or {}
    res = Resolution()
    res.doi = ids.get("doi") or ""
    res.arxiv_id = ids.get("arxiv") or ""
    res.isbn = ids.get("isbn") or ""
    crec = rec.get("canonical_record")
    if crec:
        res.record = Record(title=crec.get("title") or "", year=crec.get("year"),
                            journal=crec.get("journal") or "",
                            volume=crec.get("volume") or "",
                            number=crec.get("number") or "",
                            pages=crec.get("pages") or "")
        res.source = (rec.get("sources") or [""])[0]
    for s in (rec.get("sources") or []):
        # Per-source records are not persisted individually; a placeholder keeps
        # len(sources) correct for the confidence/cross-source logic and the
        # `sources` list. The primary record carries the comparable fields.
        res.sources[s] = res.record if s == res.source else {}
    return res
