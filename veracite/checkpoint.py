"""Checkpointing: persist a (possibly partial) run to the --json report so a
large or interrupted job is resumable and can be completed in phases
(offline -> online -> llm).

The on-disk format is **NDJSON**: one self-contained JSON record per BIBLIOGRAPHY
ENTRY, in bib order, exactly one line per entry:

    {"key": "k0", "bib_source": "@article{k0, ...}", "phases": {...}, "status": "VERIFIED", ...}
    {"key": "k1", ...}

There are NO aggregate records (summary, file-level findings) -- those are
re-derived each run.

**Write strategy**:
- After each entry that is new, stale, or needs an additional phase, the entire
  file is atomically rewritten (temp + os.replace) via `write_records()`.
- Entries already complete for the requested phases are skipped; a re-run over a
  fully complete file writes nothing and leaves the file byte-identical.
- Because every write is a full atomic rewrite, there are never duplicate records
  and an interrupt at any point leaves the last fully-written state intact.
- This is O(N) per changed entry; at online-lookup speeds (seconds per entry) the
  overhead is negligible up to ~10 000 entries.

Staleness: each record stores `bib_source` (the entry's raw source text).  On a
resumed run an entry whose current source text differs from the saved one is treated
as MODIFIED -- all its cached phases are discarded and it is recomputed.
"""

import json
import os

from .models import Record
from .record import Resolution
from .report import Finding, Severity

# The phase order, weakest first.
PHASES = ("offline", "online", "llm")


# Which phase produces a finding, by category.  Anything not listed is 'offline'.
_ONLINE_CATEGORIES = {
    "metadata_mismatch", "source_conflict", "record_unresolved", "dead_doi",
    "retraction", "related_work", "preprint_superseded",
    "id_resolves_wrong_record", "doi_available", "pid_missing",
    "container_granularity", "journal_macro",
}
_LLM_CATEGORIES = {"llm_relevance", "wrong_paper", "llm_config", "llm_unavailable"}


def finding_phase(f):
    """The phase that produced finding `f`."""
    from .report import Report   # lazy: avoid import cycle at module load
    if f.category in _LLM_CATEGORIES or f.layer == "llm":
        return "llm"
    if f.category in _ONLINE_CATEGORIES or f.layer in Report._ONLINE_LAYERS:
        return "online"
    return "offline"


def requested_phases(online, llm):
    """The phases this invocation is asking to (re)compute."""
    req = {"offline"}
    if online:
        req.add("online")
    if llm:
        req.add("llm")
    return req


# --- per-entry record (the NDJSON line) ------------------------------------

def canonical_record(rec, conf):
    """The authoritative-record snapshot serialized for one entry, or None."""
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
                 bib_year=None, bib_source=None):
    """Build the persisted record for one bib entry."""
    from .config import VERSION
    rec = (res.record if res else None) or {}
    return {
        "key": key,
        "veracite_version": VERSION,
        "entry_type": entry_type,
        "line": line,
        **({"bib_year": bib_year} if bib_year else {}),
        "uncited": uncited,
        **({"bib_source": bib_source} if bib_source else {}),
        "phases": {p: (p in phases) for p in PHASES},
        "status": status,
        "confidence": conf,
        **({"status_detail": status_detail} if status_detail else {}),
        "verify": verify,
        "identifiers": {"doi": (res.doi if res else "") or None,
                        "arxiv": (res.arxiv_id if res else "") or None,
                        "isbn": (res.isbn if res else "") or None},
        "sources": sorted(res.sources) if res else [],
        "canonical_record": canonical_record(rec, conf),
        **({"dead_doi": True} if (res and getattr(res, "dead_doi", False)) else {}),
        **({"found_by_search": True}
           if (res and getattr(res, "found_by_search", False)) else {}),
        **({"online_error": True} if (res and res.online_error) else {}),
        **({"llm_error": True} if (res and getattr(res, "llm_error", False)) else {}),
        "issues": issues,
    }


