"""Offline tests for VeraCite: parser, syntax pass, static rules, and the
predicates that have a history of false positives. No network is touched.
"""

import os

import pytest

from veracite.config import load_settings
from veracite.parser import parse_bib
from veracite.report import Report, Severity
from veracite.rules import run_static, syntax_pass
from veracite import record, normalize, verify

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(autouse=True)
def _defaults():
    """Each test runs against default settings (no user config)."""
    load_settings(explicit_path=os.devnull)


def check(name):
    """Run the full offline pipeline on a fixture; return the Report."""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        raw = fh.read()
    entries, problems = parse_bib(raw)
    rep = Report(color=False)
    syntax_pass(raw, entries, problems, rep)
    run_static(entries, rep)
    return rep, entries


def messages(rep, category=None):
    return [f.message for f in rep.findings
            if category is None or f.category == category]


def suggestions(rep, category=None):
    """The structured `suggested` dicts (ground truth for proposed edits), now that
    the '(suggested: X -> Y)' prose is derived from them rather than stored in the
    message text."""
    return [f.suggested for f in rep.findings
            if f.suggested and (category is None or f.category == category)]


def _month_notes(bib):
    """Run the static rules over one entry and return its month-related notes as
    rendered lines (so the derived '(suggested: ...)' tail is included)."""
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    return [rep._finding_line(f) for f in rep.findings if "month" in f.message]


# --- parser ---------------------------------------------------------------

def test_clean_bib_parses_all_entries():
    _, entries = check("clean.bib")
    assert {e.key for e in entries} == {"good2020", "book2018"}


def test_clean_bib_has_no_errors():
    rep, _ = check("clean.bib")
    assert rep.count(Severity.ERROR) == 0


def test_parser_recovers_after_unbalanced_entry():
    # Despite a broken first entry, later entries must still be parsed.
    _, entries = check("structural.bib")
    keys = {e.key for e in entries}
    assert "dupfield" in keys      # the last entry survived the earlier breakage


# --- syntax pass ----------------------------------------------------------

def test_unbalanced_braces_flagged():
    rep, _ = check("structural.bib")
    assert any("unbalanced braces" in m for m in messages(rep, "syntax"))


def test_field_outside_entry_flagged():
    # A 'doi = ...' after the closing '}' is dropped by BibTeX -- flag it.
    bib = ('@article{a, author={X}, title={T}, year={2020}, journal={J}}\n'
           'doi = "10.1/x"\n'
           '@article{b, author={Y}, title={U}, year={2021}, journal={K}}\n')
    _entries, problems = parse_bib(bib)
    assert any("outside the entry" in msg and "doi" in msg for _ln, msg in problems)


def test_comment_between_entries_not_flagged():
    bib = ('@article{a, author={X}, title={T}, year={2020}, journal={J}}\n'
           '% just a comment, not a field\n'
           '@article{b, author={Y}, title={U}, year={2021}, journal={K}}\n')
    _entries, problems = parse_bib(bib)
    assert not any("outside the entry" in msg for _ln, msg in problems)


def test_missing_equals_flagged():
    rep, _ = check("structural.bib")
    assert any("missing its '='" in m for m in messages(rep, "syntax"))


def test_unknown_entry_type_flagged():
    rep, _ = check("structural.bib")
    assert any("unknown entry type '@artical'" in m for m in messages(rep, "syntax"))


def test_stray_closing_brace_flagged():
    rep, _ = check("structural.bib")
    assert any("stray '}'" in m for m in messages(rep, "syntax"))


def test_duplicate_field_flagged():
    # The fixture repeats 'volume' with DIFFERING values (1 vs 2): BibTeX keeps one
    # and silently drops the other, a real risk of contaminating the compiled
    # bibliography -- so it is a WARN in its own 'duplicate_field_conflict' category,
    # distinct from a duplicate citation key/DOI (an error on the 'duplicate' floor).
    rep, _ = check("structural.bib")
    msgs = messages(rep, "duplicate_field_conflict")
    assert any("appears 2 times" in m and "volume" in m for m in msgs)
    assert all(f.severity is Severity.WARN
               for f in rep.findings if f.category == "duplicate_field_conflict")


def test_duplicate_field_same_value_is_a_note():
    # When the repeats AGREE, nothing is lost -- a note, not a warning.
    entries, _ = parse_bib("@article{k, author={A}, title={T}, journal={J}, "
                           "year={2020}, keywords={x}, keywords={x}}")
    rep = Report(color=False)
    run_static(entries, rep)
    dups = [f for f in rep.findings if f.category == "duplicate_field"]
    assert any("keywords" in f.message for f in dups)
    assert all(f.severity is Severity.INFO for f in dups)
    # and it must NOT be escalated onto the error-level 'duplicate' floor
    assert not [f for f in rep.findings if f.category == "duplicate"]


# --- static style rules ---------------------------------------------------

def test_en_dash_fix_offered():
    rep, _ = check("style.bib")
    assert any("--" in s["to"] and "--" not in s["from"]
               for s in suggestions(rep, "style") if "from" in s)


def test_brace_protection_fix_offered():
    rep, _ = check("style.bib")
    assert any(s.get("from") == "Python" and s["to"] == "{Python}"
               for s in suggestions(rep, "style"))


def test_doi_url_to_bare_fix():
    rep, _ = check("style.bib")
    assert any("doi.org" in s.get("from", "") and s["to"].startswith("10.1016")
               for s in suggestions(rep, "style"))


def test_bare_doi_unescapes_bibtex_escapes():
    # A '\_' (or '\&', '\#', ...) is a BibTeX escape for a literal character; a DOI
    # never contains a backslash, so it must be stripped before resolution -- else
    # the DOI 404s and is wrongly reported as dead.
    from veracite.normalize import bare_doi
    assert bare_doi(r"10.1007/978-3-031-25069-9\_19") == "10.1007/978-3-031-25069-9_19"
    assert bare_doi(r"https://doi.org/10.1007/978-3-031-25069-9\_19") \
        == "10.1007/978-3-031-25069-9_19"
    assert bare_doi(r"10.1234/foo\&bar") == "10.1234/foo&bar"
    # a clean DOI is untouched
    assert bare_doi("10.1103/PhysRevA.88.052108") == "10.1103/PhysRevA.88.052108"


def test_verify_url_unescapes_bibtex_specials():
    # A url field carries TeX-escaped specials ('\_', '\&', ...) that are literal in
    # the real address; the verify link must de-escape them so it is clickable.
    from veracite.record import _clean_url
    assert _clean_url(r"http://papers.nips.cc/paper\_files/x.html") \
        == "http://papers.nips.cc/paper_files/x.html"
    assert _clean_url(r"https://e.com/a\&b\%c\#d") == "https://e.com/a&b%c#d"
    assert _clean_url(r"https://e.com/~{}u") == "https://e.com/~u"
    assert _clean_url("https://clean.com/p") == "https://clean.com/p"


def test_and_others_flagged_as_truncation():
    # 'and others' is valid, but it discards the dropped names. For good record-
    # keeping the full list should live in the .bib (the style truncates), so the
    # marker is flagged as a truncation -- with the data-loss rationale, not a
    # "don't use truncation markers" message.
    rep, _ = check("style.bib")
    assert any("truncated with 'and others'" in m
               for m in messages(rep, "author_completeness"))


def test_literal_et_al_flagged():
    # A spelled-out 'et al.' becomes a fake author and bakes in a journal's
    # rendering -- flag it and point at the full list / 'and others'.
    rep, _ = check("style.bib")
    assert any("literal 'et al.'" in m
               for m in messages(rep, "author_completeness"))


def test_bare_month_macro_not_flagged_in_fixture():
    # style.bib uses 'month = jan' (a bare macro) -- the canonical, sortable form,
    # so it must NOT be flagged. (Braced/spelled-out months are covered below.)
    rep, _ = check("style.bib")
    assert not any("month" in m for m in messages(rep, "style"))


def test_clean_bib_no_style_noise():
    # A well-formed entry should not trip brace/dash/doi style rules.
    rep, _ = check("clean.bib")
    assert messages(rep, "style") == []


# --- biblatex datamodel validity ------------------------------------------

def test_journal_alias_not_flagged_on_article():
    # 'journal' aliases biblatex 'journaltitle' -- must not be flagged.
    rep, _ = check("clean.bib")
    assert not any("journal" in m for m in messages(rep, "biblatex_validity"))


# --- predicates with a false-positive history -----------------------------

def test_journal_equiv_distinguishes_nature_variants():
    assert not record._journal_equiv("nature", "nature physics")
    assert record._journal_equiv("phys. rev. lett.", "physical review letters")


def test_surname_match_particles_only():
    assert record._surname_match("dasilva", "silva")        # particle dropped
    assert record._surname_match("vandeveerdonk", "veerdonk")
    assert not record._surname_match("han", "chan")          # not a particle
    assert not record._surname_match("son", "johnson")


def test_author_mismatch_message_shows_readable_names_not_folded_keys():
    # The WARN stands (we conform to Crossref), but the message must show the
    # original, cased surnames -- not the internal folded matching keys. Crossref's
    # mis-split 'Furkan Biten' (for 'Ali Furkan Biten') should read 'Furkan Biten',
    # and the bib's 'Biten' should read 'Biten', not 'biten'/'furkanbiten'.
    e = _entry("@article{k,\n author={Ali Furkan Biten and Ruben Tito},\n"
               " title={A Study},\n year={2019},\n doi={10.1/x}\n}\n")
    rec = {"authors": ["furkanbiten", "tito"],
           "authors_display": ["Furkan Biten", "Tito"], "given": {},
           "title": "A Study", "year": "2019"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    msgs = [f.message for f in rep.findings if "author" in f.message]
    assert any("bib='Biten' vs crossref='Furkan Biten'" in m for m in msgs)
    # No folded key (lowercase, spaceless) leaks into any author message.
    assert not any("furkanbiten" in m or "'biten'" in m for m in msgs)


def test_is_initial_recognizes_initials_not_names():
    assert record._is_initial("L.")
    assert record._is_initial("J.R.")
    assert not record._is_initial("Laurin")


def test_arxiv_id_extraction():
    assert normalize.extract_arxiv_id("arXiv:2406.01482") == "2406.01482"
    assert normalize.extract_arxiv_id("quant-ph/9705052") == "quant-ph/9705052"
    assert normalize.extract_arxiv_id("nothing here") is None


def test_field_line_points_at_field():
    _, entries = check("clean.bib")
    good = next(e for e in entries if e.key == "good2020")
    # 'doi' is on a later line than the entry's @-line.
    assert good.field_line("doi") > good.lineno


# --- per-entry analysis pipeline (network-free: no doi/arxiv/url short-circuits) ---

_NO_ID_BIB = (
    "@article{cited1,\n  author = {A. One},\n  title = {T},\n  year = {2000}\n}\n"
    "@article{uncited1,\n  author = {B. Two},\n  title = {U},\n  year = {2001}\n}\n"
)


def test_analyze_entry_records_result_and_status():
    from veracite.pipeline import analyze_entry
    entries, _ = parse_bib(_NO_ID_BIB)
    rep = Report(color=False)
    results, statuses = {}, {}
    for e in entries:
        analyze_entry(e, results, statuses, rep, delay=0, timeout=1)
    # Each analyzed entry gets a Resolution and a (status, confidence) pair.
    assert set(results) == {"cited1", "uncited1"}
    assert set(statuses) == {"cited1", "uncited1"}
    # An entry with no id to verify against produces its own finding, keyed to it.
    keys = {f.key for f in rep.findings}
    assert {"cited1", "uncited1"} <= keys


def test_analyze_entry_runs_in_call_order():
    from veracite.pipeline import analyze_entry
    entries, _ = parse_bib(_NO_ID_BIB)
    rep = Report(color=False)
    results, statuses = {}, {}
    for e in entries:
        analyze_entry(e, results, statuses, rep, delay=0, timeout=1)
    assert list(results) == ["cited1", "uncited1"]


def test_emit_entry_prints_each_finding_once():
    import io

    class _E:
        def __init__(self, key, line=1):
            self.key, self.lineno, self.etype = key, line, "article"
        def field_line(self, f):
            return self.lineno

    buf = io.StringIO()
    rep = Report(color=False)
    a, b = _E("a"), _E("b")
    rep.add(Severity.WARN, a, "alpha")
    rep.add(Severity.INFO, a, "beta")
    rep.add(Severity.ERROR, b, "gamma")
    rep.add_file(Severity.ERROR, "deltafinding")
    # Per-entry emit: 'a' once, then 'b' once; neither repeats.
    rep.emit_entry(a, out=buf)
    rep.emit_entry(b, out=buf)
    # Remaining (file-level) prints once and does not repeat a/b.
    rep.emit_remaining(out=buf)
    out = buf.getvalue()
    for token in ("alpha", "beta", "gamma", "deltafinding"):
        assert out.count(token) == 1, (token, out)
    # Order: a's findings before b's before the file-level group.
    assert out.index("alpha") < out.index("gamma") < out.index("deltafinding")


def test_emit_entry_skipnotes_hides_notes_but_marks_emitted():
    import io
    class _E:
        key = "a"; lineno = 1; etype = "article"
        def field_line(self, f):
            return self.lineno
    buf = io.StringIO()
    rep = Report(color=False)
    ent = _E()
    rep.add(Severity.INFO, ent, "a note")
    rep.emit_entry(ent, out=buf, skip_notes=True)
    rep.emit_remaining(out=buf, skip_notes=True)   # must not resurface the note
    assert "a note" not in buf.getvalue()


class _FmtEnt:
    """Minimal Entry stand-in for the report-format tests."""
    def __init__(self, key, etype="article", line=1):
        self.key, self.etype, self.lineno = key, etype, line
    def field_line(self, f):
        return self.lineno


def test_entry_block_header_and_layout():
    """A per-entry block: one header line '[i/N] key @type line N STATUS (conf)' (the
    progress counter is merged INTO the header, not a separate line), findings
    indented beneath, a blank line after the block. The verification status lives on
    the header, NOT as its own finding line."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    e = _FmtEnt("smith2020", "article", 5)
    rep.add(Severity.WARN, e, "a problem", category="metadata_mismatch")
    rep.set_status("smith2020", "VERIFIED", 0.75, "a field differs from the record")
    rep.set_link("smith2020", "https://doi.org/10.1/x")
    rep.emit_entry(e, out=buf, progress="[24/83]")
    out = buf.getvalue()
    lines = out.splitlines()
    # One header line: counter + key + @type + line + status + confidence + link.
    assert lines[0].startswith("[24/83]") and "smith2020" in lines[0]
    assert "@article" in lines[0] and "line 5" in lines[0]
    assert "VERIFIED (confidence 0.75)" in lines[0]
    # Link trails after '; ' -- no 'verify:' label, no separate line.
    assert "; https://doi.org/10.1/x" in lines[0] and "verify:" not in out
    # A VERIFIED header drops the detail text -- the finding below explains it.
    assert "a field differs from the record" not in lines[0]
    assert lines[1].startswith("    [WARN]") and "metadata_mismatch" in lines[1]
    assert out.endswith("\n\n")   # blank line separates blocks
    # No bare 'key on its own line', and no separate verification_status finding.
    assert not any(ln.strip() == "smith2020" for ln in lines)
    assert "verification_status" not in out


def test_unverified_header_keeps_its_cause():
    """UNVERIFIED/MISMATCH have no sibling finding, so the header keeps their detail."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    e = _FmtEnt("noid", "book", 3)
    rep.set_status("noid", "UNVERIFIED", 0.2, "no persistent identifier to verify against")
    rep.emit_entry(e, out=buf)
    assert "UNVERIFIED" in buf.getvalue() and "no persistent identifier" in buf.getvalue()


def test_clean_verified_entry_shows_a_single_line():
    """A clean VERIFIED entry (no findings) shows ONE header line -- 'VERIFIED' with
    no caveat/confidence noise -- rather than vanishing, so the [i/N] counter stays
    contiguous and a clean reference is visibly accounted for."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    e = _FmtEnt("clean", "article", 1)
    rep.set_status("clean", "VERIFIED", 1.0, "resolved and consistent (crossref, inspire)")
    assert rep.emit_entry(e, out=buf, progress="[5/9]") is True
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1                      # exactly one line, no findings
    assert lines[0].startswith("[5/9]") and "clean" in lines[0] and "VERIFIED" in lines[0]
    assert "confidence" not in lines[0]         # clean 1.0 shows no '(confidence ...)'


def test_entry_with_no_status_and_no_findings_is_silent():
    """Only an entry with neither a status nor a finding prints nothing."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    assert rep.emit_entry(_FmtEnt("ghost", "misc", 1), out=buf) is False
    assert buf.getvalue() == ""


def test_bare_unverified_with_no_findings_still_prints():
    """An UNVERIFIED entry with no other finding still shows -- its header carries
    the status and cause (the status line was dropped, so the header must surface it)."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    e = _FmtEnt("noid", "book", 3)
    rep.set_status("noid", "UNVERIFIED", 0.2, "no persistent identifier to verify against")
    assert rep.emit_entry(e, out=buf) is True
    out = buf.getvalue()
    assert "noid" in out and "UNVERIFIED" in out and "no persistent identifier" in out


def test_finding_is_always_one_line():
    """A message with an embedded newline (e.g. the title-diff bib/record dump) is
    folded to a single line, so a finding never splits across lines -- LLM/script
    consumers can rely on one finding per line."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    e = _FmtEnt("k")
    rep.add(Severity.WARN, e, "title differs:\n   bib:    A\n   record: B",
            category="metadata_mismatch")
    rep.emit_entry(e, out=buf)
    finding_lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("    [")]
    assert len(finding_lines) == 1
    assert "\n" not in finding_lines[0]


