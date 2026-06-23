# VeraCite

**A lightweight, auditable tool for checking the accuracy and conformity of
BibTeX/biblatex bibliographies in scientific articles.**

VeraCite improves the **veracity** of the bibliographic record in scientific
papers. Where BibTeX is notoriously tolerant of imperfect entries, VeraCite
surfaces errors for fast human verification and AI-tool integration, helping
bibliographic records better satisfy the
[FAIR](https://www.go-fair.org/fair-principles/) principles (persistent
identifiers, shared standards, accurate metadata).

VeraCite is for authors, publishers, and AI assistants who want to vet a
bibliography *before* publication. It checks a `.bib` file along three levels:

- **Syntax** — does it conform to the BibTeX/biblatex datamodel?
- **Semantics** — is each entry consistent with the authoritative online record
  (Crossref, arXiv, INSPIRE-HEP, OpenAlex, Open Library)?
- **Context** — (with `--tex`) is each work genuinely cited, and cited
  appropriately, in the manuscript?

It produces both a **human-readable** report and a **machine-readable** JSON
record, each with clear descriptions of every issue and an overall **0–100
integrity score**.

VeraCite **never modifies your bibliography or your LaTeX** — it only *flags*
issues, with the offending line and (where possible) a suggested fix, for an author
to inspect and correct. Every finding carries a stable rule **category**
and, for online checks, a `verify:` link, so the report is auditable rather than
a black box.

### Why VeraCite

A bibliography is easy to get wrong and tedious to check by hand: a wrong year,
a mistyped DOI, a page number that doesn't match the published article, a
preprint that has since appeared in a journal, or a misplaced citation that
points to the wrong work. These slip through because BibTeX accepts
them without complaint — and checking each entry against the real record is slow and error prone. VeraCite does that checking for you, and is built to be:

- **Simple to run** — one small Python program you run from the command line. No
  account, no website, no setup; it works out of the box and needs no extra
  software installed.
- **Trustworthy** — it doesn't guess. Every issue it reports comes from an
  explicit rule or a comparison against an authoritative record, so you can see
  exactly why each was flagged. The optional AI relevance check is **off by
  default**.
- **Standards-based** — it checks your entries against the official BibTeX/biblatex
  rules, standard journal-name abbreviations, and validated identifiers (DOI,
  arXiv, ISBN, ISSN, ORCID).
- **Private by default** — built to help *you* fix *your own* bibliography before
  submission. Unless you opt in, it **never reads your manuscript and sends
  nothing to any AI service**, so it is safe to run on confidential drafts.

### Auditable by design

VeraCite's checks are not arbitrary or hidden in model weights. Every rule is a
small, deterministic piece of Python or generated data that an author, publisher or
developer can read, correct, and extend. The four places to look:

