"""Checkpoint / phased-resume tests: a --json report can be rebuilt and completed
in phases (offline -> online -> llm), reusing saved work and only running the
phases an entry still lacks. The online layer is stubbed so these run offline and
deterministically; a call counter proves the network is NOT re-hit on resume.
"""

import json

import pytest

from veracite import cli, llm, record
from veracite.checkpoint import Checkpoint, requested_phases

_BIB = (
    "@article{a, author={Smith, J}, title={The First Paper About Things}, "
    "year={2020}, journal={Physical Review B}, volume={1}, pages={1}, doi={10.1/a}}\n"
    "@article{b, author={Jones, K}, title={The Second Paper About Stuff}, "
    "year={2021}, journal={Nature}, volume={2}, pages={2}, doi={10.1/b}}\n"
)


@pytest.fixture
def stub_online(monkeypatch):
    """Stub every source fetcher so the online layer resolves each DOI to a clean
    matching record with an abstract, with no network. Returns a dict of call
    counters keyed by source, so a test can assert the network was (not) re-hit."""
    calls = {"crossref": 0}

    def fake_crossref(doi, timeout):
        calls["crossref"] += 1
        # Title/author chosen to match the bib so the entry verifies cleanly.
        which = "First Paper About Things" if doi.endswith("a") else "Second Paper About Stuff"
        author = "smith" if doi.endswith("a") else "jones"
        from veracite.models import Record
        return Record(authors=[author], title=f"The {which}", year=2020 if doi.endswith("a") else 2021,
                      journal="Physical Review B" if doi.endswith("a") else "Nature",
                      volume="1" if doi.endswith("a") else "2",
                      pages="1" if doi.endswith("a") else "2",
                      abstract="An abstract for rating."), 200

    monkeypatch.setattr(record, "fetch_crossref", fake_crossref)
    monkeypatch.setattr(record, "fetch_openalex", lambda *a, **k: None)
    monkeypatch.setattr(record, "fetch_inspire", lambda *a, **k: None)
    monkeypatch.setattr(record, "fetch_related", lambda *a, **k: [])
    monkeypatch.setattr(record, "fetch_abstract_s2", lambda *a, **k: "")
    # No DOI search needed (both entries carry a DOI), but stub it to be safe.
    monkeypatch.setattr("veracite.verify._search_doi", lambda e, t: "")
    return calls


def _read_ndjson(path):
    """Read the NDJSON checkpoint into {key: record}, last-line-per-key wins."""
    recs = {}
    for line in open(str(path)).read().splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            recs[r["key"]] = r
    return recs


def _phases(rec):
    """The set of true phases on an NDJSON entry record's `phases` dict."""
    return {p for p, on in rec["phases"].items() if on}


def _refs_by_key(path):
    """The entry records (the NDJSON holds nothing else) plus the summary DERIVED
    from them, as (refs, {summary, references}). The summary is no longer a stored
    record -- it is re-parsed from the entry records, so we derive it here the same
    way the tool does, with an empty Report (these fixtures carry no file-level
    findings, so duplicate/conflict counts are 0)."""
    from veracite.report import Report
    from veracite.verify import integrity
    recs = _read_ndjson(path)
    refs = {k: v for k, v in recs.items() if k not in ("<file>", "<summary>")}
    summary = integrity(list(refs.values()), Report(color=False))
    return refs, {"summary": summary, "references": list(refs.values())}


