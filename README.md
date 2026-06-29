# VeraCite

**A lightweight tool for auditing BibTeX/biblatex bibliographies in scientific
articles for accuracy and conformity — a deterministic check against hallucinated
and mangled citations.**

VeraCite improves the **veracity** of the bibliographic record in scientific
papers. Where BibTeX is notoriously tolerant of imperfect entries, VeraCite
surfaces errors for fast human verification and AI-tool integration, helping
bibliographic records better satisfy the
[FAIR](https://www.go-fair.org/fair-principles/) principles (persistent
identifiers, shared standards, accurate metadata). It confirms
against online records that a reference is real, correctly identified,
and accurately transcribed — catching broken DOIs and the subtly wrong titles, years,
or author lists that humans and LLMs can introduce.

VeraCite is for authors, publishers, and AI assistants who want to vet a
bibliography *before* publication. It checks a `.bib` file along three levels:

- **Syntax** — does it conform to the BibTeX/biblatex datamodel?
- **Semantics** — is each entry consistent with the online record
  (Crossref, arXiv, INSPIRE-HEP, OpenAlex, Open Library)?
- **Context** — (with `--tex`) is each work genuinely cited, and cited
  appropriately, in the manuscript?

It produces both a **human-readable** report and a **machine-readable** JSON
record, each with clear descriptions of every issue and two **0–100 scores** — an
**integrity** score (is the bibliography sound?) and a **confidence** score (how well
were its entries verified?).

VeraCite **never modifies your bibliography or your LaTeX** — it only *flags*
issues, with the offending line and (where possible) a suggested fix, for an author
to inspect and correct. Every finding carries a stable rule **category**
and, for online checks, a `verify:` link, so the report is auditable rather than
a black box.

### Why VeraCite

A bibliography is easy to get wrong and tedious to check by hand: a wrong year,
a mistyped DOI, a page number that doesn't match the published article, a
preprint that has since appeared in a journal or has a correction, or a
misplaced citation that points to the wrong work. These slip through because
BibTeX accepts them without complaint, and checking each entry manually is slow
and error prone. These errors, plus outright **hallucinated references**, now
appear regularly in LLM-assisted drafting, where a confident-looking citation may
point to the wrong work or to a paper that does not exist at all. VeraCite does
that checking for you — deterministically, against the record — and is built to be:

- **Simple to run** — one small Python program you run from the command line. No
  account, no logins, no setup; it works out of the box and needs no extra
  software installed.
- **Trustworthy** — it doesn't guess. Every issue it reports comes from an
  explicit rule or a comparison against a registry record, so you can see
  exactly why each was flagged.
- **Standards-based** — it checks your entries against the official BibTeX/biblatex
  rules, standard journal-name abbreviations, and validated identifiers (DOI,
  arXiv, ISBN, ISSN, ORCID).
- **Private by default** — built to help *you* fix *your own* bibliography before
  submission. Unless you opt in, it **never reads your manuscript and sends nothing
  to any AI service**, so it is safe to run on confidential drafts.

### Auditable by design

VeraCite is deterministic and inspectable. Every rule is a small, deterministic
piece of Python or generated data that an author, publisher or developer can
inspect. The main places to inspect or extend are `veracite/rules.py`,
`veracite/data/biblatex_datamodel.json`, `veracite/report.py`, and
`veracite/verify.py`.

The complete list of every finding category VeraCite can emit — with its default
severity, group, what supersedes it, the source rule(s) that raise it, and a one-line
description — can be obtained via a command-line argument:

```bash
python -m veracite --list-rules          # human-readable table (the audit sheet)
python -m veracite --list-rules json     # same, machine-readable
```

```
category             severity  group     description
--------------------  --------  --------  --------------------------------
duplicate             error     syntax    duplicate citation key or DOI
metadata_mismatch     warning   semantic  author/title/year/... differ
preprint_superseded   warning   context   a published version now exists
title_case            note      semantic  title looks miscased (UPPERCASE)
...
```

### Getting Started

Install VeraCite:

```bash
pip install veracite
```

Run it on a bibliography:

```bash
veracite --bib refs.bib
```

To also check how entries are cited in a manuscript:

```bash
veracite --bib refs.bib --tex main.tex
veracite --bib refs.bib --tex main.tex --llm
```

