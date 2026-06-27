# VeraCite

**A lightweight, auditable tool for checking the accuracy and conformity of
BibTeX/biblatex bibliographies in scientific articles — a deterministic check
against hallucinated and mangled citations.**

VeraCite improves the **veracity** of the bibliographic record in scientific
papers. Where BibTeX is notoriously tolerant of imperfect entries, VeraCite
surfaces errors for fast human verification and AI-tool integration, helping
bibliographic records better satisfy the
[FAIR](https://www.go-fair.org/fair-principles/) principles (persistent
identifiers, shared standards, accurate metadata). It confirms
against online records that a reference is real, correctly identified,
and accurately transcribed — catching broken DOIs and the subtly wrong title, year,
or author list that humans and LLMs can introduce.

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
preprint that has since appeared in a journal or has a correction, or a misplaced citation that
points to the wrong work. These slip through because BibTeX accepts
them without complaint, and checking each entry against the real record is slow and error prone. These errors, plus outright **hallucinated references**, now
appear regularly in LLM-assisted drafting, where a confident-looking citation
may point to the wrong work or to a paper that does not exist at all.
VeraCite does that checking for you — deterministically, against the record — and is built to be:

- **Simple to run** — one small Python program you run from the command line. No
  account, no logins, no setup; it works out of the box and needs no extra
  software installed.
- **Trustworthy** — it doesn't guess. Every issue it reports comes from an
  explicit rule or a comparison against an authoritative record, so you can see
  exactly why each was flagged.
- **Standards-based** — it checks your entries against the official BibTeX/biblatex
  rules, standard journal-name abbreviations, and validated identifiers (DOI,
  arXiv, ISBN, ISSN, ORCID).
- **Private by default** — built to help *you* fix *your own* bibliography before
  submission. Unless you opt in, it **never reads your manuscript and sends nothing
  to any AI service**, so it is safe to run on confidential drafts.

### Auditable by design

VeraCite's checks are not arbitrary or hidden in model weights. Every rule is a
small, deterministic piece of Python or generated data that an author, publisher or
developer can read, correct, and extend. The four places to look:

| What | Where | How to inspect / extend |
|------|-------|-------------------------|
| **Static checks** (the rule registry) | [`veracite/rules.py`](veracite/rules.py) | Each check is a function decorated `@rule` (per entry) or `@file_rule` (whole file) and appended to a registry the engine iterates. Add a check by writing one function; the module docstring marks it *"the part meant to be read and edited."* |
| **Structural validity** (legal & mandatory fields) | [`veracite/data/biblatex_datamodel.json`](veracite/data/biblatex_datamodel.json), loaded by [`veracite/datamodel.py`](veracite/datamodel.py) | Generated from biblatex's **own** `blx-dm.def` by [`tools/gen_datamodel.py`](tools/gen_datamodel.py) — not a hand-kept blocklist. Regenerate when biblatex updates. |
| **Severity, grouping & descriptions** (what's an error vs. a note, the syntax/semantic/context bucket, and the catalog text) | `resolve_severity()`, `CATEGORY_GROUP`, and `CATEGORY_DOC` in [`veracite/report.py`](veracite/report.py); defaults in `DEFAULT_SETTINGS["severity"]` ([`veracite/config.py`](veracite/config.py)) | Every finding carries a stable string **category**. List the whole catalog with `--list-rules`; re-rank any category to `error`/`warning`/`note` via the `severity` block in a settings file (see [Configuration](#configuration)) — no code change needed. |
| **Integrity score** (the 0–100 rating) | `integrity()` in [`veracite/verify.py`](veracite/verify.py) | A transparent weighted formula over explicit counts — `0.50·verification + 0.20·PID + 0.15·DOI + 0.15·(1 − defects)` — **not** a model output. |

The complete list of every finding category VeraCite can emit — with its default
severity, group, what supersedes it, the source rule(s) that raise it, and a one-line
description — can be obtained via a command-line argument:

```bash
python -m veracite --list-rules          # human-readable table (the audit sheet)
python -m veracite --list-rules json     # same, machine-readable
```

```
category                  severity  group     superseded by  rules (in source)                                   description
------------------------  --------  --------  -------------  --------------------------------------------------  ----------------------------------------
duplicate                 error     syntax    -              duplicate_keys_and_dois                             duplicate citation key or DOI ...
metadata_mismatch         warning   semantic  -              _compare_authors, compare_against_record, ...       author/title/year/vol/pages/journal differ
preprint_superseded       warning   context   -              resolve_entry                                       a published version now exists
title_case                note      semantic  record layer   compare_against_record, title_caps                  title looks miscased (mostly UPPERCASE)
...
```

(The `rules (in source)` column names the function(s) — in `rules.py`, `compare.py`,
or `record.py` — that raise each category, so the audit sheet points straight at the
code; a category raised in several places lists them all. The `json` form gives the
same data with each rule's file and line.)

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

Online checks are on by default; `--offline` makes the run fully
offline. The LLM relevance sweep is **off** unless `--llm` is given, and `--llm`
**requires `--tex`** (it needs the citation context). Every layer runs per entry
in bibliography order. By default (`--sort entry`) the report follows that order —
one block per entry, its findings printed once, then a file-level group and the
summary. `--sort severity` instead prints a single global triage list, errors first
(each line keyed), followed by the same summary.

Exit status is non-zero when any error is found, so it can gate CI.

## Message types

The three levels mean different things and call for different action:

- **`[ERROR]`** — must fix (rarely issued). A syntax error that stops BibTeX parsing (unbalanced
  braces, a missing `=`, an unknown entry type, a dropped reference), a duplicate, a
  retraction, a dead DOI, or an id that resolves to a different paper. Always from a
  deterministic check or an authoritative record — **never** the LLM.
- **`[WARN]`** — investigate. A discrepancy with the record that may or may not be
  wrong: an author/title/year/volume/pages field differs, a non-standard journal
  abbreviation, a preprint with a published version, a linked erratum, an LLM
  relevance ≤3. Open the `verify:` link and decide.
- **`[note]`** — stylistic or portability: casing, brace-protection, dashes, a name
  month, an invalid-for-biblatex field, an abbreviated given name. Hide with
  `--skipnotes` (still counted in the summary).

### Example output

Findings are grouped into one **block per bibliography entry**, in `.bib` order. A
block opens with a header line that identifies the record and the verification status,
then lists each finding indented beneath it (in severity order):

```
[ 8/83]  amo2009  @article  line 96  VERIFIED (confidence 0.75); https://doi.org/10.1038/nphys1364
    [WARN]  metadata_mismatch (line 98): [crossref] year differs (suggested: '2009' -> '2010')
    [note]  style (line 101): month '{may}' is not a bare month macro; biblatex will not sort/localize it (suggested: '{may}' -> 'may')
```

The header carries everything that identifies and verifies the record (an optional
`[i/N]` progress counter, the key, `@type`, line, status, and confidence with a
`verify:` link); a clean VERIFIED entry with no other findings prints no block at
all. Each finding line follows one fixed shape:

```
[SEVERITY] category (line N): message (suggested: 'current' -> 'fixed')
```

- `[SEVERITY]` is `[ERROR]`/`[WARN]`/`[note]`.
- `category` is the **stable rule code** (see `--list-rules`); every finding carries
  one drawn from that catalog, so a script can always map a finding to a known rule.
- `(line N)` is the offending field's line in the `.bib`.
- a fixable finding carries the advisory edit inline as `current -> suggested`.
- a message never wraps: any embedded newline is folded, so one finding = one line.

## What it checks

Checks run in layers, syntax first. A syntax error halts an entry's later layers —
comparing a garbled parse against a record only yields false mismatches.

0. **Syntax** — a file BibTeX cannot parse is never reported as healthy.
   Unbalanced braces, a field missing its `=`, an unknown entry type, a duplicate
   field, and a **cited key with no entry** are errors. The parser recovers at the
   next `@entry{`, so one broken entry doesn't hide the rest. `@string` macros and
   `#` concatenation are expanded, so `journal = prb` is checked by its full value.

1. **Static** (offline) — the `rules.py` registry: missing fields, **biblatex field
   validity** from the datamodel, title casing/brace-protection, `and others`,
   arXiv-id consistency, page/dash/numpages sanity, encoding, identifier check
   digits (DOI, arXiv, ISBN, ISSN, ORCID), duplicate keys/DOIs, and file-wide
   consistency. Uncited entries are noted.

2. **Record** (online) — resolve each entry by DOI (Crossref) or arXiv id and flag
   **disagreement**. Each flagged field carries a suggested edit that conforms the
   bib *toward* the record (`year (suggested: '2009' -> '2010')`), unless the record
   itself is clearly broken. **Severity follows render-impact:** a field that changes
   the rendered citation (title, author, year, journal, volume, issue, pages) is a
   **warning**; a stylistic difference (abbreviated given name, casing) is a
   **note**. Name folding (suffixes, particles, collaborations, abbreviated given
   names) and journal-name folding (a curated physics table plus period-insensitive
   ISO-4, so `Phys. Rev. B` matches `Physical Review B`) keep these from misfiring.
   The one identity **error** is when first author *and* title both differ strongly —
   the id likely points at a different paper (a copy-pasted DOI). A DOI that resolves
   at **DataCite** rather than Crossref (a Zenodo/figshare/Dryad **software or
   dataset**) is resolved against DataCite and verified on title/author/year only —
   the article-only locators (volume/pages/journal) it lacks are never flagged as
   missing. Because a paper and its companion dataset can share a title, the object is
   classified by its registered type, not its title: an `@article` whose DOI resolves
   to software/data is flagged (you may have cited the dataset's DOI, not the paper's).

3. **Status** (online) — retractions (OpenAlex / Retraction Watch), linked
   errata/corrections/comments/replies, and preprints with a published version.

4. **Cross-source** (online) — when more than one source resolves an entry
   (Crossref, INSPIRE-HEP, arXiv, Open Library), their records are compared against
   each other. A data difference (year, volume, issue, pages, journal) warns
   (`source_conflict`, naming both sources); stylistic-only differences don't. This
   surfaces stale or corrupted registry metadata a single source can't reveal.

5. **Verification** (online) — each entry gets a status and a **confidence** (0–1, a
   deterministic function of which sources agreed). **VERIFIED** — id resolved, first
   author and title match; confidence runs **1.0** (clean across ≥2 sources),
   **~0.95** (clean single source), **0.85** (recovered by title/author search, no
   id given), **0.75** (a field disagrees), **0.70** (sources disagree, or only arXiv
   confirms). **UNVERIFIED** — could not confirm (no identifier, no record, or a dead
   DOI). **MISMATCH** — resolved but the identity disagrees. An entry with **no
   identifier** triggers a search (Crossref by title + first author, then arXiv by
   title); a strong match verifies it and reports the id to add, preferring a linked
   published DOI over the bare preprint id. An entry that already carries an id is
   resolved against *that* — the search never runs. A post-2005 article with no
   findable identifier is flagged; pre-2005 work is not penalized.

6. **Integrity score** (online) — a roll-up: counts of verified (and caveats),
   unverified, mismatch, DOI coverage over eligible (post-2005) articles, PID
   coverage, and a **0–100 integrity score** — a transparent weighted blend:
   verification 50%, PID 20%, DOI 15%, freedom from defects 15%.

7. **LLM** (optional, `--llm`, needs `--tex`) — a language model rates each cited
   entry's **relevance** (1–5) from the abstract and surrounding sentences and flags
   a clear **wrong paper**; in a grouped citation it can drop a low-relevance
   odd-one-out a further point. All LLM findings are **advisory warnings at most,
   never errors** — relevance ≤3 and wrong-paper are `[WARN]` to investigate, 4–5 is
   a `[llm] context OK N/5` note. Every rated citation always shows exactly one line
   (it cost tokens), hidden by `--skipnotes` like any other note. The provider is
   pluggable (`llm.py`); the only supported backend is **Claude Code** (the `claude`
   CLI, your existing login), defaulting to **Claude Haiku**. **Privacy:** `--llm`
   sends those sentences to the provider, so it is off by default — don't use it on a
   confidential manuscript.

A multi-key `\cite{}` **group not in chronological order** gets an advisory note
(some styles cite the earliest work first); never an error, since grouped-citation
order is a style choice, not a standard.

## Machine-readable report (`--json`)

`--json FILE` writes the report as **NDJSON** (newline-delimited JSON): one
self-contained JSON record per line. Most lines are one bibliography **entry**,
keyed by its citation key and carrying everything about it — the `entry_type` and
source `line` that identify it, its computed `phases`
(see [Checkpointing](#checkpointing-and-phased-resume)), `status`/`confidence` (and
a short `status_detail` for a non-clean status), the `verify` link, `identifiers`,
matched `canonical_record`, the `sources` that resolved it, an `uncited` flag, and
its `issues`. Carrying the header fields (`entry_type`, `line`, `uncited`,
`status_detail`) makes the record self-sufficient: the terminal report is a
pretty-print of these records, so it is reconstructible from the NDJSON alone. Two
reserved records close the file: `"<file>"` (file-level findings — duplicates, brace
balance, dropped cited keys) and `"<summary>"` (the integrity roll-up):

```jsonc
{"key": "amo2009", "entry_type": "article", "line": 96, "uncited": false,
 "phases": {"offline": true, "online": true, "llm": false},
 "status": "VERIFIED", "confidence": 1.0, "status_detail": "",
 "verify": "https://doi.org/10.1038/nphys1364",
 "identifiers": {"doi": "10.1038/nphys1364", "arxiv": null, "isbn": null},
 "sources": ["crossref", "inspire"], "canonical_record": {"title": "...", "year": 2009},
 "issues": []}
{"key": "<file>", "issues": []}
{"key": "<summary>", "veracite_version": "0.1.3",
 "summary": {"checked": 152, "verified": 151, "verified_with_caveat": 8,
 "unverified": 1, "mismatch": 0, "doi_coverage": 0.94, "pid_coverage": 0.97,
 "integrity_score": 97}}
```

Read it line by line (`for line in open(f): json.loads(line)`). The `"<summary>"`
record holds the metrics and the `veracite_version` that produced the report, so a
saved report is traceable to the exact tool revision. Under `--offline` there is no
verification, so the summary carries the offline mode and a null score
(`{"mode": "offline", "integrity_score": null, ...}`) and each entry has
`phases.offline = true` with a null `status` — enough for a later online run to
resume it, never a fabricated score.

This shape is what makes checkpointing cheap and crash-safe: a finished entry is one
appended line, so an interrupted run leaves every prior line intact (see below).

### Using VeraCite as a verification step for an AI assistant

Because VeraCite is **read-only**, it can serve as an independent **verification
gate** in a human-supervised AI editing loop — the checker stays separate from
whatever is doing the writing. **Applying** the suggested edits is left to a
supervised tool, keeping the deterministic checker and the judgement-applying editor
cleanly separated. The NDJSON report is the integration surface, built to be
consumed by a program:

- **Every finding is grounded, not generated** — from a rule or a comparison
  against Crossref/arXiv/INSPIRE/OpenAlex, each with a `verify:` link. The
  `confidence` is a deterministic function of source agreement, **not** a model
  output, so an agent can gate its own edits on it without compounding hallucination.
- **Findings route by `group`, not by learning every category.** Each issue carries
  `syntax` (mechanical fixes), `semantic` (reconcile against the source of record), or
  `context` (needs judgement) — three policies instead of ~25 categories.
- **Fixable findings carry a structured `suggested` patch** —
  `{"field": ..., "from": ..., "to": ...}`, so a tool applies an edit as data, not by
  parsing English. `to` conforms the bib to the canonical record.
- **The catch is the point.** A hallucinated reference surfaces as `UNVERIFIED` with
  no findable identifier; a real DOI on the wrong paper as `id_resolves_wrong_record`
  (`MISMATCH`); a corrupted id fails its offline check digit; a subtly-wrong
  year/venue/author as a `metadata_mismatch` with the value to adopt — exactly the
  failure modes LLM-drafted bibliographies introduce.

Schema stability: the entry-record fields and each issue's
`severity`/`group`/`category`/`suggested` shape are the supported contract;
`--list-rules json` enumerates the category vocabulary, and `veracite_version` pins
the producing revision so a consumer can detect a contract change.

## Checkpointing and phased resume

An online run on a large bibliography is slow (a few paced network calls per
entry), so a crash shouldn't throw the work away. With `--json report.ndjson`,
VeraCite **appends each entry's record as it finishes** — an O(1) write, so
checkpointing stays cheap even at 10k references and a crash loses at most the entry
in flight. Point it at an existing report and it resumes:

```bash
python -m veracite --bib refs.bib --offline --json report.ndjson   # phase 1: fast, no network
python -m veracite --bib refs.bib          --json report.ndjson   # phase 2: resume, resolve online
python -m veracite --bib refs.bib --tex p/ --json report.ndjson --llm  # phase 3: add LLM ratings
```

On an existing report it loads the saved work and runs each entry **only for the
checks it lacks**, so a job can be built up in phases or restarted after an
interruption. A re-run appends a fresh record per entry (**last line for a key
wins**); a clean run **compacts** the file once at the end — rewritten atomically,
one line per key in bibliography order — and a partial line from a crash is skipped
on load. It prints a NOTE that it is resuming; **choose a different filename to run
from scratch.** Per entry: **offline** always re-runs (cheap); **online** runs only
for entries not yet resolved; **`--llm`** rates only entries not yet rated (and,
since the abstract isn't persisted, re-runs their online layer). An entry already
done at a layer is reused, spending no network or tokens.

VeraCite also **warns up front** when a run looks expensive — 200+ entries online
without `--json` recommends adding it, and `--llm` prints how many entries it will
rate. Both are warnings only; the run proceeds, so scripts and CI are unaffected.

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

If VeraCite is useful in your work, please cite it. The repository ships a
[`CITATION.cff`](CITATION.cff), so GitHub's **"Cite this repository"** button and most
reference managers can import the metadata directly.

A biblatex entry (the title is brace-protected so a style that lowercases titles keeps
`VeraCite`/`BibTeX` cased correctly). The `doi` is the Zenodo **concept DOI**, which
always resolves to the latest version:

```bibtex
@software{whitlock_veracite,
  author       = {Whitlock, Shannon},
  title        = {{VeraCite}: a deterministic verifier for {BibTeX}/{biblatex} bibliographies},
  year         = {2026},
  version      = {0.1.3},
  doi          = {10.5281/zenodo.20963060},
  url          = {https://github.com/Shannon-Whitlock/VeraCite},
}
```

Plain text: Shannon Whitlock. *VeraCite: a deterministic verifier for
BibTeX/biblatex bibliographies*, version 0.1.3, 2026.
https://doi.org/10.5281/zenodo.20963060

> The concept DOI `10.5281/zenodo.20963060` always points to the latest release. To
> cite one exact version, use its version-specific DOI from the
> [Zenodo record](https://doi.org/10.5281/zenodo.20963060) (e.g. v0.1.2 is
> `10.5281/zenodo.20963061`).

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
  `api.datacite.org` (software/dataset DOIs — Zenodo, figshare, Dryad),
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
pip install -e ".[test]"
python -m pytest
```

## Contributing

Contributions are welcome — especially **false positives** (a clean entry that got
flagged) and **false negatives** (a real defect that slipped through), which feed
VeraCite's self-improving loop. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup,
workflow, and how to add a rule; the design principles every change must uphold are
in [`CLAUDE.md`](CLAUDE.md).