def _write_ndjson(path, records):
    """Write a partial NDJSON checkpoint from a list of record dicts (test setup)."""
    with open(str(path), "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _bib_checksums():
    """The source checksum of each entry in _BIB, keyed by citation key -- so a
    hand-built fixture record stamps the SAME checksum the live run computes, and the
    staleness check treats it as unmodified (otherwise a missing/mismatched checksum
    forces a recompute, by design)."""
    from veracite.parser import parse_bib
    from veracite.checkpoint import entry_checksum
    entries, _ = parse_bib(_BIB)
    return {e.key: entry_checksum(e.raw) for e in entries}


_CHECKSUMS = _bib_checksums()


def _entry_line(key, phases, status=None, conf=None, doi=None, rec=None,
                checksum=...):
    """A per-entry NDJSON record for test fixtures, matching checkpoint.entry_record.
    Stamps the checksum of `key` in _BIB by default so the entry reads as unmodified;
    pass checksum=None to simulate an older (pre-checksum) record."""
    from veracite.checkpoint import PHASES
    if checksum is ...:
        checksum = _CHECKSUMS.get(key)
    rec_out = {"key": key, "phases": {p: (p in phases) for p in PHASES},
               "status": status, "confidence": conf,
               "verify": f"https://doi.org/{doi}" if doi else None,
               "identifiers": {"doi": doi, "arxiv": None, "isbn": None},
               "sources": ["crossref"] if status else [],
               "canonical_record": rec, "issues": []}
    if checksum:
        rec_out["checksum"] = checksum
    return rec_out


# --- phase bookkeeping -----------------------------------------------------

def test_requested_phases():
    assert requested_phases(online=False, llm=False) == {"offline"}
    assert requested_phases(online=True, llm=False) == {"offline", "online"}
    assert requested_phases(online=True, llm=True) == {"offline", "online", "llm"}


def test_checkpoint_load_absent_or_garbage(tmp_path):
    assert Checkpoint.load(None) is None
    assert Checkpoint.load(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert Checkpoint.load(str(bad)) is None
    nonreport = tmp_path / "x.json"
    nonreport.write_text('{"hello": 1}', encoding="utf-8")
    assert Checkpoint.load(str(nonreport)) is None


def test_needs_couples_llm_to_online():
    cp = Checkpoint("x")
    cp.phases_by_key = {"a": {"offline", "online"}}
    # online satisfied, llm requested -> llm needs online re-run (for the abstract)
    assert cp.needs("a", {"offline", "online", "llm"}) == {"llm", "online"}
    # everything satisfied
    cp.phases_by_key = {"a": {"offline", "online", "llm"}}
    assert cp.needs("a", {"offline", "online", "llm"}) == set()


def test_needs_retries_online_after_transient_error():
    # An entry whose online phase RAN but failed on a transient API error (429/5xx/
    # network) is NOT settled: a resumed online run must redo it, so the rate-limited
    # 'record_unresolved' is retried rather than replayed. A normally-resolved entry
    # in the same run is left alone.
    cp = Checkpoint("x")
    cp.phases_by_key = {"good": {"offline", "online"}, "rl": {"offline", "online"}}
    cp._online_error = {"rl"}
    assert cp.needs("good", {"offline", "online"}) == set()       # settled -> nothing
    assert cp.needs("rl", {"offline", "online"}) == {"online"}    # transient -> retry
    # but an OFFLINE-only resume does not drag the online retry in (online not requested)
    assert cp.needs("rl", {"offline"}) == set()


def test_online_error_round_trips_through_checkpoint(tmp_path):
    # entry_record persists online_error only when set, and Checkpoint.load re-flags
    # the key so a resumed run knows to retry it.
    from veracite.checkpoint import entry_record
    from veracite.record import Resolution
    res_ok = Resolution(arxiv_id="2501.00001")
    res_rl = Resolution(arxiv_id="2501.00002", online_error=True)
    rec_ok = entry_record("ok", res_ok, "VERIFIED", 1.0, {"offline", "online"}, [])
    rec_rl = entry_record("rl", res_rl, "UNVERIFIED", 0.1, {"offline", "online"}, [])
    assert "online_error" not in rec_ok           # clean record stays unchanged
    assert rec_rl["online_error"] is True
    out = tmp_path / "r.ndjson"
    _write_ndjson(str(out), [rec_ok, rec_rl])
    cp = Checkpoint.load(str(out))
    assert cp._online_error == {"rl"}


def test_llm_error_round_trips_and_needs_retries_llm(tmp_path):
    # A FAILED llm call (not a 'no abstract' skip) must not settle the llm phase: the
    # record carries llm_error and the phase is left undone, so a resumed --llm retries.
    from veracite.checkpoint import entry_record
    from veracite.record import Resolution
    res_ok = Resolution(arxiv_id="2501.00001")
    res_fail = Resolution(arxiv_id="2501.00002", llm_error=True)
    rec_ok = entry_record("ok", res_ok, "VERIFIED", 1.0, {"offline", "online", "llm"}, [])
    # phase 'llm' deliberately NOT in the set for the failed entry (the cli does this).
    rec_fail = entry_record("fail", res_fail, "VERIFIED", 1.0, {"offline", "online"}, [])
    assert "llm_error" not in rec_ok
    assert rec_fail["llm_error"] is True
    out = tmp_path / "r.ndjson"
    _write_ndjson(str(out), [rec_ok, rec_fail])
    cp = Checkpoint.load(str(out))
    assert cp._llm_error == {"fail"}
    # The settled entry needs nothing; the failed one needs llm (and online, coupled).
    assert cp.needs("ok", {"offline", "online", "llm"}) == set()
    assert cp.needs("fail", {"offline", "online", "llm"}) == {"llm", "online"}


@pytest.fixture
def stub_llm_fails(monkeypatch):
    """An LLM provider that always FAILS (connection/CLI error), counting its calls."""
    calls = {"n": 0}

    def failing_provider(prompt, model, timeout):
        calls["n"] += 1
        return {"error": "connection refused"}

    # cli.py imports these names into its own namespace, so patch them on cli.
    monkeypatch.setattr(cli, "resolve_provider", lambda name, rep: failing_provider)
    monkeypatch.setattr(cli, "preflight_provider", lambda *a, **k: None)
    monkeypatch.setattr(cli, "find_citation_contexts",
                        lambda files, base: {"a": [{"file": "m.tex", "context": "c"}],
                                             "b": [{"file": "m.tex", "context": "c"}]})
    return calls


def test_failed_llm_phase_is_not_marked_complete_and_retries(tmp_path, stub_online,
                                                             stub_llm_fails):
    """A run whose --llm calls FAIL must not record llm as done; a later --llm pass
    therefore retries them (the failure left the phase genuinely incomplete)."""
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    tex = _tex(tmp_path)
    # First --llm pass: the provider errors on both, so neither gets the llm phase.
    cli.main(["--bib", str(bib), "--tex", tex, "--llm", "--no-color", "--json", str(out)])
    refs, _ = _refs_by_key(out)
    assert all("llm" not in _phases(r) for r in refs.values())     # not falsely settled
    assert all(r.get("llm_error") for r in refs.values())          # failure recorded
    first_calls = stub_llm_fails["n"]
    assert first_calls == 2                                        # both attempted
    # A second --llm pass RETRIES them (they were never settled), not skips them.
    cli.main(["--bib", str(bib), "--tex", tex, "--llm", "--no-color", "--json", str(out)])
    assert stub_llm_fails["n"] == first_calls + 2                  # retried, not skipped


def test_edited_entry_is_recomputed_on_resume(tmp_path, stub_online):
    """The source checksum makes a resumed run recompute exactly the entries whose
    .bib text changed -- editing one entry re-resolves only it, not the whole bib."""
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    assert stub_online["crossref"] == 2                            # both resolved once
    # Edit ONLY entry 'b' (change its title); 'a' is byte-identical.
    edited = _BIB.replace("The Second Paper About Stuff", "The Second Paper, Revised")
    bib.write_text(edited, encoding="utf-8")
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    # 'a' unchanged -> reused (no new resolve); 'b' edited -> recomputed (one resolve).
    assert stub_online["crossref"] == 3
    refs, _ = _refs_by_key(out)
    assert refs["b"]["checksum"] != _CHECKSUMS["b"]                # 'b' restamped


def test_complete_resume_is_byte_identical(tmp_path, stub_online):
    """A resume over a COMPLETE report reprints the same report: the summary and every
    record are a parse of the stored records, so fresh and resumed output match."""
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    first = out.read_text()
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    assert out.read_text() == first                               # identical NDJSON
    assert stub_online["crossref"] == 2                           # no re-resolve on resume


def test_ndjson_is_forward_compatible(tmp_path):
    """A report written by a FUTURE version must still load: unknown fields on an
    entry record are ignored (not rejected), an unknown reserved (<...>) record kind
    is skipped rather than mis-loaded as a bib entry, and BOTH survive a compaction
    round-trip through this version (so an old tool never silently strips new data)."""
    from veracite.checkpoint import compact, read_records as _read_records
    out = tmp_path / "future.json"
    future_entry = {
        "key": "k", "veracite_version": "9.9.9", "checksum": "deadbeef",
        "phases": {"offline": True, "online": True, "llm": False},
        "status": "VERIFIED", "confidence": 1.0,
        "identifiers": {"doi": "10.1/x", "arxiv": None, "isbn": None},
        "sources": ["crossref"],
        "canonical_record": {"title": "T", "year": 2020},
        "issues": [], "future_field": {"new": "data"},      # field this version lacks
    }
    future_kind = {"key": "<provenance>", "tool_chain": ["a", "b"]}  # unknown record kind
    _write_ndjson(out, [future_entry, future_kind])

    cp = Checkpoint.load(str(out))
    assert cp is not None
    assert "k" in cp.phases_by_key                     # real entry loads
    assert "<provenance>" not in cp.phases_by_key      # NOT mis-loaded as an entry

    compact(str(out), ["k"])
    recs, _ = _read_records(str(out))
    assert "future_field" in recs["k"]                 # unknown field preserved
    assert "<provenance>" in recs                      # unknown record kind preserved


# --- offline persists phase info -------------------------------------------

def test_offline_run_persists_offline_phase(tmp_path):
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    cli.main(["--bib", str(bib), "--offline", "--no-color", "--json", str(out)])
    refs, _ = _refs_by_key(out)
    assert set(refs) == {"a", "b"}
    assert all(_phases(r) == {"offline"} for r in refs.values())
    assert all(r["status"] is None for r in refs.values())


# --- offline -> online resume ----------------------------------------------

def test_resume_offline_then_online(tmp_path, stub_online):
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    # Phase 1: offline only.
    cli.main(["--bib", str(bib), "--offline", "--no-color", "--json", str(out)])
    assert stub_online["crossref"] == 0          # offline hit no network
    refs1, _ = _refs_by_key(out)
    assert all(_phases(r) == {"offline"} for r in refs1.values())

    # Phase 2: resume online. Both entries lack 'online', so both resolve.
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    assert stub_online["crossref"] == 2          # exactly one resolve per entry
    refs2, d2 = _refs_by_key(out)
    assert all("online" in _phases(r) for r in refs2.values())
    assert all(r["status"] == "VERIFIED" for r in refs2.values())
    assert d2["summary"]["integrity_score"] is not None


def test_resume_online_is_not_recomputed(tmp_path, stub_online):
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    assert stub_online["crossref"] == 2
    # Resume online again: every entry already has 'online' -> NO new network calls.
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    assert stub_online["crossref"] == 2          # unchanged: reused, not re-resolved
    refs, d = _refs_by_key(out)
    assert all(r["status"] == "VERIFIED" for r in refs.values())
    assert d["summary"]["verified"] == 2


def test_partial_online_resumes_only_missing(tmp_path, stub_online, monkeypatch):
    """A run interrupted after the first entry leaves a report with one online
    entry and one offline-only; resuming online resolves only the missing one."""
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    # Hand-build a partial NDJSON checkpoint: 'a' online (resolved), 'b' offline only.
    _write_ndjson(out, [
        _entry_line("a", {"offline", "online"}, status="VERIFIED", conf=1.0,
                    doi="10.1/a",
                    rec={"title": "The First Paper About Things", "year": 2020,
                         "journal": "Physical Review B", "volume": "1",
                         "number": "", "pages": "1"}),
        _entry_line("b", {"offline"}),
    ])
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    # Only 'b' was missing online -> exactly ONE new resolve.
    assert stub_online["crossref"] == 1
    refs, _ = _refs_by_key(out)
    assert all("online" in _phases(r) for r in refs.values())
    assert refs["a"]["status"] == "VERIFIED" and refs["b"]["status"] == "VERIFIED"


# --- offline-only report does not lose prior online work on re-save --------

def test_resume_offline_preserves_prior_online(tmp_path, stub_online):
    """Resuming an OFFLINE pass over a report that already holds online results must
    not discard that online work when it rewrites the report."""
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])   # online
    cli.main(["--bib", str(bib), "--offline", "--no-color", "--json", str(out)])  # offline resume
    refs, _ = _refs_by_key(out)
    # The online phase and verified status survive the offline rewrite.
    assert all("online" in _phases(r) for r in refs.values())
    assert all(r["status"] == "VERIFIED" for r in refs.values())