def write_records(path, records_by_key, ordered_keys):
    """Atomically write the NDJSON file: one line per key in `ordered_keys` order,
    preserving unknown reserved (<...>) records from future versions verbatim.
    Uses temp + os.replace so an interrupt during the write leaves the previous
    file intact.  Returns True on success; an OSError is reported, never raised."""
    # Collect any reserved records already in the file to carry them forward.
    reserved_lines = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict) or "key" not in rec:
                        continue
                    key = rec["key"]
                    if isinstance(key, str) and key.startswith("<") and key.endswith(">"):
                        reserved_lines.append(json.dumps(rec, ensure_ascii=False))
        except OSError:
            pass

    seen = set()
    lines = []
    for k in ordered_keys:
        if k in records_by_key and k not in seen:
            seen.add(k)
            lines.append(json.dumps(records_by_key[k], ensure_ascii=False))
    lines.extend(reserved_lines)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, path)
        return True
    except OSError as ex:
        import sys
        print(f"\nwarning: could not write checkpoint to {path}: {ex}",
              file=sys.stderr)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def append_record(path, record):
    """Append one record to the NDJSON log.  Only called when a record actually
    changed (new, stale, or a new phase completed) -- unchanged records are skipped
    by the caller so no duplicate accumulation occurs on repeated runs.
    If a prior crash left a torn line with no trailing newline, a newline is
    prepended so the record lands on its own line.
    Returns True on success; an OSError is reported, never raised."""
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) != b"\n":
                    line = "\n" + line
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except OSError as ex:
        import sys
        print(f"\nwarning: could not write checkpoint to {path}: {ex}",
              file=sys.stderr)
        return False


def compact(path, ordered_keys):
    """Rewrite the checkpoint with exactly one line per key in `ordered_keys` order,
    dropping superseded duplicates and bib-entry keys not in the bib.  Unknown
    reserved (<...>) records from a future version are preserved verbatim so an
    older tool never silently strips a newer tool's data.  Called at the end of
    every run (in a `finally` block) so the file is always left clean.  Atomic
    (temp + os.replace).  Returns True on success."""
    if not path or not os.path.isfile(path):
        return False
    # Read raw to preserve unknown fields and reserved records verbatim.
    entry_records = {}   # key -> raw record dict (bib entries only)
    reserved_lines = []  # raw JSON strings for reserved (<...>) records
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or "key" not in rec:
                    continue
                key = rec["key"]
                if isinstance(key, str) and key.startswith("<") and key.endswith(">"):
                    reserved_lines.append(json.dumps(rec, ensure_ascii=False))
                else:
                    entry_records[key] = rec   # last wins
    except OSError:
        return False

    seen = set()
    lines = []
    for k in ordered_keys:
        if k in entry_records and k not in seen:
            seen.add(k)
            lines.append(json.dumps(entry_records[k], ensure_ascii=False))
    # Keys present in the file but not in ordered_keys (removed bib entries) are
    # intentionally dropped so the file stays in sync with the bib.
    lines.extend(reserved_lines)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, path)
        return True
    except OSError as ex:
        import sys
        print(f"\nwarning: could not compact checkpoint {path}: {ex}",
              file=sys.stderr)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def read_records(path):
    """Read the NDJSON checkpoint into an ordered dict {key: record}.  Skips blank
    or unparseable lines (tolerates a torn final line from a crash).  Last line per
    key wins (handles files with duplicates from an uncompacted run).  Returns
    (records, n_lines) or (None, 0) if the file is absent/unreadable."""
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
                    continue
                if not isinstance(rec, dict) or "key" not in rec:
                    continue
                key = rec["key"]
                n += 1
                records[key] = rec   # last wins
    except OSError:
        return None, 0
    return records, n