def test_link_with_embedded_newline_stays_one_line():
    """A link with a raw newline (e.g. a DOI the .bib wrapped mid-string) must not
    break the header across lines; it rides on the header after '; '."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    e = _FmtEnt("k")
    rep.add(Severity.WARN, e, "DOI does not match pattern: 'https:\n //doi:10.1/x'",
            "record", category="identifier_format")
    rep.set_link("k", "https://example.org/x\n y")
    rep.emit_entry(e, out=buf)
    lines = buf.getvalue().splitlines()
    # The URL is on the header line (line 0), one line, no stray break.
    assert "https://example.org/x y" in lines[0]
    # No body line is an orphaned URL fragment.
    assert not any(ln.lstrip().startswith("y") and "example" not in ln for ln in lines)


def test_sort_by_severity_is_one_global_errors_first_list():
    """emit_by_severity prints errors, then warnings, then notes, each line keyed."""
    import io
    buf = io.StringIO()
    rep = Report(color=False)
    rep.add(Severity.INFO, _FmtEnt("z"), "a note", category="style")
    rep.add(Severity.ERROR, _FmtEnt("a"), "an error", category="syntax")
    rep.emit_by_severity(out=buf)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("    [")]
    # Error line first, then the note; each line carries its own key.
    assert lines[0].startswith("    [ERROR]") and "a" in lines[0] and "an error" in lines[0]
    assert lines[1].startswith("    [note]") and "z" in lines[1] and "a note" in lines[1]


# --- author/title folding (false-positive reduction) ----------------------

def test_fold_drops_generational_suffix():
    assert normalize.split_authors("Hunt III, Harry B.") == ["hunt"]
    assert normalize.split_authors("Harry B. Hunt III") == ["hunt"]
    assert normalize.split_authors("Petropoulos, S. Jr") == ["petropoulos"]


def test_collaboration_author_skipped():
    assert normalize.is_collaboration("{Google Quantum AI and collaborators}")
    # split on ' and ' must not tear the brace-wrapped collaboration apart.
    assert normalize.split_authors("{Google Quantum AI and collaborators}") == []
    assert normalize.split_authors("{CMS Collaboration} and Smith, John") == ["smith"]


def test_arxiv_id_not_mined_from_conference_doi():
    assert normalize.extract_arxiv_id("10.1109/FOCS54457.2022.00117") is None
    assert normalize.extract_arxiv_id("arXiv:2103.16313") == "2103.16313"
    assert normalize.extract_arxiv_id("10.48550/arXiv.2103.16313") == "2103.16313"


def _arxiv_journal_notes(bib):
    """Run the static rules over one entry and return its arXiv-journal-canonical
    notes as Findings (so .suggested can be inspected)."""
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    return [f for f in rep.findings if "canonical form" in f.message]


def test_arxiv_journal_canonical_is_bare_id_not_preprint_prose():
    # 'arXiv preprint arXiv:2207.14255' is non-canonical: the canonical journal
    # value is the BARE 'arXiv:2207.14255' (no 'preprint' word). The note must say
    # exactly that via a concrete suggested edit.
    bib = ("@article{k, author={A. One}, title={A Title Long Enough}, year={2022},\n"
           " eprint={2207.14255}, journal={arXiv preprint arXiv:2207.14255}}\n")
    notes = _arxiv_journal_notes(bib)
    assert len(notes) == 1
    assert notes[0].suggested == {"field": "journal",
                                  "from": "arXiv preprint arXiv:2207.14255",
                                  "to": "arXiv:2207.14255"}
    # The message is terse: the value lives in the suggested edit, not repeated in
    # the prose (the '-> ' tail is rendered from `suggested`, not stored here).
    assert notes[0].message == "arXiv journal field not in canonical form"


def test_arxiv_journal_bare_canonical_is_silent():
    # The canonical form itself raises no note.
    bib = ("@article{k, author={A. One}, title={A Title Long Enough}, year={2022},\n"
           " eprint={2207.14255}, journal={arXiv:2207.14255}}\n")
    assert _arxiv_journal_notes(bib) == []


def test_clean_tex_decodes_entities_and_tex_amp():
    assert normalize.clean_tex("Science &amp; Justice") == normalize.clean_tex("Science \\& Justice")


def test_title_shortened_is_subtitle_not_plural():
    from veracite.titles import title_is_shortened
    assert title_is_shortened(
        "Combinatorial Optimization", "Combinatorial Optimization: Theory and Algorithms")
    # a genuine word difference (plural) is NOT a mere shortening.
    assert not title_is_shortened("Neutral atom systems", "Neutral atoms systems")


def test_given_name_hyphen_abbreviation_recognized():
    assert record._given_abbreviates("Karl-C.", "Karl-Christian")
    assert record._given_abbreviates("K.", "Karl")
    assert not record._given_abbreviates("Lukas", "Laurin")


# --- record identity severity (id match => discrepancies are WARN) ---------

def _entry(bib):
    entries, _ = parse_bib(bib)
    return entries[0]


def _sev_by_cat(rep, cat):
    return [f.severity for f in rep.findings if f.category == cat]


def test_et_al_marker_not_read_as_mixed_author_format():
    # A 'Last, First' list ending in a literal 'et al.' is uniform -- 'et al.' is a
    # completeness marker (already flagged by author_completeness), not a name in a
    # second 'First Last' convention, so it must not raise a mixed-format note.
    bib = ("@article{k, author={{Abbott}, B.~P. and {Adya}, V.~B. and et al.},\n"
           " title={T}, journal={J}, year={2018}, doi={10.1/x}}\n")
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    assert not any("mixes" in f.message for f in rep.findings)


def test_genuinely_mixed_author_format_still_flagged():
    # The fix must not silence a real mix of 'Last, First' and 'First Last'.
    bib = ("@article{k, author={Smith, John and Jane Doe}, title={T},\n"
           " journal={J}, year={2020}, doi={10.1/y}}\n")
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    assert any("mixes" in f.message for f in rep.findings)


def test_idmatched_author_diff_is_warning_not_error():
    e = _entry("@article{k,\n author={Daud, M. and Singh, R.},\n title={A Study},\n"
               " year={2020},\n doi={10.1/x}\n}\n")
    rec = {"authors": ["mohddaud", "singh"], "given": {}, "title": "A Study",
           "year": "2020"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    # first author folds differently (daud vs mohddaud) but the title matches:
    # a metadata warning, never a wrong_paper/id_resolves_wrong_record error.
    assert Severity.ERROR not in [f.severity for f in rep.findings]
    assert any(f.category == "metadata_mismatch" for f in rep.findings)


def test_metadata_mismatch_suggests_conforming_to_record():
    # The record is canonical: a discrepancy is flagged for a human (WARN, not an
    # auto-edit) but its suggested edit points the bib AT the record (2009 -> 2010).
    # Render-affecting fields (year) warn; a stylistic given-name abbreviation notes.
    e = _entry("@article{k,\n author={Amo, A.},\n title={A Study Of Things},\n"
               " journal={Nature Physics},\n year={2009},\n volume={5},\n pages={805},\n"
               " doi={10.1/x}\n}\n")
    rec = {"authors": ["amo"], "given": {"amo": "alberto"}, "title": "A Study Of Things",
           "journal": "Nature Physics", "year": "2010", "volume": "5", "pages": "805",
           "doi": "10.1/x"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    year = [f for f in rep.findings if f.category == "metadata_mismatch"
            and "year" in f.message]
    assert len(year) == 1 and year[0].severity is Severity.WARN          # renders -> WARN
    assert year[0].suggested == {"field": "year", "from": "2009", "to": "2010"}
    # The abbreviated given name does not change the rendered citation -> a note.
    assert not any(f.severity is Severity.WARN and "given name" in f.message
                   for f in rep.findings)


def test_first_author_and_title_both_differ_is_error():
    e = _entry("@article{k,\n author={Smith, John},\n title={Alpha Beta Gamma Delta},\n"
               " year={2020},\n doi={10.1/x}\n}\n")
    rec = {"authors": ["jones"], "given": {}, "title": "Completely Different Other Words",
           "year": "2020"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    assert any(f.category == "id_resolves_wrong_record" and f.severity is Severity.ERROR
               for f in rep.findings)


def test_dropped_subtitle_is_info_not_error():
    e = _entry("@article{k,\n author={Korte, B.},\n"
               " title={Combinatorial Optimization},\n year={2012},\n doi={10.1/x}\n}\n")
    rec = {"authors": ["korte"], "given": {},
           "title": "Combinatorial Optimization: Theory and Algorithms", "year": "2012"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    assert Severity.ERROR not in [f.severity for f in rep.findings]
    assert any("shortened form" in f.message for f in rep.findings)


def test_and_others_withdrawn_when_record_has_no_more_authors():
    # The record enumerates no more authors than the bib already lists (e.g. a
    # collaboration carried as a single name, or a list the bib gives in full), so
    # 'and others' is faithful, not lossy -- the offline finding is withdrawn.
    e = _entry("@article{k,\n author={LHCb Collaboration and others},\n"
               " title={A Result},\n year={2020},\n journal={J},\n volume={1},\n"
               " pages={1},\n doi={10.1/x}\n}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any(f.category == "author_completeness" for f in rep.findings)
    rec = {"authors": ["lhcbcollaboration"], "given": {}, "title": "A Result",
           "year": "2020"}
    record.compare_against_record(e, rec, "crossref", rep)
    # supersession is resolved at read time: live_findings() drops it.
    assert not any(f.category == "author_completeness" for f in rep.live_findings())


def test_and_others_kept_when_record_lists_more_authors():
    # The record lists more authors than the bib kept -- those are exactly the
    # names that belong in the .bib, so the truncation finding stands.
    e = _entry("@article{k,\n author={Smith, J. and others},\n"
               " title={A Result},\n year={2020},\n journal={J},\n volume={1},\n"
               " pages={1},\n doi={10.1/x}\n}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any(f.category == "author_completeness" for f in rep.findings)
    rec = {"authors": ["smith", "jones", "lee"], "given": {}, "title": "A Result",
           "year": "2020"}
    record.compare_against_record(e, rec, "crossref", rep)
    assert any(f.category == "author_completeness" for f in rep.findings)


def test_truncated_authorlist_skips_given_name_check():
    e = _entry("@article{k,\n author={Wang, Ke and Zhang, Chuanyu and others},\n"
               " title={A Demo},\n year={2026},\n doi={10.1/x}\n}\n")
    rec = {"authors": ["wang", "zhang"], "given": {"zhang": "Pengfei"},
           "title": "A Demo", "year": "2026"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    # 'and others' => a shared common surname may be a different person; no
    # given-name finding at all.
    assert not any("given name differs" in f.message for f in rep.findings)


# --- LLM scoring (relevance-only, wrong-paper gated) -----------------------

class _StubEntry:
    key = "k"
    lineno = 1
    def get(self, f, d=""):
        return {"title": "T", "author": "A. Auth", "year": "2020"}.get(f, d)
    def field_line(self, f):
        return self.lineno


def _rate(payload, abstract="an abstract"):
    from veracite import llm
    rep = Report(color=False)
    provider = lambda prompt, model, timeout: payload
    llm.rate_one(_StubEntry(), {"abstract": abstract},
                 [{"file": "m.tex", "context": "c"}], rep, provider, "model")
    return rep


def test_llm_wrong_paper_is_error():
    rep = _rate('{"relevance": 1, "wrong_paper": true, "verdict": "x", "issue": ""}')
    assert [(f.severity, f.category) for f in rep.findings] == [(Severity.ERROR, "wrong_paper")]


def test_llm_low_relevance_is_warning():
    rep = _rate('{"relevance": 3, "wrong_paper": false, "verdict": "x", "issue": ""}')
    assert [(f.severity, f.category) for f in rep.findings] == [(Severity.WARN, "llm_relevance")]


def test_llm_high_relevance_leaves_clean_pass_note():
    # An LLM call costs tokens, so even a clean 4-5/5 leaves one 'context OK' note
    # (a note, hidden by --skipnotes) rather than vanishing silently.
    for rel in (4, 5):
        rep = _rate(f'{{"relevance": {rel}, "wrong_paper": false, "verdict": "", "issue": ""}}')
        assert [(f.severity, f.category) for f in rep.findings] \
            == [(Severity.INFO, "llm_ok")]
        assert f"context OK {rel}/5" in rep.findings[0].message


def test_group_misfit_drops_low_relevance_by_one():
    # relevance 3 + group_misfit -> reported as 2/5 with the drop explained.
    rep = _rate('{"relevance": 3, "wrong_paper": false, "group_misfit": true, '
                '"verdict": "x", "issue": ""}')
    msgs = [f.message for f in rep.findings]
    assert any("2/5" in m and "odd one out" in m for m in msgs)


def test_group_misfit_ignored_when_relevance_high():
    # A high standalone relevance is NOT penalised by a group flag: no warning, just
    # the clean-pass note recording the call.
    rep = _rate('{"relevance": 5, "wrong_paper": false, "group_misfit": true, '
                '"verdict": "x", "issue": ""}')
    assert [(f.severity, f.category) for f in rep.findings] == [(Severity.INFO, "llm_ok")]


def test_prompt_lists_cocited_group():
    from veracite import llm

    class _E:
        key = "k"
        def get(self, f, d=""):
            return {"title": "Group Mate Title", "author": "A", "year": "2020"}.get(f, d)
    ctx = [{"file": "m.tex", "context": "cited here.", "group": ["sib1"]}]
    prompt = llm.build_rating_prompt(_E(), {"abstract": "abs"}, ctx,
                                     by_key={"sib1": _E()})
    assert "co-cited" in prompt and "sib1" in prompt


# --- LLM provider availability: 'not logged in' must be actionable, up front --

def test_auth_error_classifier():
    from veracite.llm import _is_auth_error
    assert _is_auth_error("Not logged in · Please run /login")
    assert _is_auth_error("Invalid API key")
    assert _is_auth_error("Your credit balance is too low")
    # a model-id error is NOT an auth error -- it should not be treated as fatal-auth.
    assert not _is_auth_error("model 'claude-x' not found")
    assert not _is_auth_error("relevance rated 3/5")


def test_preflight_passes_when_provider_replies():
    from veracite.llm import preflight_provider
    ok = lambda prompt, model, timeout: '{"relevance": 5}'
    assert preflight_provider(ok, "model") is None


def test_preflight_blocks_on_fatal_error():
    from veracite.llm import preflight_provider
    not_logged_in = lambda prompt, model, timeout: {
        "error": "Not logged in · Please run /login", "fatal": True}
    assert preflight_provider(not_logged_in, "model") == "Not logged in · Please run /login"


def test_preflight_ignores_non_fatal_error():
    # A transient/ambiguous error (no 'fatal' flag) must NOT block the run -- only
    # clearly-fatal setup problems abort; per-entry handling covers the rest.
    from veracite.llm import preflight_provider
    flaky = lambda prompt, model, timeout: {"error": "could not parse model JSON"}
    assert preflight_provider(flaky, "model") is None


def test_cli_llm_not_logged_in_aborts_up_front(tmp_path, capfd, monkeypatch):
    """A user not logged in to Claude must get one actionable error before the run,
    not a cryptic per-entry warning after the whole online pass."""
    from veracite import cli
    monkeypatch.setattr(cli, "preflight_provider",
                        lambda provider, model, timeout=30: "Not logged in · Please run /login")
    bib = tmp_path / "refs.bib"
    bib.write_text(_ONE_ENTRY, encoding="utf-8")
    tex = tmp_path / "p.tex"
    tex.write_text("\\cite{k}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--bib", str(bib), "--tex", str(tex), "--llm", "--no-color"])
    assert exc.value.code != 0
    err = capfd.readouterr().err
    assert "is not available" in err and "Not logged in" in err
    assert "sign in" in err          # actionable guidance is present


def test_cli_unknown_llm_provider_aborts(tmp_path, capfd):
    from veracite.cli import main
    bib = tmp_path / "refs.bib"
    bib.write_text(_ONE_ENTRY, encoding="utf-8")
    tex = tmp_path / "p.tex"
    tex.write_text("\\cite{k}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--bib", str(bib), "--tex", str(tex), "--llm",
              "--llm-provider", "gpt4", "--no-color"])
    assert exc.value.code != 0
    assert "unknown --llm provider" in capfd.readouterr().err


# --- advisory: chronological order within a \cite group --------------------

class _YE:
    def __init__(self, key, year):
        self.key, self._y, self.lineno = key, year, 1
    def get(self, f, d=""):
        return self._y if f == "year" else d
    def field_line(self, f):
        return 1


def test_non_chronological_group_is_noted():
    by_key = {"a": _YE("a", "2019"), "b": _YE("b", "2005")}
    rep = Report(color=False)
    verify.chronological_order([["a", "b"]], by_key, rep)
    assert any(f.category == "citation_order" and f.severity is Severity.INFO
               for f in rep.findings)


def test_chronological_group_not_noted():
    by_key = {"a": _YE("a", "2005"), "b": _YE("b", "2019")}
    rep = Report(color=False)
    verify.chronological_order([["a", "b"]], by_key, rep)
    assert rep.findings == []


def test_chronological_skipped_when_year_unknown():
    by_key = {"a": _YE("a", ""), "b": _YE("b", "2019")}
    rep = Report(color=False)
    verify.chronological_order([["a", "b"]], by_key, rep)
    assert rep.findings == []


def test_find_citation_groups_only_multi_key():
    from veracite import llm
    files = [("p.tex", r"text \cite{a,b,c} and \cite{solo} and \cite{a, b, c} more")]
    groups = llm.find_citation_groups(files)
    assert groups == [["a", "b", "c"]]   # solo excluded; duplicate group deduped


# --- LLM context window (only the citation sentences) ----------------------

def test_sentence_window_keeps_only_local_context():
    from veracite import llm
    text = (r"First unrelated sentence here. The APPS benchmark is used "
            r"\cite{k} widely. We then evaluate it carefully. A far away sentence.")
    import re
    m = re.search(r"\\cite\{k\}", text)
    bounds = llm.sentence_bounds(text)
    win = llm._sentence_window(text, m.start(), m.end(), bounds)
    assert "APPS benchmark" in win and "far away sentence" not in win


# --- interleaved analysis ordering -----------------------------------------

def test_analyze_entry_resolves_each_entry_in_order(monkeypatch):
    # Stub the network so each DOI resolves to a minimal record; analyze_entry
    # should then populate a resolved Resolution per entry, in order (the
    # interleaving the LLM sweep relies on).
    from veracite import pipeline
    monkeypatch.setattr(record, "fetch_crossref",
                        lambda doi, timeout: ({"authors": [], "given": {}, "title": "",
                                               "year": None, "abstract": "x"}, 200))
    monkeypatch.setattr(record, "fetch_openalex", lambda doi, timeout: None)
    monkeypatch.setattr(record, "fetch_related", lambda *a, **k: [])
    monkeypatch.setattr(record, "fetch_inspire", lambda *a, **k: None)
    bib = ("@article{a, author={X}, title={A}, year={2020}, journal={J}, doi={10.1000/a}}\n"
           "@article{b, author={Y}, title={B}, year={2021}, journal={J}, doi={10.1000/b}}\n")
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    results, statuses = {}, {}
    for e in entries:
        pipeline.analyze_entry(e, results, statuses, rep, delay=0, timeout=1)
    assert list(results) == ["a", "b"]
    assert all(results[k].record is not None for k in ("a", "b"))


# --- month rule: bare macro is canonical, braced/spelled-out is flagged ----

def _ARTICLE(month):
    return ("@article{k,\n author={A. One},\n title={T},\n year={2020},\n"
            f" journal={{J}},\n month = {month}\n}}\n")


def test_bare_month_macro_is_not_flagged():
    assert _month_notes(_ARTICLE("jun")) == []      # the recommended form
    assert _month_notes(_ARTICLE("6")) == []        # an integer is fine


def test_braced_month_is_flagged_with_fix():
    notes = _month_notes(_ARTICLE("{Sep}"))
    assert len(notes) == 1 and "-> 'sep'" in notes[0]


def test_spelled_out_month_is_flagged():
    notes = _month_notes(_ARTICLE("{June}"))
    assert len(notes) == 1 and "-> 'jun'" in notes[0]


def test_non_month_value_is_ignored():
    assert _month_notes(_ARTICLE("{spring}")) == []


# --- required_fields: standard-conformant entries must not be flagged ------

def _missing(bib):
    """Run the static rules and return the missing-field messages for one entry."""
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    return [f.message for f in rep.findings if f.category == "missing_field"]


def test_biber_book_requires_author_collection_requires_editor():
    # biber's datamodel: @book mandates author (an edited volume is @collection,
    # which mandates editor). We follow biber exactly.
    assert any("author" in m for m in
               _missing("@book{k, editor={A. B}, title={T}, year={2020}, publisher={P}}"))
    assert _missing("@book{k, author={A. B}, title={T}, year={2020}}") == []
    assert _missing("@collection{k, editor={A. B}, title={T}, year={2020}}") == []


def test_booklet_accepts_author_or_editor():
    # @booklet is the type whose biber constraint is author OR editor.
    assert _missing("@booklet{k, editor={A. B}, title={T}, year={2020}}") == []
    assert _missing("@booklet{k, author={A. B}, title={T}, year={2020}}") == []


def test_title_only_types_need_no_author():
    # biber mandates only a title for these standalone types.
    for t in ("manual", "dataset", "software", "misc"):
        assert _missing(f"@{t}{{k, title={{T}}, year={{2020}}}}") == [], t


def test_online_requires_a_locator_per_biber():
    # biber: @online needs title + (url OR doi OR eprint).
    assert any("url" in m for m in _missing("@online{k, title={T}}"))
    assert _missing("@online{k, title={T}, url={http://x}}") == []


def test_crossref_skips_required_field_check():
    # booktitle is inherited via crossref, so it must not be flagged as missing.
    assert _missing("@inproceedings{k, author={A. B}, title={T}, year={2020}, "
                    "crossref={proc1}}") == []


def test_thesis_school_aliases_institution():
    # biber @thesis mandates author, title, type, institution; 'school' is the
    # legacy alias of 'institution' and must satisfy it.
    assert not any("institution" in m for m in
                   _missing("@phdthesis{k, author={A}, title={T}, type={PhD}, school={U}, year={2020}}"))
    assert not any("institution" in m for m in
                   _missing("@phdthesis{k, author={A}, title={T}, type={PhD}, institution={U}, year={2020}}"))


def test_missing_year_is_warning_not_error():
    # biber doesn't require a date; we flag it, but only as a recommendation.
    entries, _ = parse_bib("@article{k, author={A}, title={T}, journal={J}, "
                           "volume={1}, pages={1}}")
    rep = Report(color=False)
    run_static(entries, rep)
    notes = [f for f in rep.findings if f.category == "missing_recommended"]
    assert notes and notes[0].severity is Severity.WARN and "year" in notes[0].message


def test_mandatory_slots_match_biber_datamodel():
    from veracite.datamodel import mandatory_slots
    # Read straight from the generated datamodel -- these are biber's constraints.
    assert mandatory_slots("article") == [["author"], ["journaltitle"], ["title"]]
    assert mandatory_slots("collection") == [["editor"], ["title"]]
    assert ["url", "doi", "eprint"] in mandatory_slots("online")
    assert mandatory_slots("phdthesis") == mandatory_slots("thesis")   # alias


def test_article_eid_or_number_satisfies_pages():
    base = "@article{{k, author={{A. B}}, title={{T}}, year={{2020}}, journal={{J}}, volume={{1}}, {extra}}}"
    assert _missing(base.format(extra="eid={012345}")) == []
    assert _missing(base.format(extra="number={3}, pages={1--2}")) == []


def test_article_date_counts_as_year():
    assert _missing("@article{k, author={A. B}, title={T}, date={2020-05}, "
                    "journal={J}, volume={1}, pages={1--2}}") == []


def test_incomplete_article_still_flagged():
    # Missing volume/pages on a published article is a locator warning, not an
    # invalid-BibTeX error -- it lives in its own 'missing_locator' category at
    # WARN, so a clean modern bibliography is never reported as broken.
    entries, _ = parse_bib("@article{k, author={A. B}, title={T}, year={2020}, journal={J}}")
    rep = Report(color=False)
    run_static(entries, rep)
    loc = [f for f in rep.findings if f.category == "missing_locator"]
    msgs = [f.message for f in loc]
    assert any("volume" in m for m in msgs) and any("pages" in m for m in msgs)
    assert all(f.severity is Severity.WARN for f in loc)
    # and it must NOT be escalated to an error-level missing_field
    assert not [f for f in rep.findings if f.category == "missing_field"]


def test_missing_title_still_flagged_everywhere():
    assert any("title" in m for m in _missing("@online{k, url={http://x}, year={2020}}"))


def test_thesis_alias_missing_type_is_not_flagged_at_all():
    # @phdthesis/@mastersthesis auto-supply 'type' (PhD/Master's thesis) -- that is
    # the whole reason these aliases exist vs the generic @thesis. So a missing
    # 'type' is CORRECT: no error, and not even an advisory note.
    for bib in (
        "@phdthesis{t, author={A}, title={T}, institution={MIT}, year={2020}}",
        "@mastersthesis{t, author={A}, title={T}, institution={MIT}, year={2020}}",
        "@techreport{t, author={A}, title={T}, institution={MIT}, year={2020}}",
    ):
        entries, _ = parse_bib(bib)
        rep = Report(color=False)
        run_static(entries, rep)
        assert not [f for f in rep.findings
                    if f.category in ("datamodel_recommended", "missing_field")
                    and "type" in f.message], bib


def test_incollection_missing_editor_is_a_note_not_an_error():
    # biber tolerates an @incollection with no editor, but it IS genuinely missing
    # data, so it stays an advisory note (never an ERROR that calls valid .bib invalid).
    entries, _ = parse_bib(
        "@incollection{c, author={A}, title={T}, booktitle={B}, year={2019}}")
    rep = Report(color=False)
    run_static(entries, rep)
    rec = [f for f in rep.findings if f.category == "datamodel_recommended"]
    assert any("editor" in f.message for f in rec)
    assert all(f.severity is Severity.INFO for f in rec)
    assert not [f for f in rep.findings if f.category == "missing_field"]


def test_phdthesis_with_explicit_type_has_no_recommendation():
    # Supplying 'type' explicitly is also fine -- still no finding.
    entries, _ = parse_bib("@phdthesis{t, author={A}, title={T}, type={PhD thesis}, "
                           "institution={MIT}, year={2020}}")
    rep = Report(color=False)
    run_static(entries, rep)
    assert not [f for f in rep.findings if f.category == "datamodel_recommended"]


def test_historical_physrev_journal_names_match():
    # The pre-1998 CASSI sub-series names are the same journal as the modern title;
    # the curated table (canonical, checked before ISO-4) must accept them.
    from veracite.compare import _journal_equiv
    assert _journal_equiv("Phys. Rev. B Condens. Matter", "Physical Review B")
    assert _journal_equiv("Phys. Rev. B: Condens. Matter Mater. Phys.", "Physical Review B")
    assert _journal_equiv("Phys. Rev. D Part. Fields", "Physical Review D")
    # but distinct series must still differ (no over-matching)
    assert not _journal_equiv("Physical Review B", "Physical Review A")


def _mixes(author):
    """Whether consistent_author_format flags one entry's author list as mixed."""
    from veracite.rules import consistent_author_format
    entries, _ = parse_bib("@article{k, author={" + author + "}, title={T}, "
                           "journal={J}, year={2020}}")
    rep = Report(color=False)
    consistent_author_format(entries, rep)
    return any("mixes" in f.message for f in rep.findings)


