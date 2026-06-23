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
    """The entry records (excluding the reserved <file>/<summary>) plus the summary
    dict, as (refs, summary) -- the NDJSON analogue of the old object's references +
    summary, so the assertions below read the same."""
    recs = _read_ndjson(path)
    refs = {k: v for k, v in recs.items() if k not in ("<file>", "<summary>")}
    summary = recs.get("<summary>", {}).get("summary", {})
    return refs, {"summary": summary, "references": list(refs.values())}


def _write_ndjson(path, records):
    """Write a partial NDJSON checkpoint from a list of record dicts (test setup)."""
    with open(str(path), "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _entry_line(key, phases, status=None, conf=None, doi=None, rec=None):
    """A per-entry NDJSON record for test fixtures, matching checkpoint.entry_record."""
    from veracite.checkpoint import PHASES
    return {"key": key, "phases": {p: (p in phases) for p in PHASES},
            "status": status, "confidence": conf,
            "verify": f"https://doi.org/{doi}" if doi else None,
            "identifiers": {"doi": doi, "arxiv": None, "isbn": None},
            "sources": ["crossref"] if status else [],
            "canonical_record": rec, "issues": []}


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
    # After compaction: exactly one line per entry + <file> + <summary>, no dups.
    assert keys.count("a") == 1 and keys.count("b") == 1
    assert sorted(keys) == ["<file>", "<summary>", "a", "b"]


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
    entry_keys = [k for k in keys if k not in ("<file>", "<summary>")]
    assert entry_keys == ["a", "b"]                 # bib order
    assert keys[-2:] == ["<file>", "<summary>"]     # reserved records last
