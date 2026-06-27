"""Checkpointing: persist a (possibly partial) run to the --json report as an
append-only NDJSON log, and rebuild it on a later run -- so a large or interrupted
job is resumable and can be completed in phases (offline -> online -> llm).

The on-disk format is **NDJSON**: one self-contained JSON record per BIBLIOGRAPHY
ENTRY -- there are NO aggregate records. Each line is keyed by the entry's citation
key and carries everything about it -- which `phases` it has (and whether each
SUCCEEDED), a `checksum` of its source text, its verification `status`/`confidence`,
`identifiers`, the matched `canonical_record`, the `sources`, and its `issues`
(findings):

    {"key": "k0", "checksum": "...", "phases": {...}, "status": "VERIFIED", ...}
    {"key": "k1", ...}

The summary roll-up and file-level findings (duplicates, dropped cited keys, ...)
are NOT stored: they are re-derived each run by parsing these entry records (the
single source of truth) plus the cheap offline file-rules.

Why NDJSON: a new entry is a single O(1) APPEND (no rewrite of the whole growing
report), so checkpointing after every entry stays cheap even at 10k references, and
a crash mid-run leaves every prior line intact (a torn final line is just skipped on
load). Re-running an entry simply APPENDS a fresh record for that key; on load, the
LAST record per key wins, so an updated entry supersedes its earlier state with no
in-place edit. At the end of a clean run the file is COMPACTED -- rewritten once,
atomically, with exactly one line per key in bibliography order.

Staleness: each record stores a `checksum` of the entry's raw source text. On a
resumed run an entry whose current checksum differs from the saved one (or whose
record predates checksums and has none) is treated as MODIFIED -- all its cached
phases are discarded and it is recomputed -- so editing the .bib and re-running
re-verifies exactly the changed entries, with no need to name them by --key.

This module owns all of that format knowledge (the per-key record shape, append,
load with last-wins, compaction) so the CLI driver and report.py stay agnostic.
"""

import hashlib
import json
import os

from .models import Record
from .record import Resolution
from .report import Finding, Severity

# The phase order, weakest first. A run "requests" a set of phases; an entry is
# (re)processed for a requested phase it does not already have.
PHASES = ("offline", "online", "llm")


def entry_checksum(raw):
    """A stable digest of an entry's raw source text, used to detect that the .bib
    entry was edited between runs. Raw text (not normalized fields): ANY change --
    even reformatting -- counts as a modification and invalidates the cached result,
    which is the conservative, never-serve-a-stale-verification choice. Short hex
    prefix: collision-resistant enough to flag edits, compact in the NDJSON."""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:16]

# Reserved (non-entry) keys are wrapped in angle brackets, which a citation key can
# never be -- so any "<...>" key is a reserved record, not a bib entry. Current
# versions write none, but treating the whole bracketed namespace as reserved means a
# record kind a FUTURE version introduces is recognized as non-entry and skipped on
# replay (rather than mis-loaded as a bogus entry), while still being carried through
# compaction verbatim. Forward-compatibility by convention.
FILE_KEY = "<file>"
SUMMARY_KEY = "<summary>"


def _is_reserved_key(key):
    """True for a non-entry (reserved) record key. Any angle-bracketed key qualifies,
    so unknown future reserved kinds are handled, not just <file>/<summary>."""
    return isinstance(key, str) and key.startswith("<") and key.endswith(">")

# Which phase produces a finding, by its category. Anything not listed is treated
# as 'offline' (the static/syntax rules), the conservative default -- those
# findings are cheap to recompute and never depend on the network. Only the
# online/llm categories must be named, since those are the ones we must NOT throw
# away when resuming a job that already paid for them.
_ONLINE_CATEGORIES = {
    "metadata_mismatch", "source_conflict", "record_unresolved", "dead_doi",
    "retraction", "related_work", "preprint_superseded",
    "id_resolves_wrong_record", "doi_available", "pid_missing",
    "container_granularity", "journal_macro",
}
_LLM_CATEGORIES = {"llm_relevance", "wrong_paper", "llm_config", "llm_unavailable"}


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

def canonical_record(rec, conf):
    """The authoritative-record snapshot serialized for one entry, or None. Carries
    title/year/journal/volume/number/pages always; the AUTHOR list only when the
    match is identity-certain (confidence >= 0.95). Authors are the identity field,
    so copying them from a weak (same-title/same-first-author) near-miss would
    overwrite the entry to a different paper and erase the mismatch signal -- the
    exact wrong-paper failure VeraCite exists to catch. `authors_complete` flags
    surname-only data (Crossref) so a consumer never clobbers full given names."""
    if not rec:
        return None
    out = {k: rec.get(k) for k in
           ("title", "year", "journal", "volume", "number", "pages")}
    authors = rec.get("authors_display") or []
    if conf is not None and conf >= 0.95 and authors:
        given = rec.get("given") or {}
        out["authors"] = list(authors)
        full = [g for g in given.values() if len(str(g).replace(".", "").strip()) > 1]
        out["authors_complete"] = len(full) >= len(authors)
    return out