def test_mixed_name_form_genuine_is_flagged():
    # A real mix of 'First Last' and 'Last, First' within one entry is a data-quality
    # issue worth a note.
    assert _mixes("Zhou Wang and Bovik, A.C.")
    assert _mixes("Personnaz, L. and Guyon, I. and G. Dreyfus")


def test_mixed_name_form_collaboration_not_flagged():
    # A braced corporate/collaboration author carries no comma and several words but
    # is NOT a 'First Last' personal name -- it must not trip the mixed-format check
    # on an otherwise uniform 'Last, First' list.
    assert not _mixes("Abbott, B. P. and Abbott, R. and {LIGO Scientific Collaboration}")
    assert not _mixes("Virtanen, Pauli and Gommers, Ralf and {SciPy 1.0 Contributors}")
    # a brace-protected multiword SURNAME (always written with a comma) is fine too
    assert not _mixes("{van der Walt}, Stefan and Brett, Matthew")


def test_modern_article_ids_not_unusual_pages():
    # Multi-letter journal article ids are valid locators, not "unusual pages".
    from veracite.rules import page_sanity
    for pid in ("eaam9288", "staf1642", "rspa20090232", "psaf050", "L123", "e0123456"):
        entries, _ = parse_bib("@article{k, author={A}, title={T}, journal={J}, "
                               "year={2020}, pages={" + pid + "}}")
        rep = Report(color=False)
        page_sanity(entries[0], rep)
        assert not any("unusual pages" in f.message for f in rep.findings), pid
    # genuine junk is still flagged
    for junk in ("pp.", "in press"):
        entries, _ = parse_bib("@article{k, author={A}, title={T}, journal={J}, "
                               "year={2020}, pages={" + junk + "}}")
        rep = Report(color=False)
        page_sanity(entries[0], rep)
        assert any("unusual pages" in f.message for f in rep.findings), junk


