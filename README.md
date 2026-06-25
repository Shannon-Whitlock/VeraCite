# VeraCite

**A lightweight, auditable tool for checking the accuracy and conformity of
BibTeX/biblatex bibliographies in scientific articles — a deterministic check
against hallucinated and mangled citations.**

VeraCite improves the **veracity** of the bibliographic record in scientific
papers. Where BibTeX is notoriously tolerant of imperfect entries, VeraCite
surfaces errors for fast human verification and AI-tool integration, helping
bibliographic records better satisfy the
[FAIR](https://www.go-fair.org/fair-principles/) principles (persistent
identifiers, shared standards, accurate metadata). Because every check is a rule
or a comparison against an authoritative record — **never** a language model
guessing — it is exactly the kind of ground-truth gate an AI writing assistant
needs: it confirms, against Crossref/arXiv and friends, that a reference is real,
correctly identified, and accurately transcribed, catching the fabricated DOI,
the invented paper, and the subtly wrong year or author that LLMs introduce.

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
them without complaint — and checking each entry against the real record is slow and error prone. The same errors, plus outright **fabricated references**, now
arrive in bulk from LLM-assisted drafting, where a confident-looking citation
may name a paper that does not exist or attach a real DOI to the wrong work.
VeraCite does that checking for you — deterministically, against the source of
record — and is built to be:

- **Simple to run** — one small Python program you run from the command line. No
  account, no website, no setup; it works out of the box and needs no extra
  software installed.
- **Trustworthy** — it doesn't guess. Every issue it reports comes from an
  explicit rule or a comparison against an authoritative record, so you can see
  exactly why each was flagged — which is also what makes it a sound check *on* an
  AI assistant's output rather than another source of guesses. The optional AI
  relevance check is **off by default**.
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
   flag **disagreement** with the record. The authoritative record is the
   **canonical reference**: VeraCite never rewrites your `.bib`, but each flagged
   field carries a **suggested edit that conforms the bib to the record** (e.g.
   `year (suggested: '2009' -> '2010')`), so the fix direction is always toward the
   registry — unless the record itself is clearly broken. **Severity follows
   render-impact:** a field that changes the rendered citation (title, author,
   year, journal, volume, issue, pages) is a **warning**; a purely stylistic
   difference (an abbreviated given name, casing) is a **note**. None is a
   wrong-paper claim — name folding handles suffixes (`Jr`/`III`), particles,
   collaborations, and abbreviated given names so these don't misfire, and findings
   show the original, readable names. A journal name matches the record when it is a
   known abbreviation (a small curated physics table in `veracite/data/`) or a valid
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
   sources), **~0.95** (clean single source), **0.85** (recovered by a title/author
   search because the entry carried no identifier — verified, but a weaker basis than
   an id that resolved), **0.75** (a field disagrees), **0.70**
   (sources disagree, or only arXiv confirms). **UNVERIFIED** — could not confirm:
   no identifier, no record returned, or a DOI that did not resolve (also an error).
   **MISMATCH** — it resolved but the record's identity disagrees (the id may point
   at a different paper). If an entry carries **no identifier at all** (no DOI and no
   arXiv id), VeraCite **searches** for one — first Crossref (title + first author,
   corroborated by journal or ±1-year), then, failing that, **arXiv by title**
   (title + first-author surname; common for ML/physics works cited by venue only).
   On a strong match it verifies the entry and reports the identifier to add; when an
   arXiv hit links a **published DOI** (its `<arxiv:doi>`), that DOI is preferred and
   suggested instead of the bare preprint id. This search is a last resort: an entry
   that **already** carries a DOI or arXiv id is resolved against *that* and the
   search never runs. A post-2005 article with no findable identifier is flagged;
   pre-2005 work is not penalized; arXiv ids and ISBNs count as PIDs.

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
   wrong-paper flag is an error; relevance ≤3 a warning; **4–5 leaves a `[llm]
   context OK N/5` note**. Because an LLM call costs tokens, every rated citation
   always shows exactly one line in the report (clean pass, weak, wrong paper, or
   rating-unavailable) rather than vanishing silently; the clean-pass note is hidden
   by `--skipnotes` like any other note. Findings are worded as tentative,
   abstract-only opinions to verify, never authoritative judgements. The provider is pluggable (`llm.py`), but **for now the only
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

