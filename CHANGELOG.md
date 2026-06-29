# Changelog

All notable changes to VeraCite are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VeraCite adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