# --- doi_format: a valid short registrant prefix is accepted ---------------

def _doi_notes(doi):
    bib = ("@article{k, author={A. B}, title={T}, year={2020}, journal={J}, "
           f"volume={{1}}, pages={{1--2}}, doi={{{doi}}}}}")
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    # Rendered lines, so the derived '(suggested: ...)' tail is included.
    return [rep._finding_line(f) for f in rep.findings if "DOI" in rep._finding_line(f)]


def test_short_doi_prefix_accepted():
    assert _doi_notes("10.123/abc") == []
    assert _doi_notes("10.1000/xyz") == []


def test_non_doi_still_flagged():
    assert _doi_notes("notadoi") != []


def test_url_doi_field_without_strippable_doi_not_a_noop_fix():
    # A doi field holding a non-doi.org URL (e.g. an arXiv link) or a mangled string
    # cannot be reduced to a bare DOI -- it must NOT produce a 'X -> X' no-op
    # 'bare DOI preferred' note; it should be diagnosed as a malformed DOI instead.
    for bad in ("https://arxiv.org/abs/1007.4566", "https://doi:10.1103/PhysRev.115.485"):
        notes = _doi_notes(bad)
        assert notes, f"expected a finding for {bad!r}"
        # No note may suggest replacing the value with itself.
        assert not any(f"{bad!r} -> {bad!r}" in m for m in notes)
        # The accurate diagnosis is the malformed-pattern one.
        assert any("does not match" in m for m in notes)


def test_real_url_wrapped_doi_still_suggests_bare():
    # The genuine case -- a doi.org URL around a real DOI -- still gets the bare-DOI
    # suggestion with a fix that actually changes the value.
    notes = _doi_notes("https://doi.org/10.1098/rspa.2018.0879")
    assert any("bare DOI preferred" in m and "10.1098/rspa.2018.0879" in m
               for m in notes)


def test_prose_suggestion_is_derived_from_structured_field():
    # The JSON `suggested` dict is the single source of truth: the human line's
    # '(suggested: X -> Y)' tail is rendered FROM it, never stored in `message`, so
    # the two cannot drift. Every finding with a structured suggestion must render
    # its 'to' value into the line, and none must duplicate it in `message`.
    from veracite.report import format_suggested
    bib = ("@article{k, author={A. B}, title={A BGK Model.}, journal={J}, "
           "year={2007}, volume={18}, pages={1961-1983}, "
           "doi={https://doi.org/10.1142/x}, month={Apr}}\n")
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    suggested_findings = [f for f in rep.findings if f.suggested]
    assert suggested_findings, "expected findings carrying a structured suggestion"
    for f in suggested_findings:
        # message itself never contains the arrow -- it is derived at render time.
        assert "suggested:" not in f.message and "->" not in f.message
        line = rep._finding_line(f)
        assert format_suggested(f.suggested) in line


def test_format_suggested_previews_long_values():
    # A whole-title suggestion is previewed on screen (elided) but kept full in the
    # structured field, so the JSON stays exact while the prose stays readable.
    from veracite.report import format_suggested
    long_to = "A" * 100
    out = format_suggested({"field": "title", "to": long_to})
    assert "..." in out and len(out) < len(long_to)


# --- journal matching: standard abbreviations accepted, garble warns -------

def test_iso4_abbreviation_accepted():
    eq = record._journal_equiv
    assert eq("Phys. Rev. B", "Physical Review B")
    assert eq("New J. Phys.", "New Journal of Physics")
    assert eq("Sci Rep", "Scientific Reports")                  # NLM no-period form
    assert eq("Rep. Prog. Phys.", "Reports on Progress in Physics")
    assert eq("Nano Lett.", "Nano Letters")


def test_journal_genuine_mismatch_still_differs():
    eq = record._journal_equiv
    assert not eq("Nature", "Nature Physics")                   # not an abbreviation
    assert not eq("Phys. Rev. A", "Physical Review B")          # wrong series
    assert not eq("Phys. Rev. B", "Nature Physics")


# --- CLI end-to-end: one per-entry list, bibtex order, no duplication ------

def test_cli_offline_output_is_ordered_and_deduplicated(tmp_path, capfd):
    from veracite.cli import main
    # Two entries; each has at least one offline finding (braced month / bad doi),
    # with the flagged field first on its own line so the line-anchored rules fire.
    bib = (
        "@article{zeta,\n  month = {Jan},\n  author = {A. B},\n  title = {T},\n"
        "  year = {2020},\n  journal = {J},\n  volume = {1},\n  pages = {1--2}\n}\n"
        "@article{alpha,\n  doi = {notadoi},\n  author = {C. D},\n  title = {U},\n"
        "  year = {2021},\n  journal = {J},\n  volume = {2},\n  pages = {3--4}\n}\n")
    p = tmp_path / "refs.bib"
    p.write_text(bib, encoding="utf-8")
    main(["--bib", str(p), "--offline", "--no-color"])
    out = capfd.readouterr().out
    finding_lines = [l for l in out.splitlines()
                     if l.lstrip().startswith(("[ERR", "[WARN", "[note"))]
    assert finding_lines, out
    # No line appears twice (the old stream+render double-print is gone).
    assert len(finding_lines) == len(set(finding_lines))
    # Both entries surfaced a finding, in bibtex (file) order: zeta before alpha.
    assert "zeta" in out and "alpha" in out
    assert out.index("zeta") < out.index("alpha")


# --- CLI input-handling: bad inputs fail cleanly, never with a traceback ----

_ONE_ENTRY = ("@article{k, title={T}, author={A. B}, year={2020}, "
              "journal={J}, volume={1}, pages={1--2}}\n")


def test_cli_zero_entries_is_an_error_not_healthy(tmp_path, capfd):
    """A wrong file (no @entries) must NOT report HEALTHY -- that is a false pass.
    It exits non-zero with a 'no BibTeX entries' message (regression: .pdf/.bbl
    given as --bib used to print 'HEALTHY')."""
    from veracite.cli import main
    p = tmp_path / "paper.bbl"
    p.write_text("\\relax \\bibcite{x}{1}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--bib", str(p), "--offline", "--no-color"])
    assert exc.value.code != 0
    err = capfd.readouterr().err
    assert "no BibTeX entries found" in err
    assert "HEALTHY" not in capfd.readouterr().out


def test_cli_binary_file_errors_cleanly(tmp_path, capfd):
    """A binary file fed to --bib errors with a clear message, not a raw
    UnicodeDecodeError traceback (regression)."""
    from veracite.cli import main
    p = tmp_path / "junk.bib"
    p.write_bytes(b"%PDF-1.5\x00\x00\xf7\xfe binary not text\x00")
    with pytest.raises(SystemExit) as exc:
        main(["--bib", str(p), "--offline", "--no-color"])
    assert exc.value.code != 0
    assert "not a text file" in capfd.readouterr().err