| What | Where | How to inspect / extend |
|------|-------|-------------------------|
| **Static checks** (the rule registry) | [`veracite/rules.py`](veracite/rules.py) | Each check is a function decorated `@rule` (per entry) or `@file_rule` (whole file) and appended to a registry the engine iterates. Add a check by writing one function; the module docstring marks it *"the part meant to be read and edited."* |
| **Structural validity** (legal & mandatory fields) | [`veracite/data/biblatex_datamodel.json`](veracite/data/biblatex_datamodel.json), loaded by [`veracite/datamodel.py`](veracite/datamodel.py) | Generated from biblatex's **own** `blx-dm.def` by [`tools/gen_datamodel.py`](tools/gen_datamodel.py) — not a hand-kept blocklist. Regenerate when biblatex updates. |
| **Severity, grouping & descriptions** (what's an error vs. a note, the syntax/semantic/context bucket, and the catalog text) | `resolve_severity()`, `CATEGORY_GROUP`, and `CATEGORY_DOC` in [`veracite/report.py`](veracite/report.py); defaults in `DEFAULT_SETTINGS["severity"]` ([`veracite/config.py`](veracite/config.py)) | Every finding carries a stable string **category**. List the whole catalog with `--list-rules`; re-rank any category to `error`/`warning`/`note` via the `severity` block in a settings file (see [Configuration](#configuration)) — no code change needed. |
| **Integrity score** (the 0–100 roll-up) | `integrity()` in [`veracite/verify.py`](veracite/verify.py) | A transparent weighted formula over explicit counts — `0.50·verification + 0.20·PID + 0.15·DOI + 0.15·(1 − defects)` — **not** a model output. |

Start from the catalog — the complete list of every finding category VeraCite can
emit, with its default severity, group, what supersedes it, and a one-line
description:

```bash
python -m veracite --list-rules          # human-readable table (the audit sheet)
python -m veracite --list-rules json     # same, machine-readable
```

```
category                  severity  group     superseded by  description
------------------------  --------  --------  -------------  ----------------------------------------
duplicate                 error     syntax    -              duplicate citation key or DOI ...
metadata_mismatch         warning   semantic  -              author/title/year/vol/pages/journal differ
preprint_superseded       warning   context   -              a published version now exists
title_case                note      semantic  record layer   title looks miscased (mostly UPPERCASE)
...
```

### Getting Started

Point it at a `.bib` file; it reports structural, stylistic, and record-level
problems for a human to read and a script to parse. Add `--tex` to also check how
the bibliography is cited.

```bash
python -m veracite --bib refs.bib            # check every entry; reads no .tex
python -m veracite --bib refs.bib --tex paper/   # check only cited entries
python -m veracite --bib refs.bib --offline  # static checks only (no network)
python -m veracite --bib refs.bib --tex paper/ --llm   # + LLM relevance sweep
python -m veracite --bib refs.bib --skipnotes          # warnings and errors only
python -m veracite --bib refs.bib --sort severity      # global triage list, errors first
python -m veracite --bib refs.bib --json report.json
python -m veracite --list-rules                        # the rule catalog / audit sheet
```

Installed (`pip install .`) it also exposes a `veracite` command.

`--bib FILE` selects the bibliography; if omitted it is auto-discovered under the
cwd. VeraCite runs in one of two modes:

- **bibliography-only** (no `--tex`): every entry is checked. **No `.tex` file is
  ever read** — the default run never touches your manuscript, so it is safe on
  confidential drafts.
- **citations** (`--tex PATH`, a file or directory, repeatable): only the entries
  cited by those sources are resolved online and (with `--llm`) rated; uncited
  entries are noted and skipped. A cited key with no `.bib` entry is an error.

`.tex` is read only when you ask for it with `--tex`; there is no silent
auto-discovery. Online checks are on by default; `--offline` makes the run fully
offline. The LLM relevance sweep is **off** unless `--llm` is given, and `--llm`
**requires `--tex`** (it needs the citation context). Every layer runs per entry
in bibliography order, so the report is a single list in `.bib` order — each
entry's findings printed once, followed by a file-level group and the summary.

Exit status is non-zero when any error is found, so it can gate CI.

## Message types

The three levels mean different things and call for different action:

- **`[ERROR]`** — must fix. A structural/syntax error that stops BibTeX from
  parsing (unbalanced braces, a missing `=`, an unknown entry type, a dropped
  reference); a duplicate; a retraction; a dead DOI; an id that resolves to a
  different paper (first author **and** title both differ); or an LLM-flagged
  clearly-wrong paper.
- **`[WARN]`** — investigate. A discrepancy between the record and the bib that
  may or may not be wrong: an author/title/given-name/year/volume/pages field
  differs from the id-resolved record, a non-standard journal abbreviation, a
  preprint with a published version, a linked erratum, or an LLM relevance ≤3.
  Open the `verify:` link and decide.
- **`[note]`** — stylistic, or filtered by biblatex anyway: casing,
  brace-protection, dashes, a name month, an invalid-for-biblatex field, an
  abbreviated given name, or a registry-parity suggestion. Hide with
  `--skipnotes` (still counted in the summary).

### Example output

Findings are grouped into one **block per bibliography entry**, in `.bib` order. A
block opens with a header line that identifies the record and the verification status,
then lists each finding indented beneath it (in severity order):

```
[ 8/83]  amo2009  @article  line 96  VERIFIED (confidence 0.75); https://doi.org/10.1038/nature07640
    [WARN]  metadata_mismatch (line 98): [crossref] year differs: bib=2009, record=2010
    [note]  style (line 101): month 'may' is a name; biblatex will not sort it (suggested: 'may' -> '5')
```

The header carries everything that identifies and verifies the record (an optional
`[i/N]` progress counter, the key, `@type`, line, status, and confidence with a
`verify:` link); a clean VERIFIED entry with no other findings prints no block at
all. Each finding line follows one fixed shape:

```
[SEVERITY] category (line N): message (suggested: 'current' -> 'fixed')
```

- `[SEVERITY]` is `[ERROR]`/`[WARN]`/`[note]`.
- `category` is the **stable rule code** (see `--list-rules`); every finding has
  one — none falls back to a bare layer name.
- `(line N)` is the offending field's line in the `.bib`.
- a fixable finding carries the advisory edit inline as `current -> suggested`.
- a message never wraps: any embedded newline is folded, so one finding = one line.

## What it checks

Checks run in layers, syntax first.

0. **Syntax** — structural validity, so a file BibTeX cannot parse is never
   reported as healthy. Unbalanced braces, a stray extra `}`, a field missing
   its `=`, an unknown entry type, a duplicate field, a file-level brace
   imbalance, and a **cited key with no entry** are each errors. The parser
   recovers at the next `@entry{`, so one broken entry does not hide the others.
   `@string` abbreviations (both `{…}` and `(…)` delimited) and `#` concatenation
   are expanded, so a `journal = prb` macro is checked by its full value, not the
   bare macro name.

1. **Static** (offline) — a rule registry (`rules.py`); add a check by writing a
   function and decorating it `@rule`/`@file_rule`. Covers missing fields;
   **biblatex field validity** derived from the standard datamodel (see below);
   title casing/brace-protection; trailing periods; `and others`; arXiv-id
   consistency; page/dash/numpages sanity; encoding; DOI format; duplicate
   keys/DOIs; and file-wide consistency. Uncited entries are noted.

2. **Record** (online) — resolve each entry by DOI (Crossref) or arXiv id and
   flag **disagreement** with the record. The id already establishes identity, so
   a field that disagrees (title, author, given name, year/volume/pages, journal)
   is a **warning** to verify, not a wrong-paper claim — name folding handles
   suffixes (`Jr`/`III`), particles, collaborations, and abbreviated given names
   so these don't misfire. A journal name matches the record when it is a known
   abbreviation (a small curated physics table in `veracite/data/`) or a valid
   ISO-4 abbreviation (period-insensitive, so `Phys. Rev. B` and `Phys Rev B` both
   match `Physical Review B`); only a genuinely non-standard journal string warns.
   The one identity **error** is when the first author *and* the title both differ
   strongly: the id likely resolves to a different paper (a copy-pasted DOI). A
   `verify:` link is printed for every entry with an online finding.

3. **Status** (online) — retraction (via OpenAlex / Retraction Watch), linked
   errata/corrections/comments/replies, and preprints with a published version.

4. **Cross-source** (online) — when more than one authoritative source resolves an
   entry (Crossref, **INSPIRE-HEP** for physics, arXiv, Open Library for books),
   their records are compared *against each other*. A data difference (year,
   volume, issue, pages, or a genuinely different journal) is a **warning**
   (`source_conflict`) naming both sources. Purely stylistic differences — title
   casing, or a full journal title vs its ISO-4 abbreviation — are **not** flagged,
   since both forms are valid. This surfaces stale or corrupted registry metadata
   the single-source comparison cannot see.

5. **Verification** (online) — each entry gets one of three statuses with a
   **confidence** (0–1, a deterministic function of which sources agreed, not a
   model output). **VERIFIED** — the id resolved and the first author and title
   match; confidence reflects corroboration: **1.0** (clean match across ≥2
   sources), **~0.95** (clean single source), **0.75** (a field disagrees), **0.70**
   (sources disagree, or only arXiv confirms). **UNVERIFIED** — could not confirm:
   no identifier, no record returned, or a DOI that did not resolve (also an error).
   **MISMATCH** — it resolved but the record's identity disagrees (the id may point
   at a different paper). If a bib omits a DOI, VeraCite **searches** Crossref (title
   + first author, corroborated by journal or ±1-year) and, on a strong match,
   reports the DOI to add and uses it to verify. A post-2005 article with no findable
   DOI is flagged; pre-2005 work is not penalized; arXiv ids and ISBNs count as PIDs.

