"""A small, dependency-free BibTeX parser.

`parse_bib` returns (entries, problems). It is brace/quote aware and, on a
structurally broken entry, records a problem and resyncs at the next '@entry{'
rather than swallowing the rest of the file -- so one bad entry cannot silently
drop the entries after it.
"""

import bisect
import re

# Start of a BibTeX entry: '@type{' or '@type(' -- BibTeX accepts either delimiter,
# and '@string'/'@preamble' in the wild are commonly written with parens (e.g.
# '@String(JOV = {J. Vis.})'). Group 2 is the opening delimiter so we know which
# closer ('}' or ')') to match. Used both to find entries and to resync after a
# malformed one rather than swallowing the rest of the file.
ENTRY_START = re.compile(r"@(\w+)\s*([{(])")

# A field declaration inside an entry: 'name = ' or a malformed 'name {'/'name "'
# with the '=' missing. Group 2 distinguishes the two.
FIELD_DECL = re.compile(r"(?m)^[^%\S\n]*([A-Za-z][\w-]*)\s*([={\"])")


class Entry:
    """One parsed BibTeX entry: type, key, lowercased fields, source line, and
    the raw text (kept for checks that inspect formatting, e.g. encoding)."""

    __slots__ = ("etype", "key", "fields", "lineno", "raw", "_field_lines")

    def __init__(self, etype, key, fields, lineno, raw):
        self.etype = etype.lower()
        self.key = key
        self.fields = fields
        self.lineno = lineno
        self.raw = raw
        self._field_lines = None

    def get(self, name, default=""):
        return self.fields.get(name.lower(), default)

    def field_line(self, name):
        """Source line of a given field (`name = ...`), or the entry's start line
        if the field is not located. Lets a finding point at the exact line to
        edit rather than the whole entry."""
        if self._field_lines is None:
            self._field_lines = {}
            for m in re.finditer(r"(?m)^[^%\S\n]*([A-Za-z][\w-]*)\s*=", self.raw):
                fld = m.group(1).lower()
                if fld not in self._field_lines:
                    self._field_lines[fld] = self.lineno + self.raw[:m.start()].count("\n")
        return self._field_lines.get(name.lower(), self.lineno)


def _blank_comments(text):
    """Return `text` with the content of every '%' line comment replaced by
    spaces, preserving length and newlines so byte offsets and line numbers are
    unchanged. A '%' escaped as '\\%' is literal and does not start a comment.

    Biber/biblatex (which this targets) treat a '%' as a line comment, so a
    commented-out '%@article{...}' block is not an entry at all. Scanning a
    blanked copy keeps such blocks from being parsed as zero-field entries (which
    would fabricate missing-field and duplicate-key findings) while the original
    text is still used for line numbers and the entry's raw source.

    A '%' *inside* a brace- or quote-delimited field value is NOT a comment --
    it is a literal character (e.g. the URL-encoded '%3A' in a doi.org url, or a
    '50%' in a title). Blanking it would eat the rest of the line, including the
    value's closing brace, and fabricate an "unbalanced braces" error on an
    otherwise sound entry. So track brace depth and quote state and only honour a
    '%' as a comment at the top level, outside any value."""
    out = []
    depth = 0          # '{' nesting depth within the current entry body
    in_quote = False   # inside a "..."-delimited value
    for line in text.splitlines(keepends=True):
        nl = len(line) - len(line.rstrip("\r\n"))
        body, eol = line[:len(line) - nl], line[len(line) - nl:]
        kept = []
        k = 0
        while k < len(body):
            c = body[k]
            if c == "\\":            # escape: keep this char and the next verbatim
                kept.append(body[k:k + 2])
                k += 2
                continue
            if c == "%" and depth == 0 and not in_quote:
                # A real line comment -- blank from here to end of line.
                kept.append(" " * (len(body) - k))
                k = len(body)
                break
            if c == '"' and depth == 0:
                in_quote = not in_quote
            elif c == "{":
                depth += 1
            elif c == "}" and depth > 0:
                depth -= 1
            kept.append(c)
            k += 1
        out.append("".join(kept) + eol)
    return "".join(out)