`--llm` adds a relevance sweep via [Claude Code](https://docs.claude.com/en/docs/claude-code)
(the `claude` CLI, your existing login) — off by default.

If `--bib` is omitted, VeraCite auto-discovers a `.bib` in the current directory.
Run `veracite --help` for the full option list, including `--offline`, `--skipnotes`,
`--sort`, `--json`, `--show-suppressed`, and `--list-rules`.

When one finding makes another redundant, VeraCite **suppresses** the weaker one (e.g.
an online "adopt the record's casing" supersedes the offline "looks miscased" guess) so
you see one clear message, not two. A suppressed finding is still recorded in the JSON
report — stamped with the finding that retracted it — and `--show-suppressed` reveals it
in the terminal (dimmed, with the reason). `veracite --list-rules suppression` prints the
full table of which finding suppresses which, and why.

## Message types

The three levels mean different things and call for different action:

- **`[ERROR]`** — must fix (rarely issued). A syntax error that stops BibTeX parsing.
- **`[WARN]`** — investigate. A discrepancy with the record that may or may not
  be wrong: invalid data formats or deviations from online records, a preprint
  with a published version, a linked erratum, an LLM relevance ≤3.
- **`[note]`** — stylistic or portability recommendations. Hide with
  `--skipnotes`.

### Example output

Findings are grouped into one **block per bibliography entry**, in `.bib`
order by default. The header line identifies the record and the verification
status, followed by each finding in severity order:

```
[ 8/83]  amo2009  @article  line 96  VERIFIED (confidence 0.75); https://doi.org/10.1038/nphys1364
    [WARN]  metadata_mismatch (line 98): [crossref] year differs (suggested: '2009' -> '2010')
    [note]  style (line 101): month '{may}' is not a bare month macro; biblatex will not sort/localize it (suggested: '{may}' -> 'may')
```

Each finding line follows a fixed shape:

```
[SEVERITY] category (line N): message (suggested: 'current' -> 'fixed')
```

## What it checks

Checks run in layers, syntax first — a syntax error halts an entry's later layers,
since comparing a garbled parse against a record only yields false mismatches.

- **Syntax** — unbalanced braces, a missing `=`, an unknown entry type, and a
  **cited key with no entry** are errors; the parser recovers at the next `@entry{`
  so one broken entry doesn't hide the rest.
- **Static** (offline, no network) — biblatex field validity, title
  casing/brace-protection, identifier check digits (DOI/arXiv/ISBN/ISSN/ORCID),
  duplicate keys/DOIs, and other structural rules. See `--list-rules` for the
  full catalog.
- **Record** (online) — resolves each entry by DOI/arXiv id against Crossref,
  arXiv, INSPIRE-HEP, OpenAlex, DataCite, or Open Library and flags disagreement;
  a flagged field carries a suggested edit toward the record. Severity follows
  render-impact: a field that changes the rendered citation (title, author, year,
  journal, volume, pages) is a warning; a stylistic difference is a note. The
  one identity **error** is when first author *and* title both differ strongly —
  the id likely points at a different paper.
- **Status** (online) — retractions, linked errata/corrections, and preprints
  with a published version.
- **Cross-source** (online) — when more than one source resolves an entry, their
  records are compared; a data disagreement warns naming both sources.
- **Verification** (online) — each entry gets a status (**VERIFIED**,
  **UNVERIFIED**, or **MISMATCH**) and a deterministic **confidence** (0–1) based
  on which sources agreed. An entry with no identifier triggers a title/author
  search before being marked unverified.
- **Integrity & confidence scores** (online) — two independent 0–100 scores.
  **Integrity** answers *"is the bibliography sound?"* (docked by each entry's
  worst author-fixable defect). **Confidence** answers *"how much does VeraCite
  trust its own verifications?"* (based on source agreement). They're
  orthogonal: thin corroboration on an otherwise-clean entry lowers confidence,
  not integrity; a field disagreement lowers integrity, not confidence.
- **LLM** (optional, `--llm`, needs `--tex`) — rates each cited entry's relevance
  (1–5) against the surrounding text and flags a clear wrong-paper match. Always
  advisory (`[WARN]` at most, never an error). Uses Claude Code (the `claude`
  CLI, your existing login); sends the cited sentence(s) to the provider, so it's
  off by default.

## Machine-readable report (`--json`)

`--json FILE` writes the report as **NDJSON** (newline-delimited JSON): one
self-contained record per bibliography **entry**, keyed by its citation key and
carrying everything about it — `entry_type`, source `line`, its computed `phases`
(see [Checkpointing](#checkpointing-and-phased-resume)), `status`/`confidence`,
the `verify` link, `identifiers`, matched `canonical_record`, the `sources` that
resolved it, and its `issues`. The terminal report is a pretty-print of these
records, so it is fully reconstructible from the NDJSON alone:

```jsonc
{"key": "amo2009", "veracite_version": "0.2.0", "entry_type": "article",
 "line": 96, "uncited": false,
 "phases": {"offline": true, "online": true, "llm": false},
 "status": "VERIFIED", "confidence": 1.0, "status_detail": "",
 "verify": "https://doi.org/10.1038/nphys1364",
 "identifiers": {"doi": "10.1038/nphys1364", "arxiv": null, "isbn": null},
 "sources": ["crossref", "inspire"], "canonical_record": {"title": "...", "year": 2009},
 "issues": []}
```

### Using VeraCite with automated tools (beware)

VeraCite is **read-only** — it never edits your `.bib`. That makes it safe to call
from an AI agent or CI pipeline as a **verification gate**, with a separate,
human-supervised step applying any fix. Keep the checker and the editor separate:
let VeraCite decide *what's wrong*, never let an agent decide *that's fine* on its
behalf.

A fixable finding's `suggested` field is structured —
`{"field": ..., "from": ..., "to": ...}` — so a tool can apply it as data rather
than parsing English. But **only apply `[ERROR]`/`[WARN]` suggestions with a
human in the loop**: a `suggested` edit conforms the bib toward the matched
record, and on a weak or ambiguous match that record could itself be wrong.
`group` (`syntax`/`semantic`/`context`) tells a caller how much judgement a
finding needs before acting on it.

## Checkpointing and phased resume

An online run on a large bibliography is slow (a few paced network calls per
entry), so a crash shouldn't throw the work away. With `--json report.ndjson`,
VeraCite rewrites the file atomically after each entry that changes, so a crash
at any point leaves a complete, duplicate-free file and loses at most the entry
in flight. Point it at an existing report and it resumes:

```bash
python -m veracite --bib refs.bib --offline --json report.ndjson   # phase 1: fast, no network
python -m veracite --bib refs.bib          --json report.ndjson   # phase 2: resume, resolve online
python -m veracite --bib refs.bib --tex p/ --json report.ndjson --llm  # phase 3: add LLM ratings
```

Each entry is rechecked **only if it's missing layers** — an entry already
verified is reused, spending no network or tokens, unless its `.bib` text
has changed since the saved run.

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
  "request_delay": 0.2,
  "request_timeout": 20,
  "endpoints": {"crossref_work": "https://api.crossref.org/works/{doi}"}
}
```

- `contact_email` is added to the User-Agent (Crossref/OpenAlex "polite pool");
  may also be set with `VERACITE_CONTACT_EMAIL`.
- `llm_provider` selects the `--llm` backend; for now the only one is `claude`
  (Claude Code, via the `claude` CLI and your existing login).
- `llm_models` pins the model per provider. The default is **Claude Haiku**
  (`claude-haiku-4-5-20251001`) — a pinned id for reproducible ratings, ample for a
  per-citation relevance rating. If it is ever retired, `--llm` reports `rating
  unavailable` — set `llm_models` to a current id to fix it. Point it at a larger
  model (e.g. Sonnet) for tougher calls.
- `severity` re-ranks any finding category to `error`/`warning`/`note`.
- `protected_terms` is the project's must-stay-capitalized title terms.
- `request_delay`/`request_timeout` set API pacing; `--delay`/`--timeout` override
  them. Pacing is **per service and time-based**: each service has a minimum
  interval (default 0.2 s; arXiv 3 s) and a request waits only the *remainder* —
  time spent elsewhere counts, so an entry resolved by Crossref never pays an arXiv
  delay and only a real outbound request ever waits.
- `endpoints` repoints the external API URLs if a service moves.

## How to cite

If VeraCite is useful in your work, please cite it.

```bibtex
@software{whitlock_veracite,
  author = {Whitlock, Shannon},
  title  = {{VeraCite}: a deterministic auditor for {BibTeX}/{biblatex} bibliographies},
  year   = {2026},
  doi    = {10.5281/zenodo.20963060},
  url    = {https://github.com/Shannon-Whitlock/VeraCite},
}
```

Plain text: Shannon Whitlock. *VeraCite: a deterministic auditor for
BibTeX/biblatex bibliographies*, 2026. https://doi.org/10.5281/zenodo.20963060

## Requirements

- Python 3.8+. Uses `requests` if present, else the stdlib `urllib`.
- Network (for the online layers): `api.crossref.org`, `export.arxiv.org`,
  `api.openalex.org`, `api.semanticscholar.org`, `inspirehep.net` (physics),
  `api.datacite.org` (software/dataset DOIs — Zenodo, figshare, Dryad),
  `openlibrary.org` / `googleapis.com` (ISBN). All optional and degrade
  gracefully — a source that fails to respond is reported as "could not retrieve",
  never a crash, and `--offline` skips them all.
- For `--llm` with the default provider: the [`claude`
  CLI](https://docs.claude.com/en/docs/claude-code) on `PATH`, **logged in** (run
  `claude` once and sign in; it needs a Claude account).

## Known limitations

VeraCite compares against registry **metadata**; errors in free text or in
fields no registry encodes are out of reach. A `url` field is **not** fetched
or validated (resolving an arbitrary URL from a `.bib` is a security risk), so
link rot is not detected. Correction/erratum and published-version coverage is
best-effort. "No problem found" means no problem in the checkable fields, not
that every field was verified.

## Tests

```bash
pip install -e ".[test]"
python -m pytest
```

## Contributing

Contributions are welcome — especially **false positives** (a clean entry that got
flagged) and **false negatives** (a real defect that slipped through), which feed
VeraCite's self-improving loop. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup,
workflow, and how to add a rule; the design principles every change must uphold are
in [`CLAUDE.md`](CLAUDE.md).
