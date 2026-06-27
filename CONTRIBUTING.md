# Contributing to VeraCite

VeraCite is a small, single-maintainer project. This file is the practical guide for
working on it — whether that's the maintainer between sessions or an occasional
outside contributor sending a fix. It stays deliberately light. The **design
principles** behind every code change live in [`CLAUDE.md`](CLAUDE.md); read those
before adding or changing a rule. In short: VeraCite is read-only, every finding is a
deterministic rule or a comparison against an authoritative record, and **a false
positive is the cardinal sin** — prefer silence to a wrong flag.

## Setup

VeraCite is pure Python with no required dependencies (it falls back to the stdlib
`urllib` when `requests` is absent).

```bash
git clone https://github.com/Shannon-Whitlock/VeraCite
cd VeraCite
pip install -e ".[test]"        # editable install + pytest
```

- **Python 3.8+.** A virtualenv (`python -m venv .venv && source .venv/bin/activate`)
  is optional but tidy. Add the `[http]` extra (`pip install -e ".[test,http]"`) for
  the faster `requests` backend.
- The optional `--llm` sweep needs the [`claude` CLI](https://docs.claude.com/en/docs/claude-code)
  on `PATH` and logged in; everything else works with no account.

See it work on the bundled sample (a few famous papers with planted defects):

```bash
python -m veracite --bib examples/sample.bib --offline   # static checks only
python -m veracite --bib examples/sample.bib             # + online verification
```

Smaller fixtures live under [`tests/fixtures/`](tests/fixtures/). To stress-test, just
point `--bib` at any `.bib` of your own.

## The one rule: keep the tests green

```bash
python -m pytest                                    # the whole suite
python -m pytest tests/test_veracite.py -k month    # a focused subset
```

The suite is what keeps VeraCite trustworthy across changes: `test_catalog.py`
checks every finding category is fully registered, and each rule has a test for both
the case it should catch *and* a valid look-alike it must not flag. **If the suite is
green, you're in good shape to commit.** If a change can't stay green, it isn't ready.

## Making a change

No heavy process — commit to `main` when the suite is green. Branch only when you want
to keep messy work-in-progress separate (`git switch -c some-fix`, then merge back).
The habits worth keeping, because they protect you later:

- **Reproduce a bug with a failing test first**, then fix it — so it can't silently
  come back. (See *Before proposing a patch* in [`CLAUDE.md`](CLAUDE.md).)
- **Ship a rule with its tests** — both the positive and the negative case (below).
- The exit status, the `--list-rules` catalog, and the `--json` NDJSON shape are a
  public contract; note it in the commit message when you change one.

An outside contributor: open a pull request against `main` with the same in mind. The
maintainer will run the suite and read the diff.

## Adding or changing a rule

The most common change, with a specific contract — read *Everything is a rule* and
*Writing a rule that doesn't misfire* in [`CLAUDE.md`](CLAUDE.md) first. The checklist:

1. Write the check in [`veracite/rules.py`](veracite/rules.py) as `@rule` (per entry)
   or `@file_rule` (whole file).
2. Give every finding a stable `category=`. For a **new** category, register it in all
   four places (`CATEGORY_DOC`, `CATEGORY_GROUP`, `SUPERSEDES` as needed in
   [`report.py`](veracite/report.py); `DEFAULT_SETTINGS["severity"]` in
   [`config.py`](veracite/config.py)) — `test_catalog.py` fails if any is missing.
3. Add **both** a positive test (the defect is flagged) and a negative test (a valid
   look-alike is not) in [`tests/`](tests/).
4. Confirm `python -m veracite --list-rules` shows the category as you intend.

The biblatex datamodel ([`veracite/data/biblatex_datamodel.json`](veracite/data/biblatex_datamodel.json))
is generated from biblatex's own `blx-dm.def` by
[`tools/gen_datamodel.py`](tools/gen_datamodel.py) — regenerate it rather than
hand-editing when biblatex updates.

## Code style

Match the surrounding code; the house style is deliberate:

- **Comments and docstrings explain *why*, not *what*** — most functions state the
  defect the rule targets and why it won't misfire.
- Plain Python, standard library first; no new dependency without good reason.
- Keep findings terse and parseable — one finding per line, the advisory edit in the
  structured `suggested` field, never baked into the prose.
- Determinism in the verification path: no language-model calls outside the opt-in
  `--llm` layer. The integrity score and confidence are transparent formulas in
  [`verify.py`](veracite/verify.py) — keep them that way.

## Reporting a problem

The most valuable report is a **false positive** (a clean entry that got flagged) or
a **false negative** (a real defect that slipped through) — each becomes a new rule
plus tests. Note the offending `.bib` entry (minimized) and what you got versus what
you expected, as an [issue](https://github.com/Shannon-Whitlock/VeraCite/issues).

## License

Contributions are licensed under the project's [MIT License](LICENSE).
