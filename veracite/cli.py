"""Command-line interface: path discovery, argument parsing, and the run loop
that drives the syntax, static, record/status and (optional) LLM layers.
"""

import argparse
import os
import sys

from .checkpoint import (Checkpoint, compact,
                         entry_record, read_records, requested_phases,
                         write_records)
from .config import HTTP_BACKEND, SETTINGS, VERSION, load_settings
from .llm import (LLM_PROVIDERS, collect_tex, find_citation_contexts,
                  find_citation_groups, preflight_provider, resolve_provider)
from .parser import parse_bib
from .pipeline import analyze_entry
from .report import enable_ansi_colors, Report, Severity
from .rules import run_entry_rules, run_file_rules, syntax_pass
from .verify import chronological_order, integrity

# A bibliography at or above this many entries is "large": an online run over it
# can take a long time, so we recommend --json (incremental save + resume).
LARGE_BIB = 200

# The --json report is an NDJSON file (see checkpoint.py): one record per bib entry,
# in bib order.  After each processed entry the whole file is rewritten atomically
# (temp + os.replace), so an interrupt at any point leaves a fully valid, duplicate-
# free file.  Unchanged entries are skipped (not rewritten), so a fully-cached run
# never touches the file.


DESCRIPTION = """\
VeraCite -- a deterministic auditor for BibTeX/biblatex bibliographies.

Point it at a .bib file. Checks run in layers: syntax (structural validity),
static (offline rules), record/status (resolve each entry against
Crossref/arXiv/OpenAlex and flag disagreements, retractions, errata, and
superseded preprints), and an optional LLM relevance sweep (--llm).

Add --tex PATH to also check citations: only entries cited by those sources are
resolved online and (with --llm) rated, and uncited entries are flagged. Without
--tex no .tex is read at all and every entry is checked. Exit status is non-zero
when any error is found (CI friendly).
"""

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def _walk(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            yield dirpath, names


def discover_bib(roots):
    """Find a .bib under the search roots, preferring the shallowest path. The
    extension match is case-insensitive (Windows/macOS filesystems happily hold a
    'References.BIB')."""
    found = [os.path.join(dp, n) for dp, names in _walk(roots)
             for n in names if n.lower().endswith(".bib")]
    found.sort(key=lambda p: (p.count(os.sep), p))
    return found[0] if found else None


def read_bib_text(bib_path, ap):
    """Read a .bib as text, tolerating the common non-UTF-8 case. A .bib saved in
    Latin-1 (still common in older European TeX setups) or a binary file fed by
    mistake would otherwise crash with a raw UnicodeDecodeError traceback. Try
    UTF-8, fall back to Latin-1 (which never raises) for a real text file, and only
    if that still looks like binary do we error cleanly instead of dumping a stack."""
    data = open(bib_path, "rb").read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # A NUL byte means it is not text at all (a .pdf, an object file, ...).
    if b"\x00" in data:
        ap.error(f"{bib_path} is not a text file (looks binary) -- is it really a "
                 ".bib? If it is a PDF or compiled output, point --bib at the .bib "
                 "source instead")
    print(f"warning: {bib_path} is not valid UTF-8; reading it as Latin-1 -- "
          "re-save it as UTF-8 to silence this", file=sys.stderr)
    return data.decode("latin-1")


def parse_args(argv):
    """Define and parse the command-line interface. Returns (parser, args)."""
    # prog="veracite" so usage/error lines read "veracite: error: ..." rather than
    # argparse's default "__main__.py" -- matching the documented `veracite` /
    # `python -m veracite` invocations and the official package name.
    ap = argparse.ArgumentParser(
        prog="veracite",
        description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"veracite {VERSION}")
    ap.add_argument("--bib", metavar="FILE", help="path to the .bib file (else auto-discovered)")
    ap.add_argument("--tex", metavar="PATH", action="append", default=[],
                    help="a .tex file or directory (recursive); repeatable. Given "
                         "this, only cited entries are checked online and uncited "
                         "ones are skipped. Omit it to check every entry and read "
                         "no .tex at all (the manuscript stays private).")
    ap.add_argument("--offline", action="store_true",
                    help="skip all network lookups (static checks only)")
    ap.add_argument("--llm", action="store_true",
                    help="also run the optional LLM relevance sweep (requires --tex; "
                         "sends the cited sentence(s) to the provider; off by default)")
    ap.add_argument("--llm-provider", metavar="NAME",
                    help=f"LLM backend for --llm (default from settings; "
                         f"known: {', '.join(sorted(LLM_PROVIDERS))})")
    ap.add_argument("--key", help="focus on ONE citation key: only it is resolved "
                    "online and only its findings are printed (the offline checks "
                    "still run for every entry so file-level rules stay correct)")
    ap.add_argument("--settings", metavar="FILE", help="path to a settings JSON file")
    ap.add_argument("--json", metavar="FILE", help="also write a JSON report")
    ap.add_argument("--delay", type=float, default=None,
                    help="seconds between API calls (default from settings)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="HTTP timeout in seconds (default from settings)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI color in the report")
    ap.add_argument("--skipnotes", action="store_true",
                    help="hide note-level findings (stylistic / biblatex-filtered); "
                         "show only warnings and errors")
    ap.add_argument("--sort", choices=["entry", "severity"], default="entry",
                    help="report order: 'entry' (default) groups findings per .bib "
                         "entry in file order; 'severity' is one global list, errors "
                         "first, for triage")
    ap.add_argument("--list-rules", nargs="?", const="table", choices=["table", "json"],
                    metavar="FORMAT",
                    help="print the catalog of finding categories (the audit sheet: "
                         "each category's default severity, group, and description) "
                         "and exit. FORMAT is 'table' (default) or 'json'")
    return ap, ap.parse_args(argv)


def main(argv=None):
    """Entry point: parse args, run the requested layers, render the report.
    Returns a process exit code (non-zero if any error-level finding)."""
    ap, args = parse_args(argv)
    load_settings(args.settings)

    # --list-rules is pure introspection: print the audit catalog and exit before
    # touching any .bib or the network. A publisher reviews this against their
    # house standard, then encodes disagreements in a settings 'severity' block.
    if args.list_rules:
        from .catalog import print_catalog
        print_catalog(as_json=(args.list_rules == "json"))
        return 0

    # CLI flags override settings; settings supply the defaults. Pacing is applied
    # per service in the HTTP layer (http._throttle), which reads request_delay from
    # SETTINGS -- so a --delay override is written back there to take effect. `delay`
    # is still passed down for signature/back-compat but the sleeps live in http.py.
    delay = args.delay if args.delay is not None else SETTINGS.get("request_delay", 0.2)
    if args.delay is not None:
        SETTINGS["request_delay"] = args.delay
    timeout = args.timeout if args.timeout is not None else SETTINGS.get("request_timeout", 20)
    roots = [os.getcwd()]

    bib_path = args.bib or discover_bib(roots)
    if not bib_path:
        ap.error("no .bib file given and none found; pass --bib FILE")
    bib_path = os.path.abspath(os.path.expanduser(bib_path))
    if not os.path.isfile(bib_path):
        ap.error(f"bib file not found: {bib_path}")
    if not args.bib:
        print(f"Using bibliography: {bib_path}", file=sys.stderr)

    # The LLM sweep needs the in-text citation context, which only --tex provides.
    if args.llm and not args.tex:
        ap.error("--llm needs citation context: pass --tex PATH with the .tex "
                 "sources (the LLM rates each citation against its surrounding text)")
    # --llm rates each citation against the record's abstract, which only the online
    # layer fetches; combined with --offline it would silently do nothing. Reject the
    # contradiction up front rather than print 'HEALTHY' with the sweep never run.
    if args.llm and args.offline:
        ap.error("--llm cannot run with --offline: the relevance rating needs the "
                 "online layer (it reads each work's abstract). Drop --offline to use --llm")

    # .tex is read ONLY when --tex is given -- the manuscript is never opened
    # otherwise. This keeps a default run confidential and removes the surprising
    # dependence on whether a .tex happened to be auto-discovered.
    #   * with --tex   -> "citations" mode: only cited entries are resolved online
    #                      and rated; uncited ones are noted and skipped.
    #   * without --tex -> "bibliography-only" mode: every entry is resolved; no
    #                      manuscript is read; no citation/uncited checks.
    tex_mode = bool(args.tex)
    tex_files = collect_tex(args.tex) if tex_mode else []
    tex_base = (os.path.commonpath([p for p, _ in tex_files]) if len(tex_files) > 1
                else os.path.dirname(tex_files[0][0]) if tex_files else os.getcwd())
    contexts = find_citation_contexts(tex_files, tex_base) if tex_files else {}
    cite_groups = find_citation_groups(tex_files) if tex_files else []

    raw = read_bib_text(bib_path, ap)
    entries, problems = parse_bib(raw)

    # A wrong file (a .pdf, .bbl, .txt, or a path that simply holds no @entries)
    # parses to zero entries. Reporting "HEALTHY" on an empty set is a false pass --
    # the worst outcome for a verification tool -- so say so plainly and exit non-zero.
    if not entries:
        hint = ("" if bib_path.lower().endswith(".bib")
                else f" (its extension is not .bib -- is '{os.path.basename(bib_path)}' "
                     "really your bibliography?)")
        ap.error(f"no BibTeX entries found in {bib_path}{hint}")

    # Color when attached to a terminal and not disabled -- and, on Windows, only
    # if the console accepts ANSI (otherwise the escapes would print literally).
    color = not args.no_color and sys.stdout.isatty() and enable_ansi_colors()
    rep = Report(color=color)

    # The file-level syntax findings (parser problems, brace balance, missing '='
    # separators) are produced up front; they print in the file-level group, not
    # under any one entry. `broken` is the set of keys with a structural error --
    # their fields parsed wrong, so online record comparison would only produce
    # spurious mismatches; skip the online layer for them.
    broken = syntax_pass(raw, entries, problems, rep)

    citedset = set(contexts)
    if tex_mode:
        if not tex_files:
            ap.error(f"--tex matched no .tex files: {', '.join(args.tex)}")
        analyze = citedset                       # online/LLM only over cited entries
        nuncited = sum(1 for e in entries if e.key not in citedset)
        print(f"mode: citations (.tex: {tex_base}) -- checking {len(entries) - nuncited} "
              f"cited of {len(entries)} entries ({nuncited} uncited skipped)", file=sys.stderr)
    else:
        analyze = None                           # resolve every entry
        print(f"mode: bibliography-only ({len(entries)} entries; no .tex read -- "
              f"pass --tex to check citations)", file=sys.stderr)

    online = not args.offline

    # Resume: if --json points at an existing VeraCite report, load it and reuse the
    # phases each entry already carries (offline/online/llm). The run then computes,
    # per entry, only the phases the saved report lacks -- so a job can be done in
    # phases (offline, then online, then --llm) or simply restarted after a crash,
    # picking up where it left off. Pick a NEW --json filename to start from scratch.
    requested = requested_phases(online, args.llm)
    checkpoint = Checkpoint.load(args.json) if args.json else None
    if checkpoint:
        print(f"NOTE: VeraCite is resuming from the existing report {args.json!r} "
              f"({len(checkpoint.phases_by_key)} saved entries) -- entries already "
              f"covering the requested checks are reused; only missing checks run. "
              f"Choose a different --json filename to re-run from scratch.",
              file=sys.stderr)

    # Large bibliography: an online run over many entries is slow (a few network
    # calls per entry, paced). Without --json a crash loses everything, so strongly
    # recommend it -- the report then saves incrementally and the run is resumable.
    # Warn only; never block (CI/pipes must proceed unattended).
    if online and len(entries) >= LARGE_BIB and not args.json:
        print(f"warning: {len(entries)} entries with online checks may take a long "
              f"time. Strongly recommend '--json report.json' so results are saved "
              f"incrementally after each entry and the run can resume if it is "
              f"interrupted (re-running with the same --json file continues it). "
              f"Proceeding without incremental save ...", file=sys.stderr)

    provider = model = None
    if online and args.llm:
        provider_name = args.llm_provider or SETTINGS.get("llm_provider", "claude")
        model = SETTINGS.get("llm_models", {}).get(provider_name, "")
        provider = resolve_provider(provider_name, rep)
        if provider is None:
            ap.error(f"unknown --llm provider {provider_name!r}; known: "
                     f"{', '.join(sorted(LLM_PROVIDERS))}")
        # Probe the provider once up front. A fatal setup problem -- most often the
        # user is not logged in to Claude / has no account -- otherwise surfaces as a
        # baffling per-entry warning repeated for every cited reference, after the
        # whole online pass has already run. Fail fast with actionable guidance.
        print(f"Checking the {provider_name!r} LLM provider ...", file=sys.stderr)
        fatal = preflight_provider(provider, model, timeout=args.timeout or 30)
        if fatal:
            hint = ("" if provider_name != "claude" else
                    " -- run 'claude' once and sign in (it needs a logged-in Claude "
                    "account/CLI), or drop --llm to skip the relevance ratings")
            ap.error(f"--llm provider {provider_name!r} is not available: {fatal}{hint}")
        print(f"NOTE: --llm sends the sentence(s) around each \\cite from your .tex "
              f"to the LLM provider ({provider_name!r}). Do not use on a confidential "
              f"manuscript.", file=sys.stderr)
        # --llm makes one model call per cited entry that still needs rating, so a
        # large bibliography spends real LLM tokens/cost. Make that explicit up front
        # (and note that resume only rates entries not already rated, so re-running
        # does not re-spend on completed ones). Only entries that actually EXIST in the
        # bib are rateable -- a cited key with no entry is reported as an error and
        # cannot be rated -- so count the cited keys that have an entry, keeping this
        # consistent with the "N cited of M entries" line above.
        rateable = citedset & {e.key for e in entries}
        n_to_rate = sum(1 for k in rateable
                        if not (checkpoint and checkpoint.has(k, "llm")))
        print(f"NOTE: --llm uses LLM tokens -- one rating call per cited entry "
              f"({n_to_rate} to rate{' more' if checkpoint else ''} of "
              f"{len(rateable)} cited). This costs tokens/credits on the provider; "
              f"with --json, already-rated entries are not re-rated on resume.",
              file=sys.stderr)
    if online:
        print(f"Looking up records, retractions, abstracts (HTTP: {HTTP_BACKEND}) ...",
              file=sys.stderr)

    rep.render_header(online=online, llm_used=args.llm)

    # Single pass, entry by entry in bibtex (file) order: run every layer for an
    # entry (static -> record/status/cross-source -> verification -> LLM), then
    # print that entry's findings once.
    total = len(entries)
    width = len(str(total))
    any_emitted = False
    results, statuses = {}, {}      # key -> Resolution / (status, confidence)
    phases_by_key = {}              # key -> set(phases done this run + reused)
    records = []                    # the per-entry record dicts built this run, in
                                    # bib order: the single source the summary parses
    ordered_keys = [e.key for e in entries]
    by_key = {e.key: e for e in entries}   # for naming a citation's group-mates
    # Working copy of the NDJSON records: starts from whatever is already on disk
    # (so unchanged entries are preserved through the run), updated in-place as each
    # entry is processed, and written atomically after each change.
    working_records, _ = read_records(args.json) if args.json else ({}, 0)
    if working_records is None:
        working_records = {}
    # Advisory: note any \cite{} group whose members are out of chronological order
    # (offline, from the bib years). Done before the loop so notes attach to the
    # group's first entry and emit in bibtex order with everything else.
    if tex_mode:
        chronological_order(cite_groups, by_key, rep)

    for i, e in enumerate(entries, 1):
        # In --tex (citations) mode an UNCITED entry is reduced to a single header
        # line and skipped from ALL further analysis -- no offline rules, no online
        # lookup, no notes. (Its structural soundness is still covered by the
        # file-wide syntax_pass, which ran before this loop, so a brace break that
        # would corrupt parsing of OTHER entries is still caught.) Without --tex
        # every entry is analyzed, so the .bib can be checked/augmented in full.
        uncited = tex_mode and e.key not in citedset
        if uncited:
            rep.mark_uncited(e.key)
            phases_by_key[e.key] = set()
            # Skip if already saved with matching source text (uncited status is stable).
            if (checkpoint and not checkpoint.is_stale(e.key, e.raw)
                    and e.key in checkpoint._records_raw):
                rec = checkpoint._records_raw[e.key]
                records.append(rec)
                if args.sort == "entry":
                    any_emitted |= rep.emit_entry(e, skip_notes=args.skipnotes,
                                                  progress=f"[{i:>{width}}/{total}]")
                continue
            rec = entry_record(
                e.key, None, None, None, set(), [], verify=None,
                entry_type=e.etype, line=e.lineno, uncited=True,
                bib_year=e.get("year"), bib_source=e.raw)
            records.append(rec)
            if args.json:
                working_records[e.key] = rec
                write_records(args.json, working_records, ordered_keys)
            if args.sort == "entry":
                any_emitted |= rep.emit_entry(e, skip_notes=args.skipnotes,
                                              progress=f"[{i:>{width}}/{total}]")
            continue

        # Staleness: if the saved record's source checksum no longer matches this
        # entry (it was edited in the .bib) or is absent (older report), the cache is
        # not trustworthy -- ignore it for this entry so every requested phase is
        # recomputed from scratch. This is what makes "edit the .bib, re-run" re-verify
        # exactly the changed entries without naming them.
        cp = None if (checkpoint and checkpoint.is_stale(e.key, e.raw)) else checkpoint

        # Resume bookkeeping: which phases this entry still needs (only the
        # requested-and-missing ones), and which prior phases to carry forward
        # unchanged.
        if cp:
            to_run = cp.needs(e.key, requested)
            # When the checkpoint is fresh and nothing new is needed, skip the offline
            # rules re-run and the append -- the saved record is already correct.
            if not to_run:
                rep.seed_findings(cp._findings_by_key.get(e.key, []))
                if e.key in cp.results:
                    results[e.key] = cp.results[e.key]
                    st, conf = cp.statuses.get(e.key, ("", 0.0))
                    statuses[e.key] = (st, conf)
                    if st:
                        rep.set_status(e.key, st, conf, cp.details.get(e.key, ""))
                    if cp.links.get(e.key):
                        rep.set_link(e.key, cp.links[e.key])
                phases_by_key[e.key] = cp.phases_by_key.get(e.key, set())
                rec = cp._records_raw[e.key]
                records.append(rec)
                if args.sort == "entry" and (not args.key or e.key == args.key):
                    any_emitted |= rep.emit_entry(e, skip_notes=args.skipnotes,
                                                  progress=f"[{i:>{width}}/{total}]")
                continue
            to_run.discard("offline")
        else:
            to_run = set(requested) - {"offline"}

        run_entry_rules(e, rep)
        phases_by_key[e.key] = {"offline"}
        analyzed = online and (analyze is None or e.key in citedset)
        # A structurally broken entry parsed wrong; comparing its garbled fields
        # against a record yields false mismatches, so skip the rest of this
        # entry's checks (record/status/cross-source/LLM) and point at the syntax
        # error as the fix.
        if analyzed and e.key in broken:
            rep.add(Severity.WARN, e, "has a structural syntax error (see above); "
                    "the rest of this entry's checks (record, status, cross-source, "
                    "LLM) are skipped until it parses cleanly", "syntax",
                    category="syntax")
            analyzed = False

        if cp:
            reuse = (cp.phases_by_key.get(e.key, set()) - to_run) \
                & {"online", "llm"}
            if reuse:
                # Replay the prior findings for the reused phases, and reuse the saved
                # resolution/status so the score and JSON are complete without
                # re-resolving.
                rep.seed_findings(cp.seed_findings_for(e.key, reuse))
                if e.key in cp.results:
                    results[e.key] = cp.results[e.key]
                    st, conf = cp.statuses.get(e.key, ("", 0.0))
                    statuses[e.key] = (st, conf)
                    if st:
                        rep.set_status(e.key, st, conf, cp.details.get(e.key, ""))
                    if cp.links.get(e.key):
                        rep.set_link(e.key, cp.links[e.key])
                phases_by_key[e.key] |= reuse

        if analyzed and (not args.key or e.key == args.key) and "online" in to_run:
            if sys.stderr.isatty():
                print(f"  [{i:>{width}}/{total}] {e.key}\r", end="",
                      file=sys.stderr, flush=True)
            run_llm = "llm" in to_run
            res = analyze_entry(e, results, statuses, rep, delay=delay, timeout=timeout,
                                provider=(provider if run_llm else None), model=model,
                                contexts=contexts, by_key=by_key)
            # Mark a phase complete only if it actually SUCCEEDED.
            if not res.online_error:
                phases_by_key[e.key].add("online")
            if run_llm and not res.llm_error:
                phases_by_key[e.key].add("llm")

        # Build this entry's canonical record -- the single source the summary parses
        # and (with --json) the line written to the log. Built for EVERY entry, even
        # without --json, so the in-memory summary derives from the same records.
        res = results.get(e.key)
        st, conf = statuses.get(e.key, (None, None))
        rec = entry_record(
            e.key, res, st, conf, phases_by_key[e.key],
            rep.issues_for(e.key), verify=rep.links.get(e.key),
            entry_type=e.etype, line=e.lineno,
            status_detail=rep.status_detail(e.key), bib_year=e.get("year"),
            bib_source=e.raw)
        records.append(rec)
        if args.json:
            working_records[e.key] = rec
            write_records(args.json, working_records, ordered_keys)
        if args.sort == "entry" and (not args.key or e.key == args.key):
            any_emitted |= rep.emit_entry(e, skip_notes=args.skipnotes,
                                          progress=f"[{i:>{width}}/{total}]")

    # File-level findings: cited keys with no entry (dropped references), then any
    # remaining file-wide findings (duplicates, consistency, brace balance).
    if tex_mode:
        parsed_keys = {e.key for e in entries}
        for key in sorted(citedset - parsed_keys):
            rep.add_file(Severity.ERROR, f"cited key '{key}' has no entry in the .bib "
                         "(entry missing or failed to parse)", "syntax", category="syntax")
    run_file_rules(entries, rep)
    if args.sort == "severity":
        any_emitted |= rep.emit_by_severity(skip_notes=args.skipnotes, only_key=args.key)
    else:
        any_emitted |= rep.emit_remaining(skip_notes=args.skipnotes, only_key=args.key)

    summary = integrity(records, rep) if online else None

    rep.render_summary(len(entries), len(contexts), skip_notes=args.skipnotes,
                       tex_mode=tex_mode, any_findings=any_emitted, integrity=summary)

    if args.json:
        print(f"\nJSON report written to {args.json}")

    return 1 if rep.count(Severity.ERROR) else 0


if __name__ == "__main__":
    sys.exit(main())