6. **Integrity score** (online) — a summary roll-up: counts of
   verified (and how many carry a caveat), unverified, mismatch, DOI coverage over eligible (post-2005)
   articles, PID coverage, and a **0–100 integrity score** — a transparent weighted
   blend of verification rate (50%), PID coverage (20%), DOI coverage (15%), and
   freedom from integrity defects (15%). Printed beneath the verdict.

7. **LLM** (optional, `--llm`, needs `--tex`) — for each cited entry, a language
   model rates **relevance** (1–5) from the abstract and the surrounding sentences,
   and flags a clear **wrong paper**. For a grouped citation (`\cite{a,b,c}`) it also
   sees the co-cited references and drops a low-relevance (≤3) odd-one-out a further
   point, surfacing an inappropriate citation hidden in a list of relevant ones. A
   wrong-paper flag is an error; relevance ≤3 a warning; 4–5 silent. Findings are
   worded as tentative, abstract-only opinions to verify, never authoritative
   judgements. The provider is pluggable (`llm.py`), but **for now the only
   supported backend is Claude Code** (the `claude` CLI, using your existing login),
   and it defaults to **Claude Haiku** for token efficiency — fast and inexpensive
   for a per-citation rating. **Privacy:** `--llm` sends those cited sentences to the
   provider, so it is off by default and prints a warning — do not use it on a
   confidential manuscript.