`--json FILE` writes the report as **NDJSON** (newline-delimited JSON): one
self-contained JSON record per line. Most lines are one bibliography **entry**,
keyed by its citation key and carrying everything about it — which `phases` have
been computed (see [Checkpointing](#checkpointing-and-phased-resume)), its
verification `status`/`confidence`, the `verify` link, its `identifiers`, the
matched `canonical_record`, the `sources` that resolved it, and its `issues` (that
entry's findings). Two reserved records close the file: `"<file>"` (file-level
findings — duplicates, brace balance, dropped cited keys) and `"<summary>"` (the
integrity roll-up):

```jsonc
{"key": "amo2009", "phases": {"offline": true, "online": true, "llm": false},
 "status": "VERIFIED", "confidence": 1.0, "verify": "https://doi.org/10.1038/nphys1364",
 "identifiers": {"doi": "10.1038/nphys1364", "arxiv": null, "isbn": null},
 "sources": ["crossref", "inspire"], "canonical_record": {"title": "...", "year": 2009},
 "issues": []}
{"key": "<file>", "issues": []}
{"key": "<summary>", "veracite_version": "0.1.1",
 "summary": {"checked": 152, "verified": 151, "verified_with_caveat": 8,
 "unverified": 1, "mismatch": 0, "doi_coverage": 0.94, "pid_coverage": 0.97,
 "integrity_score": 97}}
```

Read it line by line (`for line in open(f): json.loads(line)`); the `"<summary>"`
record holds the metrics and the `veracite_version` that produced the report (so a
saved or shared report is traceable to the exact tool revision — the version is also
printed on the terminal `BIBLIOGRAPHY HEALTH` line). Every other non-reserved record
is one reference. Under
`--offline` there is no online verification, so the `"<summary>"` record carries the
offline mode and finding counts with a null score (`{"mode": "offline",
"integrity_score": null, ...}`) and each entry appears with `phases.offline = true`,
the rest `false`, and a null `status`/`canonical_record` — enough for a later online
run to resume it, never a fabricated score.

The NDJSON shape is what makes checkpointing cheap and crash-safe: a finished entry
is one appended line, so an interrupted run leaves every prior line intact and
loadable (see below).

### Using VeraCite as a verification step for an AI assistant

VeraCite is deliberately **read-only**: it never edits your `.bib` or `.tex`. That
is what lets it serve as an independent **verification gate** in a
human-supervised AI editing loop — the checker has to be separate from whatever is
doing the writing, including an LLM. **Applying** the suggested edits is left to a
supervised tool (e.g. an AI assistant the author is driving), so the deterministic
checker and the judgement-applying editor stay cleanly separated. The NDJSON report
*is* the integration surface, designed to be consumed by a program, not just read:

- **Every finding is grounded, not generated.** A `metadata_mismatch`,
  `dead_doi`, `id_resolves_wrong_record`, or an `UNVERIFIED` status comes from a
  rule or a comparison against Crossref/arXiv/INSPIRE/OpenAlex, each with a
  `verify:` link the agent can check independently. The `confidence` is a
  deterministic function of which sources agreed — **not** a model output — so an
  agent can trust it to gate its own edits without compounding hallucination.
- **Findings route by `group`, not by learning every category.** Each issue
  carries a `group` of `syntax` / `semantic` / `context`: `syntax` is the
  written form (safe, mechanical fixes); `semantic` is metadata that should be
  reconciled against the source of record before editing; `context` needs
  judgement. An agent can hold three policies instead of ~25 categories.
- **Fixable findings carry a structured `suggested` patch** —
  `{"field": ..., "from": ..., "to": ...}`, separated from the prose `message` —
  so a tool can apply an edit as data rather than parsing English. The record is
  the canonical reference, so `to` is the value that conforms the bib to it.
- **The catch is the point.** A hallucinated reference surfaces as `UNVERIFIED`
  with no findable identifier; a real DOI on the wrong paper as
  `id_resolves_wrong_record` (status `MISMATCH`); a corrupted DOI/ISBN/arXiv id
  fails its offline check digit; a subtly-wrong year/venue/author as a
  `metadata_mismatch` with the registry value to adopt. These are exactly the
  failure modes LLM-drafted bibliographies introduce.

Schema stability: the entry-record fields (`status`, `confidence`, `phases`,
`identifiers`, `canonical_record`, `sources`, `issues`) and each issue's
`severity` / `group` / `category` / `suggested` shape are the supported contract;
`--list-rules json` enumerates the full category vocabulary, and the
`veracite_version` on the `"<summary>"` record pins the producing revision so a
consumer can detect a contract change.

## Checkpointing and phased resume

For a large bibliography an online run can take a long time (a few paced network
calls per entry), so a crash partway through should not throw the work away. When
you pass `--json report.ndjson`, VeraCite **appends each entry's record as it
finishes** — an O(1) write, so checkpointing after every entry stays cheap even at
10k references and a crash loses at most the entry in flight. It can then **resume**
from that file:

```bash
python -m veracite --bib refs.bib --offline --json report.ndjson   # phase 1: fast, no network
python -m veracite --bib refs.bib          --json report.ndjson   # phase 2: resume, resolve online
python -m veracite --bib refs.bib --tex p/ --json report.ndjson --llm  # phase 3: add LLM ratings
```

Point VeraCite at an **existing** report and it loads it, replays the work already
saved, and runs each entry **only for the checks it does not yet have** — so a job
can be built up in phases or simply restarted after an interruption. A re-run
appends a fresh record per entry (the **last line for a key wins** on load); at the
end of a clean run the file is **compacted** once — rewritten atomically with one
line per key in bibliography order. A partial line from a crash mid-write is simply
skipped on load. It prints a NOTE that it is resuming; **choose a different `--json`
filename to run from scratch.** The update rule per entry:

- **offline** (the static/syntax checks) always re-runs — it is cheap and needs no
  network.
- the **online** layer runs only for entries not already resolved online; an
  already-resolved entry is reused (its record, status and findings), no network.
- **`--llm`** rates only entries not already rated. Because the rating needs the
  work's abstract — an LLM input that is not persisted — rating an entry also
  re-runs its online layer; an entry already rated is reused, spending no tokens.

VeraCite also **warns up front** when a run looks expensive: a bibliography of 200+
entries run online without `--json` prints a recommendation to add it (so the run
is saved and resumable), and `--llm` prints how many entries it will rate (it uses
LLM tokens). Both are warnings only — the run proceeds, so scripts and CI are
unaffected.

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
  override them. Pacing is **per service and time-based**: each external service
  has a minimum interval (`request_delay`, default 0.2 s; arXiv is paced at 3 s) and
  a request waits only the *remainder* of that interval — time already spent on
  other services or the rest of the pipeline counts, and a service whose interval
  has elapsed proceeds immediately. So an entry resolved by Crossref never pays an
  arXiv delay, and arXiv's slow limit spaces out across many entries rather than
  blocking each one. Only a real outbound request ever waits.
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