def parse_bib(text):
    """Parse BibTeX source into (entries, problems). Brace/quote aware; skips
    @comment/@preamble and '%' line comments. A '@string' definition (either
    '{...}' or '(...)' delimited) is collected into a macro table and its
    abbreviations are expanded in field values, including '#' concatenation, so a
    'journal = prb' resolves to its full name rather than comparing the bare macro
    against the record. On a structurally broken entry (a brace or quote that never
    closes before the next entry) it records a structural problem and resyncs at the
    next '@entry{'. `problems` is a list of (lineno, message)."""
    entries, problems = [], []
    # Scan a comment-blanked copy for structure (so a '%@article{...}' is never
    # parsed as an entry), but keep `text` for line numbers and raw entry source.
    scan = _blank_comments(text)
    i, n = 0, len(scan)
    line_of = _line_index(text)
    # '@string' abbreviations, accumulated as we scan. A later @string may reference
    # an earlier one, so values are resolved against the table built so far. Field
    # values are then expanded against it (bare macro names and '#' concatenation).
    macros = {}
    while i < n:
        m = ENTRY_START.search(scan, i)
        if not m:
            break
        at, body_start = m.start(), m.end()
        opener = m.group(2)
        # Find this entry's closing delimiter. Stop early if a new '@entry{' starts
        # at the beginning of a line: that is unambiguously the next entry, so
        # this one's braces never balanced. (Checking the line start, not just
        # depth==1, lets us recover even from a deeply unclosed inner brace.)
        # Brace counting runs on `scan` so a '%'-commented brace is not counted.
        # A brace-delimited entry closes when brace depth returns to 0 (the opening
        # '{' counts as depth 1). A paren-delimited entry (common for '@string') closes
        # at a top-level ')' -- one seen while no '{...}' field value is open -- so its
        # braces are tracked separately and the close is the unbraced ')'.
        depth, j, resync = 1, body_start, None
        if opener == "(":
            depth = 0   # for parens, `depth` tracks only braces inside the body
        while j < n and (opener == "(" or depth):
            c = scan[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == ")" and opener == "(" and depth == 0:
                j += 1          # consume the closing ')'
                break
            elif c == "@" and (j == 0 or scan[j - 1] == "\n") and ENTRY_START.match(scan, j):
                resync = j      # next entry begins before this one closed
                break
            j += 1

        etype = m.group(1).lower()
        # Unbalanced iff we never reached the matching closer: a brace entry whose
        # depth never returned to 0, or any entry that hit the next '@entry{' first.
        closed_paren = opener == "(" and resync is None and j <= n and scan[j - 1:j] == ")"
        unbalanced = resync is not None or (opener == "{" and depth != 0) or \
            (opener == "(" and not closed_paren)
        if unbalanced:
            # Unbalanced: report and resync at the next entry (or give up at EOF).
            if etype not in ("comment", "string", "preamble"):
                key = _peek_key(scan[body_start:body_start + 200])
                problems.append((line_of(at),
                                 f"@{etype}{{{key}}}: unbalanced braces; entry does not "
                                 f"close before the next entry (structural BibTeX error)"))
            i = resync if resync is not None else n
            continue

        body = scan[body_start:j - 1]
        i = j
        if etype == "string":
            _collect_string(body, macros)
            continue
        if etype in ("comment", "preamble"):
            continue
        # An extra '}' closes the entry one brace early. It shows up two ways:
        # a stray '}' left between this entry and the next, or -- when the stray
        # brace itself became the closer -- a body whose own braces don't balance.
        # The file-level counter can mask this against an unclosed '{' elsewhere,
        # so catch it per entry.
        nxt = ENTRY_START.search(scan, j)
        between = scan[j:nxt.start() if nxt else n]
        body_unbalanced = body.count("{") != body.count("}")
        if between.lstrip(" \t\r\n,").startswith("}") or body_unbalanced:
            problems.append((line_of(j),
                             f"@{etype}{{{_peek_key(body)}}}: stray '}}' after the entry "
                             f"closed (an extra closing brace; structural BibTeX error)"))
        # A 'name = value' sitting between this entry and the next is outside any
        # entry, so BibTeX silently drops it. There are two very different causes,
        # and they need different advice:
        #   * ONE stray line -- a DOI/url the author appended after the closing '}'.
        #     The fix is to move it inside the entry above.
        #   * SEVERAL consecutive field lines (often with their own closing '}') --
        #     a whole entry whose '@type{key,' header line was deleted. The fix is
        #     to RESTORE the header, NOT to fold the fields into the previous entry
        #     (which would corrupt it). Misreading this as a stray field of the
        #     preceding key is exactly the misattribution to avoid.
        stray = [sm for sm in re.finditer(r"(?m)^([^%\n]*?)\b([A-Za-z][\w-]*)\s*=", between)
                 if not sm.group(1).strip()]
        # An orphan closing brace in the gap is signalled by the gap's braces being
        # unbalanced (more '}' than '{') -- that extra '}' is the deleted entry's
        # own closer. A balanced '}' belongs to a field value, not a phantom entry.
        gap_orphan_close = between.count("}") > between.count("{")
        if len(stray) >= 2 or (stray and gap_orphan_close):
            first = stray[0]
            problems.append((line_of(j + first.start(2)),
                             f"unlabelled block after @{etype}{{{_peek_key(body)}}}: "
                             f"{len(stray)} field line(s) ('{first.group(2).lower()}', ...) "
                             f"with no '@type{{key,' header -- this looks like an entry "
                             f"whose header line was deleted; restore it (BibTeX drops "
                             f"this block and it unbalances the file's braces)"))
        elif stray:
            sm = stray[0]
            problems.append((line_of(j + sm.start(2)),
                             f"@{etype}{{{_peek_key(body)}}}: field '{sm.group(2).lower()}' "
                             f"is outside the entry (after its closing '}}'); BibTeX "
                             f"ignores it -- move it inside the entry"))
        fields, key = _parse_body(body, macros)
        if not key:
            problems.append((line_of(at), f"@{etype}: entry has no citation key"))
            continue
        entries.append(Entry(m.group(1), key, fields, line_of(at), text[at:j]))
    return entries, problems


def _peek_key(s):
    """Best-effort citation key from the start of an entry body, for error text."""
    return s.split(",", 1)[0].strip() or "?"


def _collect_string(body, macros):
    """Record a '@string' definition ('name = value') into `macros`. The value is
    expanded against the macros defined so far, so '@string{a={X}} @string{b=a # {Y}}'
    resolves b to 'XY'. A malformed @string (no '=') is ignored, matching biber's
    tolerance. Names are lowercased; BibTeX macro lookup is case-insensitive."""
    eq = _find_top_level(body, "=")
    if eq == -1:
        return
    name = body[:eq].strip().lower()
    if not re.fullmatch(r"[a-z][\w-]*", name):
        return
    value, _ = _read_value(body[eq + 1:].lstrip(), macros)
    macros[name] = value


def _parse_body(body, macros=None):
    key, rest = _split_first_comma(body)
    fields = {}
    while rest:
        rest = rest.lstrip().lstrip(",").lstrip()
        if not rest:
            break
        eq = _find_top_level(rest, "=")
        if eq == -1:
            break
        name = rest[:eq].strip().lower()
        value, rest = _read_value(rest[eq + 1:].lstrip(), macros)
        # A clean field name is a single token; anything with whitespace/braces
        # means we ran past a malformed (e.g. '='-less) field. Skip it -- the
        # syntax pass reports the structural cause -- rather than store junk.
        if name and re.fullmatch(r"[a-z][\w-]*", name):
            fields[name] = value
    return fields, key.strip()


def iter_field_decls(body):
    """Walk an entry body and yield one (offset, name, sep) per top-level field
    declaration, in order. `offset` is the index in `body` where the field name
    starts; `name` is the lowercased field name; `sep` is the delimiter that
    followed it -- '=' for a well-formed 'name = value', or '{'/'"' for a
    malformed 'name {...}' / 'name "..."' whose '=' is missing.

    This is the parser's single, authoritative account of where the real fields
    are. It advances over each value with the same atom reader the parser uses
    (`_read_value`), so a '{...}' or a possibly multi-line '"..."' value -- which
    may itself contain commas, '=' signs, or bare words -- is skipped *wholesale*
    and never mistaken for a new field. Any check that needs to reason about field
    boundaries (e.g. the syntax pass's missing-'=' detection) must use this rather
    than re-scanning the raw text with a brace-only heuristic, which is blind to
    quote-delimited values and so fabricates phantom fields ('New\\n York' ->
    a bogus 'york' field). Malformed declarations are reported, not stored, so the
    walk keeps going to find the entry's remaining real fields."""
    _, rest = _split_first_comma(body)
    pos = len(body) - len(rest)          # offset of `rest` within `body`
    while True:
        stripped = rest.lstrip(" \t\r\n,")
        pos += len(rest) - len(stripped)
        rest = stripped
        if not rest:
            return
        # A field declaration is 'name' then a separator: '=' for a well-formed
        # field, or '{'/'"' when the '=' was dropped. A bibtex field name is a bare
        # token with no braces or quotes, so the separator is simply the FIRST of
        # '=', '{', '"' in what remains -- a plain first-occurrence scan (NOT a
        # brace-depth scan, which would treat the '{' of a missing-'=' 'title {...}'
        # as opening a value and skip right past the very error we are looking for).
        sep_idx = next((k for k, c in enumerate(rest) if c in '={"'), -1)
        if sep_idx == -1:
            return
        name = rest[:sep_idx].strip()
        sep = rest[sep_idx]
        if name and re.fullmatch(r"[A-Za-z][\w-]*", name):
            yield pos, name.lower(), sep
        # Advance past this declaration's value. For '=' the value follows the '=';
        # for a missing-'=' separator the value starts *at* the '{'/'"' itself.
        after = rest[sep_idx + 1:] if sep == "=" else rest[sep_idx:]
        _, tail = _read_value(after.lstrip())
        consumed = len(rest) - len(tail)
        pos += consumed
        rest = tail


def field_occurrences(body, macros=None):
    """All values for each field name in an entry body, in order, as
    {name: [value, ...]}. Unlike _parse_body's dict (which keeps only the last
    value), this preserves repeats so a duplicate-field check can compare them.
    Uses the same top-level scan, so it agrees with the parser on what a value is."""
    _, rest = _split_first_comma(body)
    occ = {}
    while rest:
        rest = rest.lstrip().lstrip(",").lstrip()
        if not rest:
            break
        eq = _find_top_level(rest, "=")
        if eq == -1:
            break
        name = rest[:eq].strip().lower()
        value, rest = _read_value(rest[eq + 1:].lstrip(), macros)
        if name and re.fullmatch(r"[a-z][\w-]*", name):
            occ.setdefault(name, []).append(value)
    return occ


def _read_value(s, macros=None):
    """Read one field value and return (value, rest). A value is one or more atoms
    -- '{...}', '"..."', or a bare word -- joined by '#' concatenation (standard
    BibTeX). A bare word is a '@string' abbreviation: resolved against `macros` when
    defined, else kept verbatim (so a month name like 'may', or a number, is
    untouched and the style/identifier checks still see it). With no '#' and no
    matching macro this returns exactly what the old single-atom reader did."""
    value, rest = _read_atom(s, macros)
    # '#' concatenation: 'a # {b} # "c"'. Join atoms until no '#' follows.
    after = rest.lstrip()
    while after.startswith("#"):
        piece, rest = _read_atom(after[1:].lstrip(), macros)
        value += piece
        after = rest.lstrip()
    return value, rest


def _read_atom(s, macros=None):
    """Read a single value atom -- '{...}', '"..."', or a bare word -- returning
    (text, rest). A bare word that names a '@string' macro expands to its value."""
    if not s:
        return "", ""
    if s[0] == "{":
        depth = 0
        for j, c in enumerate(s):
            depth += (c == "{") - (c == "}")
            if depth == 0:
                return _strip_braces(s[:j + 1]), s[j + 1:]
        return _strip_braces(s), ""
    if s[0] == '"':
        depth = 0
        for j in range(1, len(s)):
            depth += (s[j] == "{") - (s[j] == "}")
            if s[j] == '"' and depth == 0:
                return s[1:j], s[j + 1:]
        return s[1:], ""
    # A bare word ends at the next top-level ',' or '#' (concatenation), whichever
    # comes first -- a '#' splits two atoms, so the bare token must not absorb it.
    cut = _find_top_level_any(s, ",#")
    raw, rest = (s, "") if cut == -1 else (s[:cut], s[cut:])
    token = raw.strip()
    if macros and token.lower() in macros:
        return macros[token.lower()], rest
    return token, rest


def _strip_braces(v):
    v = v.strip()
    return v[1:-1] if v.startswith("{") and v.endswith("}") else v


def _split_first_comma(s):
    idx = _find_top_level(s, ",")
    return (s, "") if idx == -1 else (s[:idx], s[idx + 1:])


def _find_top_level(s, ch):
    depth = 0
    for k, c in enumerate(s):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ch and depth == 0:
            return k
    return -1


def _find_top_level_any(s, chars):
    """Index of the first of `chars` at brace depth 0, or -1. Like _find_top_level
    but stops at any one of several delimiters (used to end a bare value atom at a
    ',' or a concatenation '#')."""
    depth = 0
    for k, c in enumerate(s):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c in chars and depth == 0:
            return k
    return -1


def _line_index(text):
    starts = [0]
    for k, c in enumerate(text):
        if c == "\n":
            starts.append(k + 1)
    return lambda pos: bisect.bisect_right(starts, pos)
