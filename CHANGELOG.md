# Changelog

All notable changes to VeraCite are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VeraCite adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] — 2026-06-30

Patch release: false-positive fixes and journal-name canonicalization, driven by a
stress-test corpus of real physics/atomic-physics bibliographies.

### Added

- **Journal canonicalization.** Each journal carries a curated set of accepted forms
  (its abbreviation and full title) in `journal_abbrev.json`. A bib journal that
  matches any accepted form passes silently; otherwise VeraCite suggests conforming
  toward the closest accepted form. Severity follows the deviation:
  - a pure-case slip (`nat. phys.`) or a run-together abbreviation
    (`Phys.Rev.Lett.`, the INSPIRE no-space house style) is a quiet, suppressible
    **`journal_style`** note;
  - a content/punctuation difference (`Phys. Rept.` → `Phys. Rep.`,
    `Proc. Natl. Acad. Sci. U.S.A.` → `… USA`) is a **`metadata_mismatch`** warning.
- **`wrong_journal`** — when the bib's journal is a *known but different* journal than
  the DOI-resolved record (`J. Phys. A` cited with a `J. Phys. B` DOI; `Nature
  Photonics` with a `Nature` DOI), a warning flags a possible wrong DOI / mis-cited
  venue and suggests the record's journal.
- **More given-name precision.** A spelled-out given name disagreeing with the record
  now carries a suggested fix when the record is a confident correction target
  (`Jun` → `Jun-Ru`, `Minore` → `Minori`); a genuinely divergent name stays
  "check manually".

### Fixed

- **Genuine two-letter ISO-4 stems accepted.** `J. Phys. B: At. Mol. Opt. Phys.` (the
  exact ISO-4 abbreviation) is no longer a false `journal differs`; the abbreviation
  table's blunt two-letter floor is now allowlisted from the table's own stems
  (`At.`, `Ed.`, `Am.`, …), while a bogus `Ph.` for `physics` stays rejected.
- **INSPIRE `Rept.` / no-space forms** (`Rept.Prog.Phys.`, `Phys.Rept.`) are
  recognized, so cross-source comparison no longer reports a spurious
  `source_conflict`; a bib using the no-space form is nudged to the spaced canonical.
- **Book author lists.** Crossref registers book/monograph authors incompletely; a
  correct extra bib author (e.g. *Rydberg Physics* by Šibalić **and** Adams resolving
  to a record naming only Šibalić) is no longer flagged as a spurious author — the
  finding now points at the record's incompleteness and asks the user to verify.
- **No brace protection on journal names.** Standard BibTeX/biblatex styles print the
  journal field verbatim (only the title field is recased), so journal names are
  stored and suggested without brace protection.

### Fixed

A second audit pass over real-world stress-test bibliographies drove a further round
of false-positive fixes, each generalized to its class with tests.

- **Entry-type self-loop** — an `@inproceedings` whose record is a `proceedings`/
  `book-chapter` is already correct and no longer told to change into itself.
- **No ERROR on a clean software citation** — order-insensitive authors, release-title
  folding (`org/repo: Name X.Y.Z`), and brace-grouped surnames (`{Carrera Vazquez}`).
- **Name markup folded** — umlaut transliterations (`ä` ≡ `ae` ≡ `a`) and the TeX tie
  `~` no longer read as a different/glued name.
- **Manifestation-aware** — a preprint/working-paper or book-reprint DOI no longer
  overrides the cited published version (new **`divergent_manifestation`** note); a
  book *series* name is never suggested as a journal.
- **Dash handling** — a `--`/`-`-only difference is not flagged, alphanumeric ranges
  keep `--`, and a malformed `34–-38` fixes to `34--38` (never `---`).
- **Date policy** — a published article uses its publication date; an arXiv citation
  uses the v1 submission year unless it pins a version.
- **Lighter-touch titles** — a cosmetic high-overlap title difference is "check
  manually", not a rewrite; TeX `\Delta` folds to `Δ`.
