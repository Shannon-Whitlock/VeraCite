"""Drift guards for the rule catalog (`veracite --list-rules`, catalog.py).

The catalog is the publisher's audit sheet, and it is only useful if it is
*complete and accurate* with respect to what the code actually emits. These tests
fail the moment a new `category="..."` literal is added without the matching
metadata, so the audit sheet can never silently fall behind the rules. No network
is touched.
"""

import json
import io

import pytest

from veracite import catalog
from veracite.catalog import INTENTIONALLY_UNPINNED as _INTENTIONALLY_UNPINNED
from veracite.config import DEFAULT_SETTINGS, load_settings
from veracite.report import CATEGORY_DOC, CATEGORY_GROUP, SEVERITY_NAMES, SUPERSEDES


@pytest.fixture(autouse=True)
def _defaults():
    load_settings(explicit_path="/dev/null")


def test_every_emitted_category_has_a_description():
    """The audit sheet must describe every category a run can produce: a category
    with no CATEGORY_DOC entry would print a blank description."""
    missing = catalog.emitted_categories() - set(CATEGORY_DOC)
    assert not missing, f"categories emitted but missing from report.CATEGORY_DOC: {sorted(missing)}"


def test_every_emitted_category_has_an_explicit_group():
    """finding_group() falls back to 'semantic' for an unknown category, which is
    safe at runtime but hides a category from intentional grouping. The audit
    sheet should reflect a deliberate group, so every emitted category must be in
    CATEGORY_GROUP explicitly."""
    missing = catalog.emitted_categories() - set(CATEGORY_GROUP)
    assert not missing, f"categories emitted but missing from report.CATEGORY_GROUP: {sorted(missing)}"


def test_every_emitted_category_is_pinned_or_intentionally_unpinned():
    """A publisher re-ranks findings by category name in the settings 'severity'
    table. A category that is emitted but absent from that table cannot be
    re-ranked by name -- so every emitted category must either be pinned there or
    be a known mixed-severity exception."""
    table = set(DEFAULT_SETTINGS["severity"])
    emitted = catalog.emitted_categories()
    unaccounted = emitted - table - _INTENTIONALLY_UNPINNED
    assert not unaccounted, (
        "categories emitted but neither pinned in DEFAULT_SETTINGS['severity'] nor "
        f"listed in _INTENTIONALLY_UNPINNED: {sorted(unaccounted)}")


def test_intentionally_unpinned_are_actually_unpinned():
    """Keep the exception list honest: an 'unpinned' category that someone later
    pins in the table should drop off the exception list, not linger."""
    table = set(DEFAULT_SETTINGS["severity"])
    stale = _INTENTIONALLY_UNPINNED & table
    assert not stale, f"listed as intentionally unpinned but pinned in the severity table: {sorted(stale)}"


def test_severity_table_values_are_valid_labels():
    """Every default severity must be a label the resolver understands."""
    for cat, label in DEFAULT_SETTINGS["severity"].items():
        assert str(label).lower() in SEVERITY_NAMES, f"{cat!r} has invalid severity {label!r}"


def test_supersedes_targets_are_real_categories():
    """A supersession naming a category no rule emits is dead config a publisher
    would puzzle over."""
    emitted = catalog.emitted_categories()
    unknown = set(SUPERSEDES) - emitted
    assert not unknown, f"SUPERSEDES names categories nothing emits: {sorted(unknown)}"


def test_emitted_types_follow_the_naming_convention():
    """A `type` uniquely names an issue WITHIN a category, so it must be that
    category or '<category>.<suffix>' -- the prefix before the first '.' has to be a
    real emitted category. This keeps (category, type) coherent and greppable."""
    cats = catalog.emitted_categories()
    for t in catalog.emitted_types():
        prefix = t.split(".", 1)[0]
        assert prefix in cats, (
            f"type {t!r}'s category prefix {prefix!r} is not an emitted category")
        assert t == prefix or t.startswith(prefix + "."), (
            f"type {t!r} must be its category or '<category>.<suffix>'")


def test_catalog_types_group_under_their_category():
    """The catalog row for a fat category lists its disambiguated types; a category
    that emits one kind of issue has an empty `types` list."""
    by_cat = {r["category"]: r for r in catalog.catalog()}
    for cat, row in by_cat.items():
        for t in row["types"]:
            assert t.startswith(cat + "."), f"{t!r} mis-grouped under {cat!r}"


def test_catalog_covers_exactly_the_emitted_categories():
    rows = catalog.catalog()
    assert {r["category"] for r in rows} == catalog.emitted_categories()
    # Pinned categories carry their default severity; an unpinned one reads 'mixed'.
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["title_case"]["default_severity"] == "note"
    assert by_cat["author_format"]["default_severity"] == "mixed"


def test_every_category_has_at_least_one_source():
    """The catalog is an index into the logic, so every category must point at the
    code that emits it -- an orphan category (no source) would be a dead row a
    publisher could not trace to a rule."""
    for row in catalog.catalog():
        assert row["sources"], f"category {row['category']!r} has no source location"


def test_sources_point_at_real_code():
    """Each (file, line) a source names must exist and the named line must actually
    carry that category literal -- so the index is faithful: following it lands on
    the emit site, not a stale line number."""
    import os
    pkg = os.path.dirname(catalog.__file__)
    for row in catalog.catalog():
        cat = row["category"]
        for s in row["sources"]:
            path = os.path.join(pkg, s["file"])
            assert os.path.isfile(path), f"{cat}: source file {s['file']} missing"
            # Split on '\n' only -- the same line numbering the scanner uses (file
            # iteration). str.splitlines() also breaks on exotic separators like
            # U+2028/U+2029, which appear as data in rules.py's suspicious-char
            # table and would shift the numbering.
            lines = open(path, encoding="utf-8").read().split("\n")
            assert 1 <= s["line"] <= len(lines), f"{cat}: {s['file']}:{s['line']} out of range"
            assert f'category="{cat}"' in lines[s["line"] - 1], \
                f"{cat}: {s['file']}:{s['line']} does not emit category=\"{cat}\""


def test_table_lists_source_functions():
    """The human table must show the emitting function(s), capped for width."""
    buf = io.StringIO()
    catalog.print_catalog(as_json=False, stream=buf)
    out = buf.getvalue()
    assert "rules (in source)" in out          # the column header
    assert "truncated_authors" in out          # a single-function category
    assert "+" in out and "more" in out        # a capped multi-function row (style)


def test_json_output_round_trips():
    buf = io.StringIO()
    catalog.print_catalog(as_json=True, stream=buf)
    data = json.loads(buf.getvalue())
    assert data["rules"] == catalog.catalog()
    # The JSON carries the FULL source list (not the table's capped view).
    style = [r for r in data["rules"] if r["category"] == "style"][0]
    assert len(style["sources"]) > 3
    assert all({"function", "file", "line"} <= set(s) for s in style["sources"])