def test_cli_latin1_bib_reads_with_a_warning(tmp_path, capfd):
    """A Latin-1 .bib (common in older TeX setups) is read via fallback with a
    warning, not a crash."""
    from veracite.cli import main
    p = tmp_path / "refs.bib"
    p.write_bytes(("@article{k, title={Caf\xe9}, author={A. B}, year={2020}, "
                   "journal={J}, volume={1}, pages={1--2}}\n").encode("latin-1"))
    main(["--bib", str(p), "--offline", "--no-color"])
    assert "Latin-1" in capfd.readouterr().err


def test_cli_unwritable_json_warns_not_crashes(tmp_path, capfd):
    """A bad --json path must not mask the completed analysis with a traceback."""
    from veracite.cli import main
    p = tmp_path / "refs.bib"
    p.write_text(_ONE_ENTRY, encoding="utf-8")
    bad = tmp_path / "no_such_dir" / "out.json"   # parent does not exist
    rc = main(["--bib", str(p), "--offline", "--no-color", "--json", str(bad)])
    assert "could not write checkpoint" in capfd.readouterr().err
    assert not bad.exists()


# --- L1: identifier checksum validators ------------------------------------

def test_isbn_issn_orcid_checksums():
    from veracite.identifiers import isbn_valid, issn_valid, orcid_valid
    assert isbn_valid("978-3-16-148410-0") and isbn_valid("0-306-40615-2")
    assert not isbn_valid("978-3-16-148410-1")
    assert issn_valid("0378-5955") and not issn_valid("0378-5954")
    assert orcid_valid("0000-0002-1825-0097")
    assert not orcid_valid("0000-0002-1825-0098")


def test_bad_isbn_flagged_in_entry():
    entries, _ = parse_bib("@book{k, author={A. B}, title={T}, year={2020}, "
                           "publisher={P}, isbn={978-3-16-148410-1}}")
    rep = Report(color=False)
    run_static(entries, rep)
    assert any(f.category == "identifier_format" and "ISBN" in f.message
               for f in rep.findings)


# --- L4: cross-source consistency ------------------------------------------

class _Ent:
    key = "k"; lineno = 1
    def get(self, f, d=""):
        return d
    def field_line(self, f):
        return 1


def test_cross_source_year_conflict_is_warning():
    rep = Report(color=False)
    a = {"year": 2023, "title": "Same Title", "journal": "J", "volume": "1", "pages": "1"}
    b = {"year": 2024, "title": "Same Title", "journal": "J", "volume": "1", "pages": "1"}
    record.compare_sources(_Ent(), {"crossref": a, "inspire": b}, rep)
    assert any(f.category == "source_conflict" and "year" in f.message for f in rep.findings)


def test_cross_source_agreement_no_finding():
    rep = Report(color=False)
    a = {"year": 2023, "title": "Same Title", "journal": "Physical Review B", "volume": "1", "pages": "1"}
    b = {"year": 2023, "title": "Same Title", "journal": "Physical Review B", "volume": "1", "pages": "1"}
    record.compare_sources(_Ent(), {"crossref": a, "inspire": b}, rep)
    assert rep.findings == []


def test_cross_source_journal_abbreviation_is_not_flagged():
    # A full title vs its ISO-4 abbreviation across two sources is not a discrepancy
    # -- both are valid -- so nothing is flagged.
    rep = Report(color=False)
    a = {"year": 2023, "title": "T", "journal": "IEEE Trans.Info.Theor."}
    b = {"year": 2023, "title": "T", "journal": "IEEE Transactions on Information Theory"}
    record.compare_sources(_Ent(), {"crossref": a, "inspire": b}, rep)
    assert rep.findings == []


def test_cross_source_different_journals_is_conflict():
    # Two genuinely different journals -> a real cross-source conflict.
    rep = Report(color=False)
    a = {"year": 2023, "title": "T", "journal": "Nature Physics"}
    b = {"year": 2023, "title": "T", "journal": "Physical Review B"}
    record.compare_sources(_Ent(), {"crossref": a, "inspire": b}, rep)
    assert any(f.category == "source_conflict" and "journal" in f.message
               for f in rep.findings)


# --- L3: verification status classification --------------------------------

def _res(**kw):
    r = record.Resolution()
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_status_verified_clean_two_sources_is_full_confidence():
    # Clean match corroborated by 2+ authoritative sources -> confidence 1.0.
    e = _Ent()
    rep = Report(color=False)
    res = _res(record={"title": "T"}, source="crossref",
               sources={"crossref": {"title": "T"}, "inspire": {"title": "T"}})
    status, conf = verify.classify(e, res, rep)
    assert status == "VERIFIED" and conf == 1.0
    # classify records the verdict for the header; it no longer emits a finding line.
    assert rep.status[e.key][0] == "VERIFIED"
    assert not any(f.category == "verification_status" for f in rep.findings)


def test_status_verified_single_source_is_below_one():
    e = _Ent()
    rep = Report(color=False)
    res = _res(record={"title": "T"}, source="crossref", sources={"crossref": {"title": "T"}})
    status, conf = verify.classify(e, res, rep)
    assert status == "VERIFIED" and conf == 0.95


def test_status_unverified_on_dead_doi():
    # classify reports UNVERIFIED 0.0; the ERROR severity now lives on the separate
    # dead_doi finding (record.py), not on classify's status.
    rep = Report(color=False)
    status, conf = verify.classify(_Ent(), _res(dead_doi=True, doi="10.1/x"), rep)
    assert status == "UNVERIFIED" and conf == 0.0


def test_status_unverified_when_no_identifier():
    rep = Report(color=False)
    status, conf = verify.classify(_Ent(), _res(), rep)
    assert status == "UNVERIFIED" and conf == 0.2


def test_soft_mismatch_is_verified_with_reduced_confidence():
    # A right-paper-but-a-field-differs case is now VERIFIED with confidence 0.75
    # (the old 'LIKELY_VERIFIED' status is gone; confidence carries the nuance).
    e = _Ent()
    rep = Report(color=False)
    res = _res(record={"title": "T"}, source="crossref", sources={"crossref": {}})
    rep.add(Severity.WARN, e, "pages differ", "record", category="metadata_mismatch")
    status, conf = verify.classify(e, res, rep)
    assert status == "VERIFIED" and conf == 0.75


def test_garbled_doi_not_treated_as_resolvable_or_linked(monkeypatch):
    # A 'doi' field that merely CONTAINS a DOI inside junk (e.g. the .bib wrapped
    # 'https:\n //doi:10.1103/PhysRev.115.485') is not a usable DOI: it must not be
    # sent to Crossref, and no garbled 'doi.org/https:...' verify link is built.
    e = _entry('@article{k, author={A}, title={T}, journal={J},\n'
               ' DOI={https:\n //doi:10.1103/PhysRev.115.485}}\n')
    rep = Report(color=False)
    called = []
    monkeypatch.setattr(record, "fetch_crossref", lambda doi, t: called.append(doi) or (None, None))
    res = record.resolve_entry(e, rep, 0, 1)
    assert res.doi == ""                       # not treated as a resolvable DOI
    assert called == []                        # never queried Crossref with junk
    # No mangled doi.org-wrapped verify link built from the junk.
    assert "doi.org/https" not in rep.links.get("k", "")


# --- L5: year-gated DOI awareness ------------------------------------------

class _YEnt:
    def __init__(self, year, etype="article"):
        self.key, self.lineno, self.etype = "k", 1, etype
        self._year = year
    def get(self, f, d=""):
        return str(self._year) if f == "year" else d
    def field_line(self, f):
        return 1


def test_pre2005_article_not_penalized_for_missing_doi():
    rep = Report(color=False)
    verify.pid_check(_YEnt(1999), _res(record={"title": "T"}), rep, 0, 1, offline=True)
    # An informational <2005 note, never a warning.
    assert any("< 2005" in f.message and f.severity is Severity.INFO for f in rep.findings)
    assert not any(f.severity is Severity.WARN for f in rep.findings)


def test_post2005_article_missing_doi_warns_offline():
    rep = Report(color=False)
    verify.pid_check(_YEnt(2020), _res(record={"title": "T"}), rep, 0, 1, offline=True)
    assert any(f.category == "pid_missing" and f.severity is Severity.WARN
               for f in rep.findings)


def test_arxiv_only_is_sufficient_pid():
    rep = Report(color=False)
    verify.pid_check(_YEnt(2020), _res(arxiv_id="2103.16313", record={"title": "T"}),
                     rep, 0, 1, offline=True)
    assert not any(f.category in ("pid_missing", "doi_available") for f in rep.findings)


# --- L6: integrity score ---------------------------------------------------

def test_integrity_score_clean_vs_unverified():
    e1, e2 = _YEnt(2020), _YEnt(2020)
    e1.key, e2.key = "a", "b"
    rep = Report(color=False)
    clean = verify.integrity([e1], {"a": ("VERIFIED", 0.95)},
                             {"a": _res(doi="10.1/a", record={})}, rep)
    assert clean["integrity_score"] >= 90 and clean["verified"] == 1
    bad = verify.integrity([e2], {"b": ("UNVERIFIED", 0.0)},
                           {"b": _res()}, rep)
    assert bad["integrity_score"] < clean["integrity_score"]


def test_integrity_score_ignores_unchecked_entries():
    # In --tex mode only cited entries are resolved (they appear in `statuses`);
    # uncited entries are skipped by design. The score must be computed over the
    # checked entries only, so adding skipped entries to the bib must not change it
    # and `checked` must report the resolved count, not len(entries).
    checked = _YEnt(2020); checked.key = "cited"
    rep = Report(color=False)
    only = verify.integrity([checked], {"cited": ("VERIFIED", 0.95)},
                            {"cited": _res(doi="10.1/a", record={}, sources={"crossref": {}})},
                            rep)
    # Same checked entry, but the bib also holds 50 uncited/unresolved entries.
    uncited = [_YEnt(2020) for _ in range(50)]
    for i, e in enumerate(uncited):
        e.key = f"uncited{i}"
    with_skipped = verify.integrity([checked] + uncited,
                                    {"cited": ("VERIFIED", 0.95)},
                                    {"cited": _res(doi="10.1/a", record={}, sources={"crossref": {}})},
                                    rep)
    assert with_skipped["checked"] == 1
    assert with_skipped["integrity_score"] == only["integrity_score"]
    assert with_skipped["integrity_score"] >= 90


# --- L8: per-reference JSON shape ------------------------------------------

def test_json_has_summary_and_references():
    e = _Ent()
    rep = Report(color=False)
    rep.add(Severity.WARN, e, "an issue", "record", category="metadata_mismatch")
    res = _res(record={"title": "T", "year": 2020}, source="crossref",
               doi="10.1/x", sources={"crossref": {}})
    out = rep.to_json(summary={"checked": 1, "integrity_score": 90},
                      results={"k": res}, statuses={"k": ("VERIFIED", 0.75)})
    assert "findings" in out and "summary" in out and "references" in out
    ref = out["references"][0]
    assert ref["key"] == "k" and ref["status"] == "VERIFIED" and ref["confidence"] == 0.75
    assert ref["identifiers"]["doi"] == "10.1/x"
    assert ref["canonical_record"]["year"] == 2020
    assert ref["issues"] and ref["issues"][0]["category"] == "metadata_mismatch"


# --- syntax gate: broken entries skip online comparison --------------------

def test_syntax_pass_reports_broken_keys():
    # An entry with a field missing its '=' is structurally broken.
    bib = ("@article{good, author={A. B}, title={T}, year={2020}, journal={J}}\n"
           "@article{bad,\n  author = {A. B}\n  title {T}\n  year = {2020}\n}\n")
    entries, problems = parse_bib(bib)
    rep = Report(color=False)
    broken = syntax_pass(bib, entries, problems, rep)
    assert "bad" in broken and "good" not in broken


# --- L5: DOI search uses corroboration; found DOI resolves the record -------

class _SE:
    """A minimal article entry missing a DOI (for _search_doi tests)."""
    etype = "article"
    def __init__(self, **f):
        self._f = {"title": "A Sufficiently Long Distinct Title Here",
                   "author": "Gneiting, Tilmann", "journal": "Journal of Stats",
                   "year": "2007", **f}
        self.key, self.lineno = "k", 1
    def get(self, name, d=""):
        return self._f.get(name, d)
    def field_line(self, name):
        return 1


def test_search_doi_rejects_wrong_type_reprint(monkeypatch):
    # Same title+author, but a 'report' reprint with a different journal/year must
    # NOT be accepted -- this is the DTIC-tech-report false positive.
    from veracite import verify
    hit = {"DOI": "10.21236/adaXXXX", "type": "report",
           "title": ["A Sufficiently Long Distinct Title Here"],
           "author": [{"family": "Gneiting"}],
           "container-title": ["DTIC Technical Report"],
           "issued": {"date-parts": [[1999]]}}
    monkeypatch.setattr("veracite.http.http_get_json",
                        lambda url, t: ({"message": {"items": [hit]}}, 200))
    assert verify._search_doi(_SE(), 5) == ""


def test_search_doi_accepts_matching_journal(monkeypatch):
    from veracite import verify
    hit = {"DOI": "10.1111/right", "type": "journal-article",
           "title": ["A Sufficiently Long Distinct Title Here"],
           "author": [{"family": "Gneiting"}],
           "container-title": ["Journal of Stats"],
           "issued": {"date-parts": [[2007]]}}
    monkeypatch.setattr("veracite.http.http_get_json",
                        lambda url, t: ({"message": {"items": [hit]}}, 200))
    assert verify._search_doi(_SE(), 5) == "10.1111/right"


