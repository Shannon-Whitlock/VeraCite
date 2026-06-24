"""Tests for the web-demo entry point `veracite.check_bib_text`: the online,
no-LLM check the CGI endpoint calls. The source fetchers are stubbed so these run
offline and deterministically -- the point is the function's orchestration and the
returned payload shape, not live network behaviour.
"""

from veracite import check_bib_text, record


def _stub_sources(monkeypatch, by_doi):
    """Stub every fetcher so a DOI resolves to a caller-supplied Record (or None),
    with no network. `by_doi` maps a DOI to a (Record-or-None) factory result."""
    from veracite.models import Record

    def fake_crossref(doi, timeout):
        rec = by_doi.get(doi)
        return (rec, 200) if rec is not None else (None, 404)

    monkeypatch.setattr(record, "fetch_crossref", fake_crossref)
    monkeypatch.setattr(record, "fetch_openalex", lambda *a, **k: None)
    monkeypatch.setattr(record, "fetch_inspire", lambda *a, **k: None)
    monkeypatch.setattr(record, "fetch_arxiv", lambda *a, **k: None)
    monkeypatch.setattr(record, "fetch_isbn", lambda *a, **k: None)
    monkeypatch.setattr(record, "fetch_related", lambda *a, **k: [])
    monkeypatch.setattr(record, "fetch_abstract_s2", lambda *a, **k: "")
    # No-id search paths: keep them quiet so an entry without a DOI just stays
    # unverified rather than hitting the network.
    monkeypatch.setattr("veracite.verify._search_doi", lambda e, t: "")
    monkeypatch.setattr("veracite.verify._search_arxiv_id", lambda *a, **k: "")
    return Record


def test_envelope_shape_and_clean_entry(monkeypatch):
    Record = _stub_sources(monkeypatch, {})
    monkeypatch.setattr(record, "fetch_crossref", lambda doi, timeout: (
        Record(authors=["smith"], title="The First Paper About Things",
               year=2020, journal="Physical Review B", volume="1", pages="1"), 200))
    bib = ("@article{a, author={Smith, J}, title={The First Paper About Things}, "
           "year={2020}, journal={Physical Review B}, volume={1}, pages={1}, "
           "doi={10.1/a}}\n")
    out = check_bib_text(bib)

    # Envelope fields the CGI/front end rely on.
    assert out["veracite_version"]
    assert out["n_entries"] == 1
    assert out["truncated"] is False
    assert out["max_entries"] == 10
    # to_json payload.
    assert "findings" in out and "summary" in out and "references" in out
    assert out["summary"]["checked"] == 1
    assert out["summary"]["integrity_score"] is not None
    ref = out["references"][0]
    assert ref["key"] == "a"
    assert ref["status"] == "VERIFIED"


def test_metadata_mismatch_surfaces(monkeypatch):
    # Record says 2010; the bib says 2011 -> a metadata_mismatch on year.
    Record = _stub_sources(monkeypatch, {})
    monkeypatch.setattr(record, "fetch_crossref", lambda doi, timeout: (
        Record(authors=["nielsen"], title="Quantum Computation and Quantum Information",
               year=2010, journal="", volume="", pages=""), 200))
    bib = ("@book{n, author={Nielsen, M}, "
           "title={Quantum Computation and Quantum Information}, year={2011}, "
           "doi={10.1/n}}\n")
    out = check_bib_text(bib)
    cats = {f["category"] for f in out["findings"]}
    assert "metadata_mismatch" in cats


def test_caps_at_max_entries(monkeypatch):
    _stub_sources(monkeypatch, {})  # every DOI 404s; status doesn't matter here
    entries = "\n".join(
        f"@article{{k{i}, author={{A, B}}, title={{Paper Number {i} Here}}, "
        f"year={{2020}}, doi={{10.1/{i}}}}}" for i in range(15))
    out = check_bib_text(entries, max_entries=10)
    assert out["n_entries"] == 15
    assert out["truncated"] is True
    assert len(out["references"]) == 10
    # Only the first 10 keys were checked.
    keys = {r["key"] for r in out["references"]}
    assert keys == {f"k{i}" for i in range(10)}


def test_no_llm_categories_ever(monkeypatch):
    """The web path hard-wires provider=None, so no LLM finding can appear."""
    Record = _stub_sources(monkeypatch, {})
    monkeypatch.setattr(record, "fetch_crossref", lambda doi, timeout: (
        Record(authors=["smith"], title="Some Paper Title Goes Here", year=2020), 200))
    bib = ("@article{a, author={Smith, J}, title={Some Paper Title Goes Here}, "
           "year={2020}, doi={10.1/a}}\n")
    out = check_bib_text(bib)
    cats = {f["category"] for f in out["findings"]}
    assert not (cats & {"llm_relevance", "wrong_paper", "llm_config"})


