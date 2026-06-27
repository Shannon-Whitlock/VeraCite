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

A **VeraCite-side failure must never masquerade as a citation problem.** When a
lookup fails on a TRANSIENT error (an HTTP 429 rate-limit, a 5xx, or a network drop),
that is the tool's hiccup, not a defect in the entry — say so in the finding ("source
was rate-limited or unreachable; re-run to retry"), mark the result retryable
(`Resolution.online_error`), and have a resumed run RE-RUN that entry's online phase
(`Checkpoint.needs`) instead of replaying the failure. Distinguish it from a settled
404 ("no such record"), which is NOT retried. The HTTP layer therefore returns the
status code (`http_get_text`, like `http_get_json`) so the cause is visible, not
collapsed into a bare `None`. A whole tail of entries silently turning UNVERIFIED
because of one rate-limit is a trust failure as real as a false positive.

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
- **The per-record NDJSON is the single source of truth — every output is a parse of
  it, never a recompute from live objects.** Each record holds *all* its own data,
  including every input to any roll-up (e.g. `dead_doi`/`found_by_search`, which
  `_confidence_kind` needs — persist them or the score drifts on reprint). No
  aggregate is stored: the integrity/confidence summary is **re-derived by parsing the
  records** each run, so fresh and resumed runs take the identical path and a saved
  report is byte-stable. When `--json` points at a file, a record already complete for
  the requested phases is **pretty-printed unchanged** (no re-resolve, no network
  setup); a record *missing* a requested phase (online failed, `--llm` not yet run, an
  uncited entry later run without `--tex`) has **only the missing phase run and the
  record updated** — keeping its existing notes. A phase is marked done **only when it
  actually succeeded** — a transient API failure (`online_error`) or a failed LLM call
  (`llm_error`) leaves the phase undone so a later pass retries it; never record a
  failed attempt as complete. Each record stamps a `checksum` of its source text: on
  resume an entry whose `.bib` text changed (or whose record predates checksums) is
  recomputed from scratch, so editing the bib re-verifies exactly the changed entries
  without naming them by `--key`.
- **Keep the NDJSON forward-compatible.** The loader must tolerate a report a *future*
  version wrote: read records with `.get` (never reject a record for an unknown field),
  treat any angle-bracketed `<…>` key as a reserved non-entry record (skip kinds you
  don't know rather than load them as entries), and preserve unknown fields/records
  verbatim through compaction so an older tool never silently strips a newer tool's
  data. Each record carries the `veracite_version` that wrote it. The ratchet is
  `test_ndjson_is_forward_compatible`.

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
  over free-text heuristics, which are the likeliest to misfire. *Example:* the
  stray-superscript author check scanned for digits glued to a name (`Cohen1`) — but a
  digit inside **TeX markup is not name content**: `H.{\hspace{0.167em}}L. Sørensen`
  reads the `0.167em` dimension as a "stray year" and would "fix" it to `\hspace{0.em}`,
  corrupting the spacing. Fold the markup out first — strip TeX *spacing* macros
  (`\hspace`/`\kern`/`\,`…) to a plain space (a separate, correct finding) while
  leaving accents (`\o`, `\"{u}`) untouched — then scan the cleaned name.
- Check every proposed rule change against these principles before writing it —
  even when it comes from the user.
- Fold away legitimate variation before comparing (name particles/suffixes, ISO-4
  journal abbreviations, brace/quote wrapping, `--` vs `-`, **an arXiv title the
  author cited from an earlier version that a later revision renamed**) — each
  unfolded thing is a false positive waiting. *Example:* arXiv's single-id query
  serves only the **latest** version's title, so a faithfully-cited v1 title looks
  like a mismatch; probe the versioned ids (`<id>v1`…) and, on a match, emit the
  `preprint_retitled` note **without** a `suggested` overwrite rather than a
  `metadata_mismatch` (the bib is correct — pushing the new title would corrupt it).
  Corollary: never conclude "this title exists nowhere" from a title *search* index
  alone — the index, too, only holds the latest title.
- Attach `suggested=` only when certain of the target; otherwise report the
  discrepancy without one. A *resolved* but DIVERGENT record is also "uncertain":
  when arXiv links a published DOI whose title or first author disagrees with the
  bib, the link may be wrong (or the paper was retitled at publication) — soften the
  claim ("a published version MAY exist — verify it is the same work") rather than
  asserting it. Strip registry markup (MathML) before that comparison so formatting
  noise alone never reads as a different paper.
- A DOI mined from a url is canonical — but one publisher (**Nature**) puts the DOI
  *suffix* in the path without the `10.<registrant>/` prefix
  (`nature.com/articles/s41586-…` ⇒ `10.1038/s41586-…`). The prefix is unrecoverable
  from the url or by search (a bare suffix is not a resolvable DOI), so the host
  supplies it. This is a single high-precision host rule, not a publisher table —
  Science/APS/IOP/Springer already embed the literal DOI; reconstruct only where the
  prefix is genuinely absent, pin the suffix shape tight, and let resolution validate.
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
- A note that fires on (nearly) **every** entry is noise, not signal — it can't
  discriminate and it buries the useful findings. If a nudge lights up the whole bib,
  the trigger is too broad: scope it to the entries the standard actually targets.
  *Example:* `urldate` is recommended for sources that can change/vanish (`@online`/
  grey-web items), **not** for every url-bearing entry — in many bibs the DOI/arXiv id
  lives *inside* the url, so a published @article landing page (an arXiv/DOI page)
  carries a url yet is a stable source of record an access date adds nothing to. Fire
  only on online-typed entries or a genuine web/press/grey url with no mineable id.

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

## Known gaps / deferred (with constraints)

- **`url` is not validated.** A dead or wrong link in a `@misc`/`@online`/`@software`
  entry passes silently. Checking it means VeraCite would **fetch a URL from an
  untrusted `.bib`**, which is an SSRF risk.

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