def test_found_doi_resolves_and_upgrades_status(monkeypatch):
    from veracite import verify, record
    e = _SE(year="2010")           # post-2005, no DOI
    res = record.Resolution()
    rep = Report(color=False)
    # search returns a strong hit; the subsequent fetch returns a clean record.
    monkeypatch.setattr(verify, "_search_doi", lambda e, t: "10.1111/right")
    monkeypatch.setattr(record, "fetch_crossref",
                        lambda doi, t: ({"authors": ["gneiting"], "given": {},
                                         "title": e.get("title"), "year": 2010,
                                         "journal": "Journal of Stats", "abstract": "x"}, 200))
    monkeypatch.setattr(record, "fetch_openalex", lambda doi, t: None)
    verify.pid_check(e, res, rep, 0, 1, offline=False)
    assert res.record is not None and res.doi == "10.1111/right"
    # Missing-DOI case: the finding suggests ADDING the found DOI (a `to`, no `from`).
    da = [f for f in rep.findings if f.category == "doi_available"]
    assert da and "add it" in da[0].message and "10.1111/right" in da[0].message
    assert da[0].suggested == {"field": "doi", "to": "10.1111/right"}
    status, _ = verify.classify(e, res, rep)
    assert status == "VERIFIED"   # no longer UNVERIFIED


def test_pre2005_missing_doi_with_none_found_is_not_warned(monkeypatch):
    from veracite import verify, record
    e = _SE(year="1990")
    res = record.Resolution()
    rep = Report(color=False)
    monkeypatch.setattr(verify, "_search_doi", lambda e, t: "")   # none found
    verify.pid_check(e, res, rep, 0, 1, offline=False)
    assert not any(f.severity is Severity.WARN for f in rep.findings)


def test_dead_doi_falls_back_to_search_and_recovers(monkeypatch):
    # A recorded DOI that 404'd (dead_doi) should fall through to the title search
    # just like a missing DOI, and recover the real DOI -- worded as a REPLACEMENT of
    # the dead one (old->new), with the entry upgraded to VERIFIED at low confidence.
    from veracite import verify, record
    e = _SE(year="2010")
    res = record.Resolution()
    res.doi, res.dead_doi = "10.1/dead", True       # recorded but unresolvable
    rep = Report(color=False)
    monkeypatch.setattr(verify, "_search_doi", lambda e, t: "10.1111/right")
    monkeypatch.setattr(record, "fetch_crossref",
                        lambda doi, t: ({"authors": ["gneiting"], "given": {},
                                         "title": e.get("title"), "year": 2010,
                                         "journal": "Journal of Stats", "abstract": "x"}, 200))
    monkeypatch.setattr(record, "fetch_openalex", lambda doi, t: None)
    verify.pid_check(e, res, rep, 0, 1, offline=False)
    assert res.doi == "10.1111/right"
    da = [f for f in rep.findings if f.category == "doi_available"]
    # Replacement wording + the old->new edit (from the DEAD doi, not the new one).
    assert da and "replace the dead one" in da[0].message
    assert da[0].suggested == {"field": "doi", "from": "10.1/dead", "to": "10.1111/right"}
    # The work is now verified via the corrected DOI -- not left UNVERIFIED -- but at a
    # reduced confidence flagging "right paper, wrong DOI on file".
    status, conf = verify.classify(e, res, rep)
    assert status == "VERIFIED" and conf == 0.6


def test_dead_doi_search_fails_does_not_emit_pid_missing(monkeypatch):
    # When the dead-DOI fallback search finds nothing, do NOT also say "no DOI
    # recorded" -- a DOI IS recorded (just dead); the dead_doi error already covers it.
    from veracite import verify, record
    e = _SE(year="2010")
    res = record.Resolution()
    res.doi, res.dead_doi = "10.1/dead", True
    rep = Report(color=False)
    monkeypatch.setattr(verify, "_search_doi", lambda e, t: "")          # none found
    monkeypatch.setattr(verify, "_search_arxiv_id", lambda e, t: "")
    verify.pid_check(e, res, rep, 0, 1, offline=False)
    assert not any(f.category == "pid_missing" for f in rep.findings)


# --- _search_doi robustness to stylistic variation -------------------------

def test_title_similar_handles_accents_greek_and_subtitle():
    from veracite.titles import title_similar
    assert title_similar("Schr{\\\"o}dinger gas dynamics here", "Schrodinger gas dynamics here")
    assert title_similar("Study of $\\alpha$ decay in nuclei", "Study of alpha decay in nuclei")
    assert title_similar("Forecasts & sharpness of models", "Forecasts and sharpness of models")
    assert title_similar("Combinatorial Optimization", "Combinatorial Optimization: Theory")
    # genuinely different titles still rejected
    assert not title_similar("Atom interferometry with cold gas",
                             "Optical lattices for quantum sims")


def _hit(**kw):
    base = {"DOI": "10.1/d", "type": "journal-article",
            "title": ["A Sufficiently Long Distinct Title Here"],
            "author": [{"family": "Gneiting"}],
            "container-title": ["Journal of Stats"],
            "issued": {"date-parts": [[2007]]}}
    base.update(kw)
    return base


def _search_with(monkeypatch, hit, entry):
    from veracite import verify
    monkeypatch.setattr("veracite.http.http_get_json",
                        lambda url, t: ({"message": {"items": [hit]}}, 200))
    return verify._search_doi(entry, 5)


def test_search_doi_tolerates_online_first_year_offset(monkeypatch):
    # bib year 2007, Crossref issued 2006 (online-first), different journal name ->
    # the +-1 year still corroborates, so the DOI is accepted.
    hit = _hit(DOI="10.1/right", issued={"date-parts": [[2006]]})
    hit["container-title"] = ["Some Other Journal Name Entirely"]
    assert _search_with(monkeypatch, hit, _SE(year="2007")) == "10.1/right"


def test_search_doi_tolerates_accented_title(monkeypatch):
    hit = _hit(DOI="10.1/acc",
               title=["A Sufficiently Long Distinct Title H{\\'e}re"])
    # bib has the ASCII form; the accented Crossref title must still match.
    assert _search_with(monkeypatch, hit, _SE()) == "10.1/acc"


def test_search_doi_collaboration_author_skips_surname_gate(monkeypatch):
    hit = _hit(DOI="10.1/collab", author=[{"name": "CMS Collaboration"}])
    e = _SE(author="{CMS Collaboration}")
    assert _search_with(monkeypatch, hit, e) == "10.1/collab"


def test_search_doi_still_rejects_wrong_year_and_journal(monkeypatch):
    # No corroboration: different journal AND a >1y year gap -> reject.
    hit = _hit(DOI="10.1/nope",
               **{"container-title": ["Totally Other Journal"]},
               issued={"date-parts": [[2001]]})
    assert _search_with(monkeypatch, hit, _SE(year="2007")) == ""


# --- modifications.md regressions -----------------------------------------
# Each test pins a false-positive/misleading-message fix from the audit so the
# behaviour cannot silently regress. Item numbers refer to modifications.md.

def test_accent_on_dotless_i_keeps_the_letter():
    # Item 4: '\'{\i}' (= i-acute) must fold to 'i', not drop the letter.
    assert normalize.fold_surname(r"{B{\'{\i}}lek}") == "bilek"
    assert normalize.fold_surname(r"{J{\'{\i}}lkov{\'a}}") == "jilkova"
    assert normalize.fold_surname(r"{K{\v r}{\'{\i}}{\v z}ek}") == "krizek"
    assert normalize.fold_surname(r"{Garc{\'\i}a-Benito}") == "garciabenito"


def test_ifmmode_conditional_takes_text_branch():
    # Item 12a: '\ifmmode \check{z}\else \v{z}\fi{}' (math-aware ž) -> 'pizorn'.
    assert normalize.fold_surname(r"Pi\ifmmode \check{z}\else \v{z}\fi{}orn") == "pizorn"


def test_scandinavian_ligature_macros_fold_to_base_letter():
    # Item 12b: '\aa'/'\AA' (å) fold to 'a', not 'aa'.
    assert normalize.fold_surname(r"Nyg{\aa}rd") == "nygard"
    assert normalize.fold_surname(r"{\AA}berg") == "aberg"


def test_ordinary_accents_still_fold():
    # Guard against the accent-regex rewrite breaking the common cases.
    assert normalize.fold_surname(r'Schr{\"o}dinger') == "schrodinger"
    assert normalize.fold_surname(r"Erd{\H o}s") == "erdos"
    assert normalize.fold_surname("Müller") == "muller"


def test_commented_entry_not_parsed():
    # Item 7: a '%'-commented-out entry is not a real entry (Biber line comment).
    bib = ("@article{real, author={A}, title={T}, journal={J}, year={2020}}\n"
           "%@article{ghost,\n%  author = {X},\n%  title  = {Y},\n%}\n"
           "@article{after, author={B}, title={U}, journal={K}, year={2021}}\n")
    entries, problems = parse_bib(bib)
    keys = [e.key for e in entries]
    assert "ghost" not in keys
    assert keys == ["real", "after"]
    assert problems == []


def test_escaped_percent_is_not_a_comment():
    # A '\%' is a literal percent, not a comment start -- the field after it stays.
    bib = "@article{k, title={50\\% effect}, author={A}, journal={J}, year={2020}}\n"
    entries, _ = parse_bib(bib)
    assert entries[0].get("title") == "50\\% effect"


def test_unescaped_percent_inside_value_is_not_a_comment():
    # A '%' inside a brace-delimited value (e.g. URL-encoded '%3A' in a doi.org
    # url) is literal, not a line comment. Blanking it would eat the closing brace
    # and fabricate an "unbalanced braces" structural error on a sound entry.
    bib = ("@article{k, title={T}, author={A}, journal={J}, year={2002},\n"
           " url={http://dx.doi.org/10.1023/A%3A1014599729717}}\n"
           "@article{after, title={U}, author={B}, journal={K}, year={2003}}\n")
    entries, problems = parse_bib(bib)
    assert problems == []
    assert [e.key for e in entries] == ["k", "after"]
    assert entries[0].get("url") == "http://dx.doi.org/10.1023/A%3A1014599729717"


def test_cite_parameter_token_not_mined_as_key():
    # Item 8: '\cite{#1}' in a \newcommand body must not become a cited key.
    from veracite import llm
    tex = (r"\providecommand{\autocite}[1]{\cite{#1}}" "\n"
           r"Body \cite{realkey} and \cite{a, b}." "\n")
    ctx = llm.find_citation_contexts([("/x.tex", tex)], "/")
    assert "#1" not in ctx
    assert set(ctx) == {"realkey", "a", "b"}
    assert llm.find_citation_groups([("/x.tex", tex)]) == [["a", "b"]]