- **Integrity score** — a structurally unparseable file is capped below the healthy
  band instead of reading `100/100`.
- **Crash fixes** — a non-UTF-8 `.tex` reads with an encoding fallback; an undefined
  score renders `--` instead of crashing.

## [0.2.0] — 2026-06-29

First public release. VeraCite audits BibTeX/biblatex bibliographies for
hallucinated, mangled, or mis-identified citations, comparing each entry against
authoritative online records (Crossref by DOI primary; arXiv, INSPIRE-HEP,
OpenAlex, DataCite, Open Library corroborating). It flags, never edits.

### Highlights

- **Three-layer audit** — syntax → semantics → context. Static (offline) checks
  for biblatex datamodel validity, title casing/brace-protection, identifier
  check digits (DOI/arXiv/ISBN/ISSN/ORCID), and duplicates; online record
  resolution and cross-source comparison; optional `--tex` citation-context and
  `--llm` relevance sweep.
- **Trust-first by design** — every finding is a deterministic rule or a
  comparison against a registry record (`--llm` the sole exception). A suggested
  edit always conforms the bib *toward* the matched record, and is withheld on a
  weak or ambiguous match.
- **Two deterministic 0–100 scores** — an *integrity* score (is the bibliography
  sound?) and a *confidence* score (how well were entries verified?), both
  transparent formulas, not model outputs.

### Added

- **Per-record NDJSON as the single source of truth** (`--json FILE`): one
  self-contained record per entry. The terminal report and `--json` are both a
  render of the same records through one builder and one renderer.
- **Checkpointing and phased resume** — the NDJSON is rewritten atomically after
  each changed entry, so a crash loses at most the entry in flight. Re-running
  resumes: an entry is rechecked only if it is missing a layer or its `.bib` text
  changed (tracked by a source `checksum`). A phase is marked done only when it
  actually succeeded.
- **Forward-compatible reports** — a report written by a future version is read
  with `.get`, and unknown fields/records are preserved verbatim rather than
  dropped. Resume tolerates an unknown future suppression category.
- **Cross-finding suppression (`SUPERSEDES`)** — when one fix resolves several
  findings, the dependents are suppressed (e.g. the online "adopt the record's
  casing" supersedes the offline "looks miscased" guess). A suppressed finding is
  still persisted in the JSON, stamped with what retracted it, and revealed in the
  terminal with `--show-suppressed`.
- **`--list-rules`** — the full audit catalog (severity, group, what supersedes
  what, source rules, description), introspected from the code so it cannot drift;
  `--list-rules json` for the machine-readable form and `--list-rules suppression`
  for the suppression table.
- **Finer finding `type`** — each issue carries a sub-category within its
  `category` (e.g. `metadata_mismatch.title_overlap_strong`), enabling per-`type`
  severity overrides in the settings file.
- **Transient-vs-settled error handling** — a rate-limit/5xx/network failure is
  marked retryable and re-run on resume rather than mislabeled as an unverified
  citation; only a settled 404 ("no such record") is treated as final.
- **Given-name refinement suggestions** — a spelled-out given name disagreeing
  with the resolved record now carries a suggested fix when the record is a
  confident correction target (a prefix the bib truncated, e.g. `Jun` →
  `Jun-Ru`, or a single-character typo, e.g. `Minore` → `Minori`); a genuinely
  divergent name stays "check manually" with no suggestion.

### Known limitations

- A `url` field is **not** fetched or validated — resolving an arbitrary URL from
  an untrusted `.bib` is an SSRF risk, so link rot is not detected.
- VeraCite compares against registry **metadata**; errors in free text or in
  fields no registry encodes are out of reach. Correction/erratum and
  published-version coverage is best-effort.

[0.2.0]: https://github.com/Shannon-Whitlock/VeraCite/releases/tag/v0.2.0