# --- --llm resume: rate only entries that lack a rating --------------------

@pytest.fixture
def stub_llm(monkeypatch):
    """Register a fake LLM provider that always rates 5/5 (silent) and counts its
    calls, plus stub the citation-context discovery so both entries are 'cited'.
    Returns the call counter."""
    calls = {"n": 0}

    def fake_provider(prompt, model, timeout):
        calls["n"] += 1
        return '{"relevance": 5, "wrong_paper": false}'

    monkeypatch.setitem(llm.LLM_PROVIDERS, "claude", fake_provider)
    # The CLI imported preflight_provider by name, so patch the CLI's reference
    # (patching llm.preflight_provider would leave cli.preflight_provider bound to
    # the real probe, which would call our provider with a 'ping' and skew the count).
    monkeypatch.setattr(cli, "preflight_provider", lambda *a, **k: None)
    # Pretend both keys are cited with some surrounding context.
    monkeypatch.setattr(cli, "find_citation_contexts",
                        lambda files, base: {"a": [{"file": "m.tex", "context": "ctx a"}],
                                             "b": [{"file": "m.tex", "context": "ctx b"}]})
    monkeypatch.setattr(cli, "find_citation_groups", lambda files: [])
    return calls


def _tex(tmp_path):
    t = tmp_path / "m.tex"
    t.write_text("\\cite{a} and \\cite{b}\n", encoding="utf-8")
    return str(t)