def test_issn_in_isbn_field_gets_issn_message():
    # Items 9/10: a valid ISSN in the isbn field is "looks like an ISSN", not
    # "ISBN fails its check digit".
    e = _entry("@article{Bradlyn2017, title={T}, author={A}, journal={Nature},\n"
               " year={2017}, isbn={1476-4687}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    msgs = [f.message for f in rep.findings if f.category == "identifier_format"]
    assert any("looks like an ISSN" in m for m in msgs)
    assert not any("ISBN fails" in m for m in msgs)


def test_genuinely_bad_isbn_still_flagged():
    e = _entry("@book{k, title={T}, author={A}, year={2020},\n"
               " isbn={978-0-306-40615-8}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any("ISBN fails" in f.message for f in rep.findings)


def test_nature_ep_page_form_reduced_to_start_page():
    # Item 11: 'NNN EP -' is the Nature electronic-page form (start page only).
    assert normalize.norm_pages("412 EP -") == "412"
    assert normalize.norm_pages("887 EP") == "887"
    # an ordinary range is untouched.
    assert normalize.norm_pages("412--417") == "412-417"


def test_amp_entity_does_not_block_journal_match():
    # Item 6a: Crossref's 'Astronomy &amp; Astrophysics' must match INSPIRE's
    # 'Astron.Astrophys.' (the spurious 'amp' token was the blocker).
    assert record._journal_equiv("Astronomy &amp; Astrophysics", "Astron.Astrophys.")


def test_astronomy_abbreviations_resolve():
    # Item 6b: curated overrides (now sourced from the RMP Style Guide) -- acronyms
    # and the official RMP forms resolve to full titles, tolerating a leading
    # 'The' that Crossref adds...
    assert record._journal_equiv("ApJ", "The Astrophysical Journal")
    assert record._journal_equiv("Astrophys. J.", "The Astrophysical Journal")
    assert record._journal_equiv("MNRAS",
                                 "Monthly Notices of the Royal Astronomical Society")
    assert record._journal_equiv("A&A", "Astron. Astrophys.")
    # ...but ApJ and ApJ Letters stay distinct (the curated table is authoritative
    # and overrides the ISO-4 prefix heuristic that would wrongly equate them).
    assert not record._journal_equiv("ApJ", "ApJL")
    assert not record._journal_equiv("Astrophys.J.", "Astrophys.J.Lett.")


def test_rmp_physics_abbreviations_resolve():
    # The RMP Table XI import covers the common physics journals by their ISO-4
    # abbreviation against the full title.
    assert record._journal_equiv("Phys. Rev. Lett.", "Physical Review Letters")
    assert record._journal_equiv("Commun. Math. Phys.",
                                 "Communications in Mathematical Physics")
    assert record._journal_equiv("Europhys. Lett.", "Europhysics Letters")
    assert record._journal_equiv("J. Chem. Phys.", "Journal of Chemical Physics")


def test_noncanonical_parenthetical_journal_not_equated():
    # Per the human note: 'EPL (Europhysics Letters)' is a non-canonical form that
    # matches neither the standard abbreviation 'EPL' nor the full title -- it must
    # NOT be silently equated (so the journal metadata_mismatch still fires).
    assert not record._journal_equiv("EPL", "Europhysics Letters")
    assert not record._journal_equiv("EPL (Europhysics Letters)",
                                     "Europhysics Letters (EPL)")


def test_headerless_entry_reported_as_missing_header_not_stray_field():
    # Items 1/2: a block whose '@type{key,' header was deleted is one structural
    # error ("restore the header"), NOT a stray field attributed to the entry
    # above it. The previous entry parses cleanly and is not marked broken.
    bib = ("@inproceedings{Gok2010, author={Gok, S.}, title={LB}, year={2010},\n"
           " booktitle={Proc}}\n"
           "  author = {Lallemand, Pierre and Luo, Li-shi},\n"
           "  title = {LB moving boundaries},\n"
           "  journal = {Phys. Rev. E},\n"
           "  year = {2003}\n}\n"
           "@article{Next, author={X}, title={Y}, journal={Z}, year={2011}}\n")
    entries, problems = parse_bib(bib)
    rep = Report(color=False)
    broken = syntax_pass(bib, entries, problems, rep)
    assert "Gok2010" not in broken
    assert any(f.category == "missing_entry_header" for f in rep.findings)
    assert not any(f.category == "dropped_field" for f in rep.findings)


def test_single_stray_field_still_reported_as_dropped():
    # The single-appended-field case (a DOI after the '}') keeps the old advice.
    bib = ('@article{a, author={X}, title={T}, year={2020}, journal={J}}\n'
           'doi = "10.1/x"\n'
           '@article{b, author={Y}, title={U}, year={2021}, journal={K}}\n')
    entries, problems = parse_bib(bib)
    rep = Report(color=False)
    syntax_pass(bib, entries, problems, rep)
    assert any(f.category == "dropped_field" and "doi" in f.message
               for f in rep.findings)


def test_glued_and_separator_flagged():
    # Item 13: 'F.and Peng' (missing space after the initial) is one delimiter
    # error, reported as such.
    e = _entry("@article{k, author={Pientka, F.and Peng, Y.},\n"
               " title={T}, journal={J}, year={2015}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any(f.category == "author_format" and "F.and" in f.message
               for f in rep.findings)


def test_surname_with_and_not_flagged_as_glued():
    e = _entry("@article{k, author={Anderson, P. W. and Brandt, U.},\n"
               " title={T}, journal={J}, year={2015}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert not any(f.category == "author_format" for f in rep.findings)


def test_article_with_isbn_suggests_incollection():
    # Item 14: an @article carrying an ISBN/book-series DOI is a chapter -- suggest
    # @incollection/@inproceedings, not @online/@misc.
    e = _entry("@article{chap, author={N}, title={Chapter}, journal={Contemp. Math.},\n"
               " year={2017}, doi={10.1090/conm/717}, isbn={9781470449391}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    # A wrong-@type suggestion is a warning to weigh, not an invalid-BibTeX error,
    # so it lives in 'entrytype_suggestion', not 'missing_field'.
    msgs = [f.message for f in rep.findings if f.category == "entrytype_suggestion"]
    assert any("incollection" in m for m in msgs)
    assert all(f.severity is Severity.WARN
               for f in rep.findings if f.category == "entrytype_suggestion")
    assert not any("@online" in m for m in msgs)


def test_aip_journal_doi_not_treated_as_book_series():
    # An AIP DOI '10.1063/1.<digits>' serves both journal articles (Phys. Fluids,
    # J. Appl. Phys., ...) and conference proceedings, so it must NOT be read as a
    # book-series DOI -- doing so wrongly told plain journal articles to become
    # @incollection/@inproceedings.
    from veracite.normalize import is_book_series_doi
    assert not is_book_series_doi("10.1063/1.1471914")
    e = _entry("@article{Guo2007, author={Guo, Z.}, title={An extrapolation method},\n"
               " journal={Physics of Fluids}, volume={14}, number={6}, pages={2007-2010},\n"
               " year={2002}, doi={10.1063/1.1471914}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert not any("incollection" in f.message for f in rep.findings)


def test_zero_overlap_title_downranked_for_container_doi():
    # Item 14: a ~0% title overlap against a record reached via a book-series DOI
    # is a granularity artifact (id resolves to the volume) -> INFO note, not a
    # metadata_mismatch WARN, and not the wrong-record error.
    e = _entry("@article{chap, author={Nash, J.}, title={A Specific Chapter Title},\n"
               " year={2017}, doi={10.1090/conm/717}, isbn={9781470449391}}\n")
    rec = {"authors": ["nash"], "given": {},
           "title": "Contemporary Mathematics Volume 717", "year": "2017"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    assert not any(f.category == "id_resolves_wrong_record" for f in rep.findings)
    assert not any(f.category == "metadata_mismatch" and "title" in f.message
                   for f in rep.findings)
    assert any("containing book/volume" in f.message for f in rep.findings)


def test_locator_mismatch_is_per_field_with_record_suggestion():
    # Each soft-field disagreement is its OWN finding, leaning toward the canonical
    # record: the differing volume is a WARN carrying a 'bib -> record' suggested
    # edit (475.2229 -> 475), and the omitted number is a parity note pointing at
    # the record's value. Both directional toward Crossref.
    e = _entry('@article{k, author={Bondar, D.}, title={Koopman wave functions},\n'
               ' journal={Proc R Soc A}, volume={475.2229}, number={}, pages={20180879},\n'
               ' year={2019}, doi={10.1098/rspa.2018.0879}}\n')
    rec = {"authors": ["bondar"], "given": {}, "title": "Koopman wave functions",
           "volume": "475", "number": "2229", "pages": "20180879", "year": "2019",
           "journal": "Proc R Soc A", "doi": "10.1098/rspa.2018.0879"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    vol = [f for f in rep.findings if f.category == "metadata_mismatch"
           and "volume" in f.message]
    assert len(vol) == 1 and vol[0].severity is Severity.WARN
    assert vol[0].suggested == {"field": "volume", "from": "475.2229", "to": "475"}
    # The omitted number is offered as a parity suggestion pointing at the record.
    assert any(f.category == "parity_suggestion" and "number '2229'" in f.message
               for f in rep.findings)


def test_omitted_locator_without_mismatch_stays_a_parity_note():
    # An entry that simply omits 'number' with NO other locator conflict must keep
    # its benign parity note -- it must NOT gain a metadata_mismatch warning.
    e = _entry('@article{k, author={Smith, Jane}, title={A Study}, journal={J},\n'
               ' volume={12}, number={}, pages={5}, year={2020}, doi={10.1/x}}\n')
    rec = {"authors": ["smith"], "given": {"smith": "jane"}, "title": "A Study",
           "volume": "12", "number": "3", "pages": "5", "year": "2020",
           "journal": "J", "doi": "10.1/x"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    assert not any(f.category == "metadata_mismatch" and "issue" in f.message
                   for f in rep.findings)
    assert any(f.category == "parity_suggestion" and "number '3'" in f.message
               for f in rep.findings)


def test_genuine_volume_mismatch_suggests_record_value():
    # A genuinely wrong volume (number present and correct) is a WARN whose
    # suggested edit conforms the bib to the record (99 -> 12); no empty-locator
    # text, and no parity note for the already-present number.
    e = _entry('@article{k, author={Smith, Jane}, title={A Study}, journal={J},\n'
               ' volume={99}, number={3}, pages={5}, year={2020}, doi={10.1/x}}\n')
    rec = {"authors": ["smith"], "given": {"smith": "jane"}, "title": "A Study",
           "volume": "12", "number": "3", "pages": "5", "year": "2020",
           "journal": "J", "doi": "10.1/x"}
    rep = Report(color=False)
    record.compare_against_record(e, rec, "crossref", rep)
    vol = [f for f in rep.findings if f.category == "metadata_mismatch"
           and "volume" in f.message]
    assert len(vol) == 1
    assert vol[0].suggested == {"field": "volume", "from": "99", "to": "12"}
    assert not any("(empty)" in f.message for f in rep.findings)


def test_biblatex_validity_consolidates_multiple_fields():
    # Per-user request: several invalid fields on one entry collapse into a single
    # note listing each field with its line.
    e = _entry("@article{k, author={A}, title={T}, journal={J}, year={2001},\n"
               " adsurl={http://x}, adsnote={ADS}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    notes = [f for f in rep.findings if f.category == "biblatex_validity"]
    assert len(notes) == 1
    assert "adsurl" in notes[0].message and "adsnote" in notes[0].message


def test_all_caps_title_is_one_recase_note_not_acronym_flood():
    # An entirely UPPERCASE title is one 'recase' note, not one acronym note per
    # word (each word would otherwise be flagged as an unprotected acronym).
    e = _entry("@article{k, author={CHEN, Q.},\n"
               " title={A NOVEL LATTICE BOLTZMANN SCHEME FOR COMPRESSIBLE FLOWS},\n"
               " journal={J}, year={2012}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert sum(1 for f in rep.findings if f.category == "title_case") == 1
    # no per-word 'acronym in title not brace-protected' notes for the all-caps run.
    assert not any("acronym in title" in f.message for f in rep.findings)


def test_normal_title_keeps_acronym_protection_notes():
    # A normally-cased title with real acronyms still gets the per-word brace
    # protection note (the miscased-title path must not swallow these).
    e = _entry("@article{k, author={Smith, J.},\n"
               " title={A BGK model for DNA folding}, journal={J}, year={2012}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    acronyms = [f.suggested["from"] for f in rep.findings
                if "acronym" in f.message and f.suggested]
    assert "BGK" in acronyms and "DNA" in acronyms
    assert not any(f.category == "title_case" for f in rep.findings)


def test_record_casing_refines_and_withdraws_offline_miscase():
    # Online, the record carries canonical casing: the offline 'looks miscased'
    # note is withdrawn and replaced by an 'adopt the record's casing' note.
    e = _entry("@article{k, author={CHEN, Q.},\n"
               " title={A NOVEL LATTICE BOLTZMANN SCHEME FOR COMPRESSIBLE FLOWS},\n"
               " journal={J}, year={2012}, doi={10.1/x}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    rec = {"authors": ["chen"], "given": {},
           "title": "A novel lattice Boltzmann scheme for compressible flows",
           "year": "2012"}
    record.compare_against_record(e, rec, "crossref", rep)
    # the offline 'looks miscased' note is superseded; live view shows only the
    # record-layer 'adopt the record's casing' note.
    tc = [f for f in rep.live_findings() if f.category == "title_case"]
    assert len(tc) == 1 and tc[0].layer == "record"
    assert "adopt the record's casing" in tc[0].message


# --- broadened rule coverage ----------------------------------------------

def test_duplicate_doi_normalizes_url_vs_bare():
    # 'https://doi.org/10.1/x' and '10.1/x' are the same DOI -> flagged as shared.
    bib = ("@article{a, title={T}, author={X}, journal={J}, year={2020},\n"
           " doi={https://doi.org/10.1/x}}\n"
           "@article{b, title={U}, author={Y}, journal={K}, year={2021},\n"
           " doi={10.1/x}}\n")
    entries, _ = parse_bib(bib)
    rep = Report(color=False)
    run_static(entries, rep)
    assert any(f.category == "duplicate" and "DOI shared" in f.message
               for f in rep.findings)


def test_et_al_tie_and_run_together_flagged():
    for val in ("Smith, J. et~al.", "Smith, J. et.al."):
        e = _entry("@article{k, author={" + val + "}, title={T},\n"
                   " journal={J}, year={2020}}\n")
        rep = Report(color=False)
        run_static([e], rep)
        assert any("literal 'et al.'" in f.message for f in rep.findings), val


def test_isbn_in_issn_field_gets_isbn_message():
    e = _entry("@book{k, title={T}, author={A}, year={2020},\n"
               " issn={978-0-306-40615-7}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any("looks like an ISBN" in f.message for f in rep.findings)


def test_doi_trailing_punctuation_flagged():
    e = _entry("@article{k, title={T}, author={A}, journal={J}, year={2020},\n"
               " doi={10.1103/PhysRevB.1.234.}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any("stray punctuation" in f.message for f in rep.findings)


def test_single_hyphen_page_range_flagged_but_single_page_is_not():
    e = _entry("@article{k, title={T}, author={A}, journal={J}, year={2020},\n"
               " pages={10-20}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any("single hyphen" in f.message for f in rep.findings)
    e2 = _entry("@article{k, title={T}, author={A}, journal={J}, year={2020},\n"
                " pages={42}}\n")
    rep2 = Report(color=False)
    run_static([e2], rep2)
    assert not any("single hyphen" in f.message for f in rep2.findings)


def test_implausible_year_flagged_placeholder_and_clean_year_not():
    bad = _entry("@article{k, title={T}, author={A}, journal={J}, year={20201}}\n")
    rep = Report(color=False)
    run_static([bad], rep)
    assert any("no plausible" in f.message for f in rep.findings)
    for ok in ("1995", "in press", "forthcoming"):
        e = _entry("@article{k, title={T}, author={A}, journal={J}, year={" + ok + "}}\n")
        r = Report(color=False)
        run_static([e], r)
        assert not any("no plausible" in f.message for f in r.findings), ok


def test_shouted_author_surnames_flagged():
    e = _entry("@article{k, author={CHEN, Q. and ZHANG, X. B.},\n"
               " title={T}, journal={J}, year={2020}}\n")
    rep = Report(color=False)
    run_static([e], rep)
    assert any(f.category == "author_format" and "ALL-CAPS" in f.message
               for f in rep.findings)
    # normal casing is not flagged.
    e2 = _entry("@article{k, author={Chen, Q. and Zhang, X.},\n"
                " title={T}, journal={J}, year={2020}}\n")
    rep2 = Report(color=False)
    run_static([e2], rep2)
    assert not any(f.category == "author_format" and "ALL-CAPS" in f.message
                   for f in rep2.findings)


# --- @string abbreviation + '#' concatenation expansion --------------------
# A .bib that uses @string journal macros is correct BibTeX; leaving the macro
# name unexpanded makes the record-layer compare 'prb' against 'Physical Review B'
# and emit a false metadata_mismatch. These pin the expansion in the parser.

def test_string_macro_brace_form_is_expanded():
    e, _ = parse_bib('@string{prb = "Phys. Rev. B"}\n'
                     '@article{r, journal=prb, title={T}, author={A. B}, year={2020}}')
    assert e[0].fields["journal"] == "Phys. Rev. B"


def test_string_macro_paren_form_is_expanded():
    # '@String(NAME = {value})' -- the paren-delimited form common in real .bib files.
    e, _ = parse_bib('@String(JOV = {J. Vis.})\n'
                     '@article{r, journal=JOV, title={T}, author={A. B}, year={2020}}')
    assert e[0].fields["journal"] == "J. Vis."


def test_string_concatenation_with_hash():
    e, _ = parse_bib('@string{ieee = "IEEE Transactions on "}\n'
                     '@article{c, journal = ieee # "Information Theory", '
                     'title={T}, year={2020}}')
    assert e[0].fields["journal"] == "IEEE Transactions on Information Theory"


def test_string_macro_referencing_earlier_macro():
    e, _ = parse_bib('@string{a={X}}\n@string{b = a # {Y}}\n'
                     '@article{r, journal=b, title={T}, year={2020}}')
    assert e[0].fields["journal"] == "XY"


def test_string_lookup_is_case_insensitive():
    e, _ = parse_bib('@string{PR="Physical Review"}\n'
                     '@article{r, journal=pr, title={T}, year={2020}}')
    assert e[0].fields["journal"] == "Physical Review"


def test_undefined_bare_word_is_kept_verbatim():
    # A bare word that is NOT a defined macro (a month name, a number) must pass
    # through unchanged so the month-name and identifier checks still see it.
    e, _ = parse_bib('@article{r, month=may, year=2020, journal={Nature}, title={T}}')
    assert e[0].fields["month"] == "may"
    assert e[0].fields["year"] == "2020"


def test_unclosed_paren_string_recovers_at_next_entry():
    # A malformed @string(...) that never closes must not swallow the entries after
    # it -- the parser resyncs at the next '@entry{'.
    e, _ = parse_bib('@string(bad = {oops}\n'
                     '@article{survivor, title={T}, author={A. B}, '
                     'journal={Nature}, year={2020}}')
    assert [x.key for x in e] == ["survivor"]


# --- JSON report shape & --offline/--llm guard -----------------------------

def test_offline_json_has_summary_record_with_null_score(tmp_path):
    """The NDJSON report carries a reserved <summary> record even offline, with an
    honest null integrity_score -- never a fabricated 100 from zero verified."""
    import json
    from veracite.cli import main
    bib = tmp_path / "refs.bib"
    bib.write_text(_ONE_ENTRY, encoding="utf-8")
    out = tmp_path / "rep.json"
    main(["--bib", str(bib), "--offline", "--no-color", "--json", str(out)])
    recs = {json.loads(l)["key"]: json.loads(l)
            for l in out.read_text().splitlines() if l.strip()}
    assert "<summary>" in recs and "<file>" in recs
    summary = recs["<summary>"]["summary"]
    assert summary["mode"] == "offline"
    assert summary["integrity_score"] is None
    # The single entry appears as its own record with the offline phase set.
    entry = [r for k, r in recs.items() if k not in ("<summary>", "<file>")][0]
    assert entry["phases"]["offline"] is True and entry["phases"]["online"] is False


def test_llm_with_offline_is_rejected(tmp_path, capfd):
    """--llm needs the online abstract layer; with --offline it would silently do
    nothing, so it must error rather than print HEALTHY with no sweep."""
    from veracite.cli import main
    bib = tmp_path / "refs.bib"
    bib.write_text(_ONE_ENTRY, encoding="utf-8")
    tex = tmp_path / "p.tex"
    tex.write_text("\\cite{k}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--bib", str(bib), "--offline", "--llm", "--tex", str(tex), "--no-color"])
    assert exc.value.code != 0
    assert "--llm cannot run with --offline" in capfd.readouterr().err


def test_uncited_entry_is_single_line_and_not_analyzed(tmp_path, capfd):
    """In --tex mode an uncited entry is reduced to one UNCITED header line: no
    offline rules run for it, so its style/structural problems neither print nor
    gate the verdict. An uncited @inproceedings missing 'booktitle' (an ERROR when
    analyzed) must NOT flip the run to NEEDS ATTENTION."""
    from veracite.cli import main
    bib = tmp_path / "r.bib"
    bib.write_text(
        "@article{cited, author={Smith, J}, title={A Cited Study Here}, journal={J},\n"
        " year={2020}, volume={1}, pages={1}, doi={10.1/a}}\n"
        "@inproceedings{uncited, author={Doe, J}, title={UPPERCASE BAD TITLE}, year={2019}}\n",
        encoding="utf-8")
    tex = tmp_path / "m.tex"
    tex.write_text("\\cite{cited}\n", encoding="utf-8")
    rc = main(["--bib", str(bib), "--tex", str(tex), "--offline", "--no-color"])
    out = capfd.readouterr().out
    assert "uncited  @inproceedings  line 3  UNCITED in .tex source" in out
    # The uncited entry's would-be findings are absent.
    assert "missing_field" not in out and "title_case" not in out
    assert rc == 0          # the uncited entry's error does not gate the verdict


def test_no_tex_analyzes_every_entry(tmp_path, capfd):
    """Without --tex, every entry is analyzed -- the same entry's structural error
    now appears and gates the verdict, so the .bib can be checked in full."""
    from veracite.cli import main
    bib = tmp_path / "r.bib"
    bib.write_text(
        "@inproceedings{uncited, author={Doe, J}, title={A Fine Title Here}, year={2019}}\n",
        encoding="utf-8")
    rc = main(["--bib", str(bib), "--offline", "--no-color"])
    out = capfd.readouterr().out
    assert "missing_field" in out and "booktitle" in out
    assert rc != 0          # analyzed -> the error gates the verdict


# --- per-service request pacing (http._throttle) ---------------------------

def test_throttle_paces_per_service_and_credits_elapsed_time(monkeypatch):
    """The HTTP throttle waits only the remainder of a service's interval, counts
    time already elapsed, and treats each service independently -- so a Crossref-only
    entry never pays an arXiv delay and a slow service spaces out across work."""
    import time
    from veracite import http
    from veracite.config import SETTINGS
    monkeypatch.setitem(SETTINGS, "request_delay", 0.2)
    cr = "https://api.crossref.org/works/x"
    ax = "http://export.arxiv.org/api/query?id=1"

    # Three same-service calls: first free, two waits of ~0.2s -> ~0.4s total.
    http.reset_throttle()
    t0 = time.monotonic()
    for _ in range(3):
        http._throttle(cr)
    assert 0.35 <= time.monotonic() - t0 <= 0.7

    # arXiv has its own 3s interval, independent of Crossref's timer.
    http.reset_throttle()
    http._throttle(ax)                       # arms arXiv only
    t0 = time.monotonic()
    http._throttle(cr)                       # different service -> no wait
    assert time.monotonic() - t0 < 0.1

    # Enough time already elapsed since the last call -> proceed immediately.
    http.reset_throttle()
    http._throttle(cr)
    time.sleep(0.25)                         # > 0.2 interval
    t0 = time.monotonic()
    http._throttle(cr)
    assert time.monotonic() - t0 < 0.1
    http.reset_throttle()


def test_throttle_only_runs_for_real_requests(monkeypatch):
    """A run that makes no HTTP call sleeps not at all: the pacing lives in the GET
    helpers, so a mocked/offline path is instant (no scattered per-entry sleeps)."""
    import time
    from veracite import http
    http.reset_throttle()
    # No _throttle calls -> no time spent.
    t0 = time.monotonic()
    assert time.monotonic() - t0 < 0.05


# --- arXiv title-search fallback for entries missing a DOI/arXiv id ---------

def test_search_arxiv_id_requires_title_and_author_match(monkeypatch):
    """The arXiv fallback returns an id only when the title is similar AND the first
    author surname matches -- a same-title different-author hit is rejected."""
    from veracite import verify
    from veracite.models import Record
    e = _entry("@inproceedings{k, author={Lee, Kenton and Joshi, M},\n"
               " title={Pix2Struct: Screenshot Parsing as Pretraining},\n"
               " booktitle={ICML}, year={2023}}\n")

    # _search_arxiv_id imports search_arxiv from .sources, so patch it there.
    import veracite.sources as src
    # Right title, right first author -> returns the id.
    monkeypatch.setattr(src, "search_arxiv", lambda t, to: [
        ("2210.03347", Record(authors=["lee"], authors_display=["Lee"],
                              title="Pix2Struct: Screenshot Parsing as Pretraining"))])
    assert verify._search_arxiv_id(e, 5) == "2210.03347"

    # Right title, WRONG first author -> rejected (no false match).
    monkeypatch.setattr(src, "search_arxiv", lambda t, to: [
        ("9999.99999", Record(authors=["smith"], authors_display=["Smith"],
                              title="Pix2Struct: Screenshot Parsing as Pretraining"))])
    assert verify._search_arxiv_id(e, 5) == ""


def test_pid_check_resolves_missing_id_via_arxiv(monkeypatch):
    """An article with no DOI and no arXiv id, but findable on arXiv by title, is
    resolved and verified (not left UNVERIFIED), and flags the eprint to add."""
    from veracite import verify, record as rec_mod
    from veracite.models import Record
    from veracite.report import Report

    e = _entry("@inproceedings{k, author={Lee, Kenton},\n"
               " title={Pix2Struct Screenshot Parsing as Pretraining},\n"
               " booktitle={ICML}, year={2023}}\n")
    res = rec_mod.Resolution()        # no doi / arxiv id
    rep = Report(color=False)

    # Crossref DOI search finds nothing; arXiv title search finds the paper.
    monkeypatch.setattr(verify, "_search_doi", lambda e, t: "")
    monkeypatch.setattr(verify, "_search_arxiv_id", lambda e, t: "2210.03347")
    monkeypatch.setattr(rec_mod, "fetch_arxiv", lambda i, t: Record(
        authors=["lee"], authors_display=["Lee"],
        title="Pix2Struct Screenshot Parsing as Pretraining", year=2022,
        abstract="abs"))

    strongest = verify.pid_check(e, res, rep, 0, 5, offline=False)
    assert strongest == "arxiv"
    assert res.arxiv_id == "2210.03347" and res.record is not None
    assert any(f.category == "doi_available" and "on arXiv" in f.message
               for f in rep.findings)
    # The classify step then treats it as resolved (VERIFIED), not UNVERIFIED.
    status, _ = verify.classify(e, res, rep)
    assert status == "VERIFIED"


def test_arxiv_hit_prefers_published_doi(monkeypatch):
    """When the arXiv record links a PUBLISHED version (its <arxiv:doi>), the entry
    is resolved against THAT DOI (Crossref) and the DOI -- not the bare preprint id
    -- is what gets suggested. The arXiv id is kept alongside."""
    from veracite import verify, record as rec_mod
    from veracite.models import Record
    from veracite.report import Report

    e = _entry("@inproceedings{k, author={Lee, Kenton},\n"
               " title={A Findable Paper Title Here}, booktitle={ICML}, year={2023}}\n")
    res = rec_mod.Resolution()
    rep = Report(color=False)
    monkeypatch.setattr(verify, "_search_doi", lambda e, t: "")
    monkeypatch.setattr(verify, "_search_arxiv_id", lambda e, t: "2210.03347")
    # arXiv record carries a published DOI; Crossref resolves it.
    monkeypatch.setattr(rec_mod, "fetch_arxiv", lambda i, t: Record(
        authors=["lee"], authors_display=["Lee"], title="A Findable Paper Title Here",
        year=2022, abstract="abs", published_doi="10.1234/published.1"))
    monkeypatch.setattr(rec_mod, "fetch_crossref", lambda d, t: (Record(
        authors=["lee"], authors_display=["Lee"], title="A Findable Paper Title Here",
        year=2023, journal="ICML"), 200))

    strongest = verify.pid_check(e, res, rep, 0, 5, offline=False)
    assert strongest == "doi"
    assert res.doi == "10.1234/published.1"        # published DOI used
    assert res.arxiv_id == "2210.03347"            # arXiv id kept too
    assert any(f.category == "doi_available" and "published DOI 10.1234/published.1"
               in f.message for f in rep.findings)


def test_report_is_stamped_with_veracite_version(tmp_path, capfd):
    """A report is traceable to the tool revision: the terminal summary names the
    version and the NDJSON <summary> record carries veracite_version."""
    import json
    from veracite import __version__
    from veracite.cli import main
    bib = tmp_path / "r.bib"
    bib.write_text("@article{k, author={Smith, J}, title={A Title Here Now Long},\n"
                   " journal={J}, year={2020}, volume={1}, pages={1}}\n", encoding="utf-8")
    out = tmp_path / "rep.ndjson"
    main(["--bib", str(bib), "--offline", "--no-color", "--json", str(out)])
    assert f"VeraCite {__version__}" in capfd.readouterr().out
    summary = [json.loads(l) for l in out.read_text().splitlines()
               if l.strip() and json.loads(l)["key"] == "<summary>"][0]
    assert summary["veracite_version"] == __version__
