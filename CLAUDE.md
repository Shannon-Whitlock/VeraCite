# CLAUDE.md — the VeraCite contributor charter

VeraCite audits BibTeX/biblatex bibliographies to catch hallucinated, mangled, or
mis-identified citations before publication. Three docs, three audiences:
`README.md` is for users, [`CONTRIBUTING.md`](CONTRIBUTING.md) is the mechanics for
human contributors (setup, workflow, tests, code style), and **this file is the
*principles* — the contract for changing the code** that every contributor, human or
AI, must follow. When a change conflicts with a principle here, the principle wins;
if the principle itself is wrong, change *it* first, in its own commit, with the
reasoning. The "stop and flag the trade-offs" steps below are about not over-editing;
they apply to anyone proposing a change, whether or not an agent is driving.

## Trust is the #1 priority

Trust is the whole product: one false positive on a clean entry makes the user
distrust *all* output, so **a false positive is the cardinal sin — prefer silence to
a wrong flag**, tightening a rule even at some cost to recall, never the reverse.

VeraCite is **read-only**: it flags, never edits. **Crossref (by DOI) is the
canonical metadata resource** — the closest public stand-in for the publisher of
record — and arXiv/INSPIRE/OpenAlex/Open Library corroborate it. A `suggested:` edit
always conforms the bib *toward* that record, unless the record is clearly broken.

## Every message must suggest an action

- If a finding implies nothing to do, don't emit it — silence is the clean pass.
- Output stays parseable: one finding per line, no repeated information, carrying a
  stable `category`, the offending line, and a structured `suggested` patch when one
  exists.
- Repeating the same issue across entries is fine, but when one fix resolves several
  findings, suppress the dependents (via `SUPERSEDES`).
- The output must be fully reconstructible from the NDJSON record source, whether or
  not `--json` is used. There is **one** per-entry record builder
  (`checkpoint.entry_record`) and **one** terminal renderer
  (`Report.render_entry_record`, which pretty-prints that record). Both `--json` and
  the live terminal report flow through them, so the screen can show nothing the
  record lacks — never add a second formatting path. A round-trip test
  (`test_terminal_block_reconstructs_from_ndjson_record`) is the ratchet.

## Severity means a specific kind of action

| level | meaning | examples |
|-------|---------|----------|
| `[ERROR]` | a syntax error or clearly-broken citation — **must fix** | unbalanced braces, missing `=`, dead DOI, duplicate, id resolves to a different paper |
| `[WARN]` | a real data problem hurting accuracy — **investigate** | year/author/title/volume/pages/journal disagree with the record; sources conflict |
| `[note]` | stylistic or a completeness/portability nudge — usually no render effect | casing, dashes, brace-protection, an abbreviated given name, an invalid-for-biblatex field |

## Everything is a rule

No guessing in the verification path — every finding is a comparison against an authoritative record or a deterministic rule (`--llm` is the sole exception).

- A per-entry check is `@rule def fn(entry, report)`; a whole-file check is
  `@file_rule def fn(entries, report)` — both in [`rules.py`](veracite/rules.py).
- Every finding carries a stable `category` that sets its severity, group, and
  catalog text — the public identity of the check.
- The category's metadata lives in [`report.py`](veracite/report.py)
  (`CATEGORY_DOC`, `CATEGORY_GROUP`, `SUPERSEDES`, `resolve_severity`) with its
  default severity in `DEFAULT_SETTINGS["severity"]`
  ([`config.py`](veracite/config.py)); [`catalog.py`](veracite/catalog.py)
  introspects those to build `--list-rules`, so it cannot drift. When you add or
  change a category, update all four and run `python -m pytest` —
  `test_catalog.py` fails if any is missing.

## Writing a rule that doesn't misfire

A rule must be **principled** and **general enough** to catch a broad class, narrow enough to **never fire on a valid entry**.

- Generalize to the underlying defect, not the one input that surfaced it.
- Prefer structural signals (check digits, datamodel legality, brace/quote balance)
  over free-text heuristics, which are the likeliest to misfire.
- Check every proposed rule change against these principles before writing it —
  even when it comes from the user.
- Fold away legitimate variation before comparing (name particles/suffixes, ISO-4
  journal abbreviations, brace/quote wrapping, `--` vs `-`) — each unfolded thing is
  a false positive waiting.
- Attach `suggested=` only when certain of the target; otherwise report the
  discrepancy without one.
- An offline guess the authoritative record later disproves must be **withdrawn, not
  shipped**. When an entry resolves, the record is ground truth, so a heuristic the
  record can check must never survive the resolve — declare the supersession in
  `SUPERSEDES` (the "record layer") or `rep.withdraw()` it. *Example:* a record whose
  issue corroborates the bib's `number` kills the offline "that's a misplaced year"
  guess.
- A style/`[note]` recommendation must be **grounded in a BibTeX/biblatex data
  standard** (the datamodel, ISO-4, the biber sort/date rules, a standard journal
  abbreviation), never personal taste — if you can't name the standard it enforces,
  it isn't a rule.

## Self-improving by design

VeraCite should **get more accurate after every stress test** — a virtuous cycle, not
a growing pile of rules: run it on a real bibliography, hand-audit the output against
the record, and feed each false positive/negative back as a *generalized* rule plus
tests. This file is the loop's memory: a durable principle learned in one session
must be written here so it is enforced in the next.

- Generalize, don't proliferate — ten symptoms of one gap is one rule change, and two
  categories that fire on the same defect should be merged into one.
- A change must close a recall gap, never flip a clean entry into an error (the
  real-world failure is omission, not false assertion).
- Never push a bad value — validate before suggesting, withhold mangled values, and
  cap weak-match confidence so it can't overwrite toward a wrong reference.
- Every cycle ships positive and negative tests for the class — the suite is the
  ratchet that keeps the cycle monotonic.

## Before proposing a patch

1. Reproduce and find the **true root cause** (not a diagnostic artifact).
2. **Enumerate the candidate fixes** — for each: what it catches, what it might miss,
   how it could falsely fire.
3. **Flag the best option with trade-offs, and wait** — don't reflexively edit beyond
   a trivial, obviously-correct change.
4. **Test the class, not the fixture** — including the look-alike valid inputs the
   rule must not flag (`synthesis` must not match a `thesis` rule).

## Commands

```bash
python -m pytest                                   # full suite — keep green
python -m veracite --list-rules                    # the audit catalog
python -m veracite --bib refs.bib --offline        # static/syntax only, no network
python -m veracite --bib refs.bib --tex p/         # online + citation context
python -m veracite --bib refs.bib --tex p/ --llm   # + LLM relevance sweep (sends text)
python -m veracite --bib refs.bib --key SmithVerify  # re-check ONE entry (after a fix)
```

`--key KEY` focuses the whole run on a single citation key — only that entry is
resolved and only its findings are reported (plus file-level). Use it to verify a
specific record after applying a suggested fix, without re-running the entire
bibliography.

Integrity score and confidence are transparent deterministic formulas
([`verify.py`](veracite/verify.py)), not model outputs — keep them that way.