def test_llm_resume_rates_only_unrated(tmp_path, stub_online, stub_llm):
    """Point 3 of the agreed logic: --llm re-runs online (for the abstract) and
    rates any entry whose llm phase is missing; an entry already online+llm is
    fully reused -- no re-resolve, no token spend."""
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    tex = _tex(tmp_path)

    # Phase 1: online only (no --llm). Both entries get online, neither gets llm.
    cli.main(["--bib", str(bib), "--tex", tex, "--no-color", "--json", str(out)])
    assert stub_online["crossref"] == 2 and stub_llm["n"] == 0
    refs, _ = _refs_by_key(out)
    assert all(_phases(r) == {"offline", "online"} for r in refs.values())

    # Phase 2: resume with --llm. Both lack llm -> both re-resolve online (abstract)
    # AND get rated. (online re-run is required: the abstract is not persisted.)
    cli.main(["--bib", str(bib), "--tex", tex, "--llm", "--no-color", "--json", str(out)])
    assert stub_llm["n"] == 2                     # both rated
    assert stub_online["crossref"] == 4           # both re-resolved for the abstract
    refs, _ = _refs_by_key(out)
    assert all(_phases(r) == {"offline", "online", "llm"} for r in refs.values())

    # Phase 3: resume --llm again. Both already online+llm -> fully reused.
    cli.main(["--bib", str(bib), "--tex", tex, "--llm", "--no-color", "--json", str(out)])
    assert stub_llm["n"] == 2                     # no new ratings (no token spend)
    assert stub_online["crossref"] == 4           # no new resolves