def test_truncated_false_at_exactly_max(monkeypatch):
    _stub_sources(monkeypatch, {})
    entries = "\n".join(
        f"@article{{k{i}, author={{A, B}}, title={{Paper Number {i} Here}}, "
        f"year={{2020}}, doi={{10.1/{i}}}}}" for i in range(10))
    out = check_bib_text(entries, max_entries=10)
    assert out["truncated"] is False
    assert len(out["references"]) == 10


def test_fast_mode_suppresses_slow_sources(monkeypatch):
    """fast=True must not call the SUPPRESSED sources: INSPIRE, the Crossref related-
    works (errata) lookup, and the Semantic Scholar abstract. (OpenAlex, ISBN, and the
    need-to-basis title searches are KEPT in fast mode -- see the separate tests.) Any
    call to a suppressed source blows up so the test catches it."""
    from veracite import record, verify
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("suppressed source called"))
    from veracite.models import Record
    monkeypatch.setattr(record, "fetch_crossref", lambda doi, timeout: (
        Record(authors=["smith"], title="A Paper With A Findable Title", year=2020), 200))
    monkeypatch.setattr(record, "fetch_arxiv", lambda *a, **k: None)
    # The suppressed sources must stay untouched in fast mode.
    monkeypatch.setattr(record, "fetch_inspire", boom)
    monkeypatch.setattr(record, "fetch_related", boom)
    monkeypatch.setattr(record, "fetch_abstract_s2", boom)
    # Kept-but-capped sources: harmless stubs so the run completes (the id-less entry
    # would invoke the title searches, which are kept in fast mode).
    monkeypatch.setattr(record, "fetch_openalex", lambda *a, **k: None)
    monkeypatch.setattr(verify, "_search_doi", lambda e, t: "")
    monkeypatch.setattr(verify, "_search_arxiv_id", lambda *a, **k: "")

    bib = ("@article{a, author={Smith, J}, title={A Paper With A Findable Title}, "
           "year={2020}, doi={10.1/a}}\n"
           "@misc{b, author={Doe, J}, title={An Entry With No Identifier At All}, "
           "year={2019}}\n")
    out = check_bib_text(bib, fast=True)        # must not raise
    assert out["fast"] is True
    assert {r["key"] for r in out["references"]} == {"a", "b"}


def test_fast_mode_keeps_openalex_and_caps_its_timeout(monkeypatch):
    """fast=True KEEPS OpenAlex (for retraction detection) and ISBN, but clamps their
    per-call timeout to AUX_TIMEOUT so a slow host can't drag the request."""
    from veracite import record
    from veracite.models import Record
    from veracite.webcheck import AUX_TIMEOUT
    monkeypatch.setattr(record, "fetch_crossref", lambda doi, timeout: (
        Record(authors=["smith"], title="A Retracted Looking Paper Title", year=2020), 200))
    seen = {}

    def fake_openalex(doi, timeout):
        seen["timeout"] = timeout
        return {"is_retracted": True, "abstract": ""}

    monkeypatch.setattr(record, "fetch_openalex", fake_openalex)
    bib = ("@article{a, author={Smith, J}, title={A Retracted Looking Paper Title}, "
           "year={2020}, doi={10.1/a}}\n")
    # Core timeout well above AUX_TIMEOUT so the clamp is observable (min(30, AUX)).
    out = check_bib_text(bib, fast=True, timeout=30)
    # OpenAlex ran (retraction surfaced) and was called with the capped timeout, not 30.
    assert seen["timeout"] == AUX_TIMEOUT
    assert "retraction" in {f["category"] for f in out["findings"]}


def test_fast_mode_restores_sources_after_call(monkeypatch):
    """The slow-source stubs are installed only for the duration of the call; the
    originals (here, the test's own sentinels) must be back afterward."""
    from veracite import record, verify
    # A resolvable Crossref + stubbed OpenAlex so the call completes with no network.
    from veracite.models import Record
    monkeypatch.setattr(record, "fetch_crossref", lambda doi, timeout: (
        Record(authors=["x"], title="Title Goes Right Here Indeed", year=2020), 200))
    monkeypatch.setattr(record, "fetch_openalex", lambda *a, **k: None)
    # Capture the sentinels AFTER the monkeypatches above, so we compare against the
    # state check_bib_text should restore to (an entered-then-exited patch round-trip).
    sentinel_inspire = record.fetch_inspire
    sentinel_search = verify._search_doi
    sentinel_openalex = record.fetch_openalex
    check_bib_text("@article{a, title={Title Goes Right Here Indeed}, "
                   "author={X, Y}, year={2020}, doi={10.1/a}}\n", fast=True)
    # Restored to whatever they were before the call (including the wrapped OpenAlex).
    assert record.fetch_inspire is sentinel_inspire
    assert verify._search_doi is sentinel_search
    assert record.fetch_openalex is sentinel_openalex