Identifier formats (DOI, arXiv, **ISBN**, **ISSN**, **ORCID**) are checked offline
by their check digits. An entry with a structural **syntax error** is reported, and
the rest of its checks (record, status, cross-source, LLM) are skipped until it
parses cleanly — comparing a garbled parse against a record only yields false
mismatches. When `--tex` is given, a multi-key `\cite{}` **group that is not in
chronological order** gets an advisory note (some bibliography styles cite the
earliest work first); it is never an error, since grouped-citation order is a
style choice, not a standard.

## Machine-readable report (`--json`)

`--json FILE` writes a JSON report with a `findings` list (every finding), a
`summary` block (the integrity-score metrics), and a per-reference `references`
array — each with its `status`, `confidence`, `identifiers`, the matched
`canonical_record`, the `sources` that resolved it, and its `issues`:

```json
{
  "summary": {"checked": 152, "verified": 151, "verified_with_caveat": 8,
              "unverified": 1, "mismatch": 0,
              "doi_coverage": 0.94, "pid_coverage": 0.97, "integrity_score": 97},
  "references": [
    {"key": "amo2009", "status": "VERIFIED", "confidence": 1.0,
     "identifiers": {"doi": "10.1038/nature07640", "arxiv": null, "isbn": null},
     "sources": ["crossref", "inspire"], "canonical_record": {"title": "...", ...},
     "issues": []}
  ],
  "findings": [ ... ]
}
```

All three top-level keys (`summary`, `references`, `findings`) are always present.
Under `--offline` there is no online verification, so the `summary` records the
offline mode and finding counts with a null score
(`{"mode": "offline", "integrity_score": null, "errors": …, "warnings": …, "notes": …}`)
and `references` is empty — a stable shape to parse, never a fabricated score.

## Configuration

VeraCite runs with no configuration. Optional settings are read from the first
of `./veracite.json`, `~/.config/veracite/settings.json`, `~/.veracite.json`, or
a `--settings FILE` path. None is shipped, so the tool carries no personal data.
Recognized keys (all optional):

```json
{
  "contact_email": "you@example.org",
  "llm_provider": "claude",
  "llm_models": {"claude": "claude-haiku-4-5-20251001"},
  "document_context": "a paper on <your topic>",
  "protected_terms": ["Rydberg", "Yb", "Pulser"],
  "severity": {"preprint_superseded": "error", "biblatex_validity": "note"},
  "request_delay": 0.4,
  "request_timeout": 20,
  "endpoints": {"crossref_work": "https://api.crossref.org/works/{doi}"}
}
```

- `contact_email` is added to the User-Agent (Crossref/OpenAlex "polite pool");
  may also be set with `VERACITE_CONTACT_EMAIL`.
- `llm_provider` selects the `--llm` backend. For now the only supported provider
  is `claude` (Claude Code, via the `claude` CLI and your existing login).
- `llm_models` pins the model used per provider. The default is **Claude Haiku**
  (`claude-haiku-4-5-20251001`) — chosen for token efficiency, ample for a
  per-citation relevance rating. It is a specific, pinned id for reproducible
  ratings; if that model is ever retired, `--llm` will report `rating unavailable:
  claude CLI failed (model '...')` — set `llm_models` to a current id to fix it, no
  code change needed. Point it at a larger model (e.g. Sonnet) for tougher calls.
- `severity` re-ranks any finding category to `error`/`warning`/`note`.
- `protected_terms` is the project's must-stay-capitalized title terms.
- `request_delay`/`request_timeout` set API pacing; `--delay`/`--timeout`
  override them.
- `endpoints` repoints the external API URLs if a service moves.

## Layout

```
veracite/        package: config, parser, normalize, datamodel, report,
                 rules, record, llm, cli
tools/           gen_datamodel.py (regenerates the datamodel JSON)
tests/           pytest suite + .bib fixtures
```

## Requirements

- Python 3.8+. Uses `requests` if present, else the stdlib `urllib`.
- Network (for the online layers): `api.crossref.org`, `export.arxiv.org`,
  `api.openalex.org`, `api.semanticscholar.org`, `inspirehep.net` (physics),
  `openlibrary.org` / `googleapis.com` (ISBN). All optional and degrade
  gracefully — a source that fails to respond is reported as "could not retrieve",
  never a crash, and `--offline` skips them all.
- For `--llm` with the default provider: the [`claude`
  CLI](https://docs.claude.com/en/docs/claude-code) on `PATH`, **logged in** (run
  `claude` once and sign in; it needs a Claude account). `--llm` probes the
  provider before the run and, if it is missing or not logged in, stops up front
  with how to fix it rather than failing per entry. Everything except `--llm`
  works with no account.

## Known limitations

VeraCite compares against registry **metadata**; errors in free text or in
fields no registry encodes are out of reach. Correction/erratum and published-version coverage is best-effort. "No problem found" means no problem in the checkable fields, not
that every field was verified.

## Tests

```bash
pip install pytest
python -m pytest
```