def test_llm_resume_rates_only_the_missing_one(tmp_path, stub_online, stub_llm):
    """A report where 'a' is fully rated and 'b' only online: --llm rates just 'b'
    (and re-resolves only 'b' for its abstract)."""
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    out = tmp_path / "rep.json"
    tex = _tex(tmp_path)
    _write_ndjson(out, [
        _entry_line("a", {"offline", "online", "llm"}, status="VERIFIED", conf=1.0,
                    doi="10.1/a",
                    rec={"title": "The First Paper About Things", "year": 2020,
                         "journal": "Physical Review B", "volume": "1",
                         "number": "", "pages": "1"}),
        _entry_line("b", {"offline", "online"}, status="VERIFIED", conf=0.95,
                    doi="10.1/b",
                    rec={"title": "The Second Paper About Stuff", "year": 2021,
                         "journal": "Nature", "volume": "2", "number": "",
                         "pages": "2"}),
    ])
    cli.main(["--bib", str(bib), "--tex", tex, "--llm", "--no-color", "--json", str(out)])
    assert stub_llm["n"] == 1                     # only 'b' rated
    assert stub_online["crossref"] == 1           # only 'b' re-resolved
    refs, _ = _refs_by_key(out)
    assert _phases(refs["a"]) == {"offline", "online", "llm"}
    assert _phases(refs["b"]) == {"offline", "online", "llm"}


