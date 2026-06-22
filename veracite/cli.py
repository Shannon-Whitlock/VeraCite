"""Command-line interface: path discovery, argument parsing, and the run loop
that drives the syntax, static, record/status and (optional) LLM layers.
"""

import argparse
import json
import os
import sys

from .config import HTTP_BACKEND, SETTINGS, load_settings
from .llm import (LLM_PROVIDERS, collect_tex, find_citation_contexts,
                  find_citation_groups, resolve_provider)
from .parser import parse_bib
from .pipeline import analyze_entry
from .report import enable_ansi_colors, Report, Severity
from .rules import run_entry_rules, run_file_rules, syntax_pass
from .verify import chronological_order, integrity

DESCRIPTION = """\
VeraCite -- a bibliography health checker for LaTeX projects.

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
    ap = argparse.ArgumentParser(
        description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    ap.add_argument("--key", help="restrict online checks to one citation key")
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

    # CLI flags override settings; settings supply the defaults.
    delay = args.delay if args.delay is not None else SETTINGS.get("request_delay", 0.4)
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
    provider = model = None
    if online and args.llm:
        provider_name = args.llm_provider or SETTINGS.get("llm_provider", "claude")
        model = SETTINGS.get("llm_models", {}).get(provider_name, "")
        provider = resolve_provider(provider_name, rep)
        print(f"NOTE: --llm sends the sentence(s) around each \\cite from your .tex "
              f"to the LLM provider ({provider_name!r}). Do not use on a confidential "
              f"manuscript.", file=sys.stderr)
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
    by_key = {e.key: e for e in entries}   # for naming a citation's group-mates
    # Advisory: note any \cite{} group whose members are out of chronological order
    # (offline, from the bib years). Done before the loop so notes attach to the
    # group's first entry and emit in bibtex order with everything else.
    if tex_mode:
        chronological_order(cite_groups, by_key, rep)
    for i, e in enumerate(entries, 1):
        run_entry_rules(e, rep)                  # offline static checks
        analyzed = online and (analyze is None or e.key in citedset)
        if not analyzed and tex_mode and e.key not in citedset:
            rep.add(Severity.INFO, e, "not cited in the .tex sources; "
                    "skipped from record/status/LLM analysis", category="not_cited")
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
        if analyzed and (not args.key or e.key == args.key):
            # Live progress while the (possibly slow) online lookup runs -- but ONLY
            # to an interactive terminal, as a transient \r line that the entry's
            # header then overwrites. When stderr is redirected (a .log file, a
            # pipe) it is suppressed entirely, so the saved log carries no progress
            # noise or stray carriage returns; the '[i/N]' counter lives on the
            # header, which prints to stdout regardless.
            if sys.stderr.isatty():
                print(f"  [{i:>{width}}/{total}] {e.key}\r", end="",
                      file=sys.stderr, flush=True)
            analyze_entry(e, results, statuses, rep, delay=delay, timeout=timeout,
                          provider=provider, model=model, contexts=contexts,
                          by_key=by_key)
        if args.sort == "entry":
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
        # Triage view: one global list, errors first, instead of per-entry blocks.
        any_emitted |= rep.emit_by_severity(skip_notes=args.skipnotes)
    else:
        any_emitted |= rep.emit_remaining(skip_notes=args.skipnotes)

    # Layer 6: bibliography integrity score + coverage. Rates are computed over the
    # entries actually checked online (in --tex mode, the cited subset; otherwise
    # every entry) -- uncited entries are skipped by design, not failures, so they
    # do not drag the score of the references that were examined. File-wide defects
    # (duplicate keys/DOIs) still count regardless of citation.
    summary = integrity(entries, statuses, results, rep) if online else None

    rep.render_summary(len(entries), len(contexts), skip_notes=args.skipnotes,
                       tex_mode=tex_mode, any_findings=any_emitted, integrity=summary)

    if args.json:
        try:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(rep.to_json(summary=summary, results=results, statuses=statuses),
                          fh, indent=2, ensure_ascii=False)
            print(f"\nJSON report written to {args.json}")
        except OSError as ex:
            # The analysis already ran and printed; a bad --json path should not
            # mask that with a traceback. Report it and let the exit code stand.
            print(f"\nwarning: could not write JSON report to {args.json}: {ex}",
                  file=sys.stderr)

    return 1 if rep.count(Severity.ERROR) else 0


if __name__ == "__main__":
    sys.exit(main())