def entry_record(key, res, status, conf, phases, issues, verify=None,
                 entry_type=None, line=0, uncited=False, status_detail="",
                 bib_year=None, checksum=None):
    """Build the persisted record for one bib entry: a self-contained dict with its
    phases, verification status, identifiers, canonical record, sources and issues.
    `res` is a Resolution (or None for an offline-only entry); `issues` is a list of
    finding dicts (Report._finding_dict shape). This is the inverse of the loader's
    `_resolution_from_record`, so a round trip reproduces the same report.

    The record is the SINGLE canonical result for the entry: the terminal report is a
    pretty-print of exactly these fields, so the record carries everything a header
    needs -- the `entry_type` (@article/...) and source `line` that identify it, and
    an `uncited` flag for the --tex skipped state -- not just what a resume needs.
    Hence the report is fully reconstructible from the NDJSON whether or not --json
    was used (the in-memory run builds the same records to render)."""
    from .config import VERSION
    rec = (res.record if res else None) or {}
    return {
        "key": key,
        # The tool revision that produced THIS record -- per-record (not a single
        # report-wide stamp), so a report resumed across versions is traceable
        # line-by-line and a shared NDJSON is self-identifying.
        "veracite_version": VERSION,
        # Digest of the entry's source text: a resumed run recomputes the entry when
        # this differs from the .bib (it was edited) or is absent (an older report).
        **({"checksum": checksum} if checksum else {}),
        "entry_type": entry_type,
        "line": line,
        # The entry's OWN (bib) year, kept distinct from canonical_record.year (the
        # resolved year, which can differ). The DOI-eligibility gate (post-2005
        # article-likes) keys off the bib year, so the summary re-derived from records
        # needs it. Persisted only when present.
        **({"bib_year": bib_year} if bib_year else {}),
        "uncited": uncited,
        "phases": {p: (p in phases) for p in PHASES},
        "status": status,
        "confidence": conf,
        # The short human reason shown in the header for a non-clean status
        # (UNVERIFIED/MISMATCH) -- persisted so the header is reconstructible from the
        # record alone, including on resume (where it was previously lost).
        "status_detail": status_detail or "",
        "verify": verify,
        "identifiers": {"doi": (res.doi if res else "") or None,
                        "arxiv": (res.arxiv_id if res else "") or None,
                        "isbn": (res.isbn if res else "") or None},
        "sources": sorted(res.sources) if res else [],
        "canonical_record": canonical_record(rec, conf),
        # Score inputs that are NOT recoverable from the other persisted fields, so the
        # confidence roll-up is identical on a reprint. `_confidence_kind` keys off
        # these, and without them a resumed run would re-bucket the entry (e.g. a
        # dead-DOI-recovered or found-by-search entry would read as plainly 'trusted')
        # and the score would drift. Persisted only when true, like online_error.
        **({"dead_doi": True} if (res and getattr(res, "dead_doi", False)) else {}),
        **({"found_by_search": True}
           if (res and getattr(res, "found_by_search", False)) else {}),
        # A TRANSIENT online failure (rate-limit/5xx/network) is marked so a resumed
        # run RE-RUNS this entry's online phase instead of trusting the failed result.
        # Persisted only when true, so a clean record stays unchanged.
        **({"online_error": True} if (res and res.online_error) else {}),
        # A FAILED LLM call (provider/connection error, not a 'no abstract' skip) is
        # likewise marked so a resumed run retries the llm phase. The phases map above
        # already records llm as NOT done in this case; this flag makes the failure
        # explicit and survives even a record whose llm phase is re-requested later.
        **({"llm_error": True} if (res and getattr(res, "llm_error", False)) else {}),
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
    """Rewrite the checkpoint with exactly one line per ENTRY key, in `ordered_keys`
    order (bib order). One record per bib entry is the whole file -- no reserved
    <file>/<summary> records (the summary and file-level findings are recomputed each
    run, not stored); any such lines left by an older report are dropped here. Done
    once at the end of a clean run so a finished report has no superseded duplicate
    lines. Atomic (temp + os.replace), so an interruption during compaction cannot
    corrupt the still-valid append log it replaces. Returns True on success."""
    records, _ = _read_records(path)        # last-wins map: key -> record
    if records is None:
        return False
    reserved = {FILE_KEY, SUMMARY_KEY}
    seen = set()
    lines = []
    for k in ordered_keys:
        if k in records and k not in seen and k not in reserved:
            seen.add(k)
            lines.append(json.dumps(records[k], ensure_ascii=False))
    # Any record whose key was not in `ordered_keys` (shouldn't happen, but be safe)
    # is appended at the end so nothing is silently dropped -- except the retired
    # reserved aggregate records, which are intentionally not carried forward.
    for k, rec in records.items():
        if k not in seen and k not in reserved:
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
        self.details = {}                   # key -> status_detail (header reason)
        self.links = {}                     # key -> verify url
        self.phases_by_key = {}             # key -> set(phases done)
        self._online_error = set()          # keys whose online phase failed transiently
        self._llm_error = set()             # keys whose llm phase call FAILED (retry it)
        self._checksums = {}                # key -> saved source checksum (if any)
        self.summary = None                 # legacy <summary> record, if an old file
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
            # Reserved (<...>) records are not bib entries. Current versions write
            # none; an OLD report may carry <summary>/<file>, and a FUTURE one may
            # carry kinds we do not know. Replay the legacy <file> findings (so an
            # old report's file-level issues still surface), keep a legacy <summary>
            # for reference, and otherwise SKIP -- never load a reserved record as an
            # entry. They remain in the file (compaction preserves them verbatim).
            if _is_reserved_key(key):
                if key == FILE_KEY:
                    for fd in rec.get("issues", []):
                        f = _finding_from_dict(fd, key)
                        if f is not None:
                            self.findings.append(f)
                            self._findings_by_key.setdefault(key, []).append(f)
                elif key == SUMMARY_KEY:
                    self.summary = rec.get("summary")
                continue
            for fd in rec.get("issues", []):
                f = _finding_from_dict(fd, key)
                if f is not None:
                    self.findings.append(f)
                    self._findings_by_key.setdefault(key, []).append(f)
            self.phases_by_key[key] = {p for p, on in (rec.get("phases") or {}).items() if on}
            if rec.get("online_error"):
                self._online_error.add(key)
            if rec.get("llm_error"):
                self._llm_error.add(key)
            if rec.get("checksum"):
                self._checksums[key] = rec["checksum"]
            self.results[key] = _resolution_from_record(rec)
            self.statuses[key] = (rec.get("status"),
                                  float(rec.get("confidence") or 0.0))
            if rec.get("status_detail"):
                self.details[key] = rec["status_detail"]
            if rec.get("verify"):
                self.links[key] = rec["verify"]

    # -- phase coverage -----------------------------------------------------

    def is_stale(self, key, checksum):
        """True if the saved record for `key` cannot be trusted for the current .bib:
        its source checksum differs from `checksum` (the entry was edited) OR no
        checksum was saved (an older report, predating this field). A stale entry has
        ALL its cached phases discarded and is recomputed from scratch. A key never
        seen before is NOT stale here (it is simply new work)."""
        if key not in self.phases_by_key:
            return False
        return self._checksums.get(key) != checksum

    def has(self, key, phase):
        return phase in self.phases_by_key.get(key, set())

    def needs(self, key, requested):
        """The requested phases this key has NOT already computed -- the work left
        to do for it. Empty means the saved entry already satisfies the request.

        The LLM rating needs the work's abstract, which only the online layer
        fetches and which is NOT persisted (it is an LLM input, not a result). So
        when the llm phase must run, the online phase is run with it -- otherwise a
        resumed --llm pass would have no abstract to rate. This is the one phase
        coupling; offline is always independent.

        A saved entry whose online phase failed on a TRANSIENT error (rate-limit/
        5xx/network) is NOT settled: re-run its online phase so a resumed pass
        recovers it, rather than replaying the failed 'record_unresolved'."""
        todo = {p for p in requested if not self.has(key, phase=p)}
        if "online" in requested and key in self._online_error:
            todo.add("online")
        # A failed llm call left the phase not-done, but flag it explicitly too so a
        # re-run retries it even if a future format marked the phase present.
        if "llm" in requested and key in self._llm_error:
            todo.add("llm")
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
                            pages=crec.get("pages") or "",
                            authors_display=list(crec.get("authors") or []))
        res.source = (rec.get("sources") or [""])[0]
    for s in (rec.get("sources") or []):
        # Per-source records are not persisted individually; a placeholder keeps
        # len(sources) correct for the confidence/cross-source logic and the
        # `sources` list. The primary record carries the comparable fields.
        res.sources[s] = res.record if s == res.source else {}
    # Score inputs persisted only when true (see entry_record): restore so the
    # confidence roll-up buckets the entry identically on reprint.
    res.dead_doi = bool(rec.get("dead_doi"))
    res.found_by_search = bool(rec.get("found_by_search"))
    return res