class Checkpoint:
    """A loaded prior run rebuilt from an NDJSON checkpoint."""

    def __init__(self, path):
        self.path = path
        self.findings = []
        self.results = {}
        self.statuses = {}
        self.details = {}
        self.links = {}
        self.phases_by_key = {}
        self._online_error = set()
        self._llm_error = set()
        self._bib_sources = {}
        self._findings_by_key = {}
        # (key, loser_category) pairs re-derived from saved issues stamped
        # `suppressed_by`, so a reused phase keeps its findings suppressed on resume.
        self._replay_superseded = set()
        self._records_raw = {}
        self.loaded = False

    @classmethod
    def load(cls, path):
        """Load an NDJSON checkpoint if it exists and parses.  Returns a Checkpoint
        (with .loaded True) or None if the file is absent or holds no usable records."""
        records, _ = read_records(path)
        if not records:
            return None
        cp = cls(path)
        cp._replay(records)
        cp.loaded = True
        return cp

    def _replay(self, records):
        for key, rec in records.items():
            # Skip reserved (<...>) records -- they are not bib entries.
            if isinstance(key, str) and key.startswith("<") and key.endswith(">"):
                continue
            for fd in rec.get("issues", []):
                f = _finding_from_dict(fd, key)
                if f is not None:
                    self.findings.append(f)
                    self._findings_by_key.setdefault(key, []).append(f)
                    # A saved issue stamped `suppressed_by` was retracted in the run
                    # that wrote it. Re-record that supersession so a REUSED phase (not
                    # re-run this time, so it won't re-declare it) keeps the finding
                    # suppressed -- the stamp on disk drives the re-derivation, so the
                    # outcome is identical whether the phase ran fresh or was replayed.
                    if fd.get("suppressed_by"):
                        self._replay_superseded.add((key, f.category))
            self.phases_by_key[key] = {p for p, on in (rec.get("phases") or {}).items() if on}
            if rec.get("online_error"):
                self._online_error.add(key)
            if rec.get("llm_error"):
                self._llm_error.add(key)
            if rec.get("bib_source"):
                self._bib_sources[key] = rec["bib_source"]
            self._records_raw[key] = rec
            self.results[key] = _resolution_from_record(rec)
            self.statuses[key] = (rec.get("status"),
                                  float(rec.get("confidence") or 0.0))
            if rec.get("status_detail"):
                self.details[key] = rec["status_detail"]
            if rec.get("verify"):
                self.links[key] = rec["verify"]

    def is_stale(self, key, raw):
        """True if the saved record cannot be trusted: source text differs or absent."""
        if key not in self.phases_by_key:
            return False
        return self._bib_sources.get(key) != raw

    def has(self, key, phase):
        return phase in self.phases_by_key.get(key, set())

    def needs(self, key, requested):
        """The requested phases this key has NOT already computed."""
        todo = {p for p in requested if not self.has(key, phase=p)}
        if "online" in requested and key in self._online_error:
            todo.add("online")
        if "llm" in requested and key in self._llm_error:
            todo.add("llm")
        if "llm" in todo:
            todo.add("online")
        return todo

    def seed_findings_for(self, key, keep_phases):
        """Saved findings for `key` belonging to a phase in `keep_phases`."""
        return [f for f in self._findings_by_key.get(key, [])
                if finding_phase(f) in keep_phases]

    def seed_superseded_for(self, key, keep_phases):
        """(key, loser_category) supersessions re-derived from saved issues whose
        LOSER finding belongs to a reused phase -- so seeding a reused phase also
        restores its suppressions, keeping a resumed run identical to a fresh one."""
        keep_cats = {f.category for f in self._findings_by_key.get(key, [])
                     if finding_phase(f) in keep_phases}
        return {(k, c) for (k, c) in self._replay_superseded
                if k == key and c in keep_cats}


def _finding_from_dict(fd, key):
    try:
        sev = Severity[fd.get("severity", "INFO")]
    except KeyError:
        sev = Severity.INFO
    return Finding(severity=sev, key=key,
                   line=int(fd.get("line") or 0), message=fd.get("message", ""),
                   layer=fd.get("layer", "static"), category=fd.get("category", ""),
                   type=fd.get("type", ""),
                   suggested=fd.get("suggested"),
                   source_file=fd.get("source_file", ""))


def _resolution_from_record(rec):
    """Rebuild a Resolution from a saved entry record."""
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
        res.sources[s] = res.record if s == res.source else {}
    res.dead_doi = bool(rec.get("dead_doi"))
    res.found_by_search = bool(rec.get("found_by_search"))
    return res