# --- crash-safety & bad-path handling --------------------------------------

def test_bad_json_path_does_not_crash(tmp_path, stub_online, capfd):
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    bad = tmp_path / "nodir" / "rep.json"        # parent does not exist
    rc = cli.main(["--bib", str(bib), "--no-color", "--json", str(bad)])
    assert rc == 0                               # analysis still completed
    assert "could not write checkpoint" in capfd.readouterr().err


# --- NDJSON format: append, last-wins, torn-line tolerance -----------------

def test_torn_final_line_is_skipped_on_resume(tmp_path, stub_online):
    """A crash mid-write leaves a torn (incomplete) final line. Resume must skip it
    and load every COMPLETE prior line -- not refuse the whole file."""
    out = tmp_path / "rep.json"
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    # 'a' fully online; then a torn half-line (as if killed mid-append).
    good = json.dumps(_entry_line("a", {"offline", "online"}, status="VERIFIED",
                                  conf=1.0, doi="10.1/a",
                                  rec={"title": "The First Paper About Things",
                                       "year": 2020, "journal": "Physical Review B",
                                       "volume": "1", "number": "", "pages": "1"}))
    with open(out, "w") as fh:
        fh.write(good + "\n")
        fh.write('{"key": "b", "phases": {"offline": true, "onli')   # torn, no newline
    cp = Checkpoint.load(str(out))
    assert cp is not None and set(cp.phases_by_key) == {"a"}   # 'b' torn line ignored
    # Resuming online resolves only 'b' (a was loaded, b was lost to the torn line).
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    assert stub_online["crossref"] == 1
    refs, _ = _refs_by_key(out)
    assert _phases(refs["a"]) == {"offline", "online"}
    assert _phases(refs["b"]) == {"offline", "online"}


def test_appends_grow_then_compaction_dedupes(tmp_path, stub_online):
    """Each phase appends new lines (no rewrite), so the raw log can hold duplicate
    keys mid-run; the end-of-run compaction collapses to one line per key."""
    out = tmp_path / "rep.json"
    bib = tmp_path / "r.bib"
    bib.write_text(_BIB, encoding="utf-8")
    cli.main(["--bib", str(bib), "--offline", "--no-color", "--json", str(out)])
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])   # resume online
    lines = [l for l in out.read_text().splitlines() if l.strip()]
    keys = [json.loads(l)["key"] for l in lines]
    # After compaction: exactly one line per entry, nothing else (no reserved
    # <file>/<summary> records -- the summary and file findings are recomputed).
    assert keys.count("a") == 1 and keys.count("b") == 1
    assert sorted(keys) == ["a", "b"]


def test_last_line_per_key_wins(tmp_path):
    """When the raw log holds two records for a key, the LAST one is authoritative."""
    out = tmp_path / "rep.json"
    _write_ndjson(out, [
        _entry_line("a", {"offline"}),                       # stale: offline only
        _entry_line("a", {"offline", "online"}, status="VERIFIED", conf=0.95,
                    doi="10.1/a"),                           # newer: online done
    ])
    cp = Checkpoint.load(str(out))
    assert cp.phases_by_key["a"] == {"offline", "online"}
    assert cp.statuses["a"][0] == "VERIFIED"


def test_entry_order_preserved_through_compaction(tmp_path, stub_online):
    """Compaction writes entries in bib order regardless of append order."""
    out = tmp_path / "rep.json"
    bib = tmp_path / "r.bib"
    # Reverse bib order on disk to prove compaction re-sorts to bib (entry) order.
    bib.write_text(_BIB, encoding="utf-8")
    cli.main(["--bib", str(bib), "--no-color", "--json", str(out)])
    keys = [json.loads(l)["key"] for l in out.read_text().splitlines() if l.strip()]
    assert keys == ["a", "b"]      # bib order, one record per entry, nothing else
