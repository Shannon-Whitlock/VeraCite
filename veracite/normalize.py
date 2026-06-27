"""Text normalization shared across the checks.

Folds TeX, accents, math and markup to comparable plain text, and extracts
structured bits (author surnames/given names, arXiv ids) from BibTeX fields.
"""

import html
import re
import unicodedata

_SPECIAL_LETTERS = {
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ı": "i", "ħ": "h",
    # Ligatures NFKD does NOT decompose (they are letters, not accented forms), so
    # they must be expanded explicitly or 'Hjertenæs' never folds to 'Hjertenaes'
    # (the bib's ASCII transliteration) and the same author reads as two people.
    "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
}
_MATHML_RE = re.compile(r"<m(?:ml:)?math\b.*?</m(?:ml:)?math>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_TEX_MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$]*\$|\\\(.*?\\\)|\\\[.*?\\\]", re.S)
# New-style arXiv id (1234.5678). Anchored so it is NOT matched inside a DOI
# such as 10.1109/FOCS54457.2022.00117: the four leading digits must not be
# preceded by a digit (a fragment of a longer number), and the id must not be
# followed by a dot+digit (a continuing number). A preceding dot is allowed so
# an arXiv DOI '10.48550/arXiv.2103.16313' still resolves.
ARXIV_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(v\d+)?(?!\.?\d)")
# Old-style arXiv id: a KNOWN archive name (optionally with a subject class) then
# '/YYMMNNN'. Restricting to the real archive vocabulary stops a path like
# 'springer.com/book/9780387952741' from being read as an arXiv id ('book/...').
_ARXIV_ARCHIVES = (
    "astro-ph|cond-mat|gr-qc|hep-ex|hep-lat|hep-ph|hep-th|math-ph|nlin|nucl-ex|"
    "nucl-th|physics|quant-ph|math|cs|q-bio|q-fin|stat|eess|econ")
ARXIV_OLD_RE = re.compile(r"\b((?:" + _ARXIV_ARCHIVES + r")(?:\.[A-Za-z]{2})?/\d{7})")
# A value that CONTAINS a DOI substring -- never mine an arXiv id out of one.
DOI_RE = re.compile(r"\b10\.\d{4,9}/")
# A value that IS a bare DOI, whole-string: '10.' + registrant (dot-separated
# digits) + '/' + a suffix of one-or-more '/'-joined segments with NO whitespace.
# Anchored, so a DOI merely *embedded* in junk (e.g. a field that wrapped
# 'https:\n //doi:10.1103/...') does NOT match -- such a value is not usable as a
# DOI for resolution or a verify link. This is the single definition of "is a
# usable DOI", shared by the resolver and the doi_format rule so they agree on what
# counts.
#
# Security: no suffix segment may be entirely dots ('.' or '..'). A real DOI suffix
# never needs one, but a crafted 'doi' like '10.1/../../etc/passwd' would otherwise
# pass the gate and -- since DOIs keep '/' literal in the API URL -- be normalized by
# the HTTP client into a traversing path on the trusted API host (api.crossref.org/
# etc/passwd). The `_DOI_SEG` lookahead rejects an all-dots segment, closing that
# request-path injection at the one shared gate every source resolves through.
_DOI_SEG = r"(?!\.+(?:/|$))[^/\s]+"
DOI_FULL_RE = re.compile(r"^10\.\d+(\.\d+)*/" + _DOI_SEG + r"(?:/" + _DOI_SEG + r")*$")
# A 13- or 10-digit ISBN (publisher URLs often embed one, e.g. springer .../9780387952741).
_ISBN13_RE = re.compile(r"(?<!\d)(97[89]\d{10})(?!\d)")
# Generational name suffixes, dropped before keying a surname so 'Hunt III' and
# 'Hunt' fold alike.
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# Markers of a collaboration/consortium "author" that must not be compared
# name-by-name against a record's author list.
_COLLAB_MARKERS = ("collaboration", "collaborations", "collaborators",
                   "consortium", "team", "group", "et al")


def deaccent(s):
    """Fold accented and special Latin letters to ASCII for comparison."""
    s = "".join(_SPECIAL_LETTERS.get(c, c) for c in s)
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


# clean_tex patterns, compiled once (clean_tex runs on every author/title/journal).
# The special-letter macros are kept as an ORDERED list of compiled (pattern,
# repl) pairs -- applied sequentially exactly as before -- because the per-pattern
# \b boundary makes the result order-dependent for adjacent macros; collapsing
# them into one alternation would change behavior. Compiling once (vs rebuilding
# via re.escape per call) is the win.
#
# The accent argument may be a plain letter ('\'a'), or the dotless-i / no-dot-j
# macros that are the correct way to put an accent on an i/j: "\'{\i}" = í,
# "\^{\j}" = ĵ. Those must fold to the base letter ('i'/'j'), so the argument
# alternation accepts '\i'/'\j' as well as '[a-zA-Z]'. Without this the accent
# regex misses '\'{\i}' and the bare '\i' is later deleted, dropping the letter.
_ACCENT_MACRO_RE = re.compile(r'\\[`\'"^~=.Hvuc]\s*\{?\s*(?:\\([ij])|([a-zA-Z]))\s*\}?')
# math-mode-aware idiom '\ifmmode <math>\else <text>\fi': keep the text-mode
# (\else) branch and drop the conditional delimiters, so e.g.
# 'Pi\ifmmode \check{z}\else \v{z}\fi{}orn' reduces to 'Pi\v{z}orn' (-> Pižorn)
# rather than folding fragments of both branches.
_IFMMODE_RE = re.compile(r"\\ifmmode\b.*?\\else\b(.*?)\\fi\b", re.S)
# Dotless-i / no-dot-j as bare letter macros ('{\i}', '{\j}') outside an accent.
# Mapped to 'i'/'j' BEFORE the generic macro stripper would delete them.
_DOTLESS_RE = re.compile(r"\\([ij])(?![a-zA-Z])")
_TEX_LETTER_SUBS = [(re.compile(re.escape(pat) + r"\b"), repl)
                    for pat, repl in (("\\o", "o"), ("\\O", "O"), ("\\l", "l"),
                                      ("\\ss", "ss"), ("\\aa", "a"), ("\\AA", "a"),
                                      ("\\ae", "ae"), ("\\oe", "oe"))]
_TEX_MACRO_RE = re.compile(r"\\[a-zA-Z]+")
_WS_RE = re.compile(r"\s+")


def clean_tex(s):
    """Reduce TeX (accent macros, braces, math) to comparable plain text.
    Also decodes HTML entities (Crossref returns '&amp;', '&#x2013;') so a bib
    value compares against a registry value without spurious entity mismatches."""
    s = html.unescape(s)
    s = s.replace("\\&", "&")        # TeX-escaped ampersand -> plain '&'
    s = _IFMMODE_RE.sub(r"\1", s)    # math-mode conditional -> its text branch
    s = _ACCENT_MACRO_RE.sub(lambda m: m.group(1) or m.group(2), s)
    s = _DOTLESS_RE.sub(r"\1", s)    # bare '\i'/'\j' -> 'i'/'j'
    for pat_re, repl in _TEX_LETTER_SUBS:
        s = pat_re.sub(repl, s)
    s = _TEX_MACRO_RE.sub("", s)
    s = s.replace("{", "").replace("}", "").replace("$", "")
    return deaccent(_WS_RE.sub(" ", s)).strip()


def is_collaboration(name):
    """True if an author token denotes a collaboration/consortium rather than a
    person -- a marker word, or a brace-wrapped group containing internal 'and'
    (e.g. '{Google Quantum AI and collaborators}'). Such tokens cannot be folded
    to a single surname and must be skipped in author comparison."""
    low = name.lower()
    if any(m in low for m in _COLLAB_MARKERS):
        return True
    stripped = name.strip()
    if stripped.startswith("{") and stripped.endswith("}") and " and " in low:
        return True
    return False


def _surname_token(a):
    """The surname portion of one author entry ('Last, First' or 'First Last'),
    with a trailing generational suffix (Jr/III/...) dropped."""
    if "," in a:
        last = a.split(",")[0]
    else:
        parts = a.split()
        last = parts[-1] if parts else a
        # 'Harry A. Hunt III' -> drop the trailing 'III' so the surname is 'Hunt'.
        if len(parts) >= 2 and re.sub(r"[^a-z]", "", parts[-1].lower()) in _NAME_SUFFIXES:
            last = parts[-2]
    # 'Hunt III, Harry B.' -> the comma form can also carry the suffix.
    last = re.sub(r",?\s+(?:jr|sr|ii|iii|iv|v)\.?\s*$", "", last, flags=re.I)
    return last


def strip_math(s):
    """Drop math/markup so titles compare on prose words only (Crossref returns
    MathML whose token order will not match a TeX source like '{$^{1}S_{0}$}')."""
    s = _MATHML_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = _TEX_MATH_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_tags(text):
    """Strip HTML/JATS/XML tags and collapse whitespace (e.g. for abstracts)."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


def title_is_miscased(title):
    """True if a title's overall casing is wrong -- it is (mostly) SHOUTED in
    uppercase rather than title/sentence case, the convention some journals bake
    into their export. Judged by the fraction of multi-letter words that are
    ALL-CAPS, so a normal title carrying one or two real acronyms (BGK, DNA) does
    NOT trip it -- only a title whose casing as a whole needs fixing does.

    TeX/braces are stripped first (a brace-protected '{BGK}' is the author's
    deliberate casing, not a defect). Returns False for short titles, where the
    fraction is not meaningful."""
    plain = clean_tex(title)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", plain) if len(w) >= 2]
    if len(words) < 4:
        return False
    caps = sum(1 for w in words if w.isupper())
    return caps / len(words) >= 0.6


def fold_surname(name):
    """Reduce a surname to a lowercase ASCII key for matching. Deaccent FIRST so a
    Unicode form and its ASCII transliteration collapse the same way (the Nordic
    ligature 'Hjertenæs' -> 'hjertenaes', matching a bib that wrote 'Hjertenaes'),
    then fold the German umlaut transliterations (oe/ue/ae/ss -> o/u/a/s) so
    'Muller' == 'Mueller' == 'Müller'."""
    s = deaccent(clean_tex(name)).lower()
    for a, b in (("oe", "o"), ("ue", "u"), ("ae", "a"), ("ss", "s")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z]", "", s)


def _split_on_and(field):
    """Split a BibTeX author field on ' and ' at brace depth 0 only, so a
    brace-wrapped collaboration like '{Google Quantum AI and collaborators}'
    stays a single token instead of being torn in two."""
    s = re.sub(r"\s+", " ", field.replace("\n", " "))
    out, depth, start, i = [], 0, 0, 0
    while i < len(s) - 4:
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and s[i:i + 5].lower() == " and ":
            out.append(s[start:i])
            i += 5
            start = i
            continue
        i += 1
    out.append(s[start:])
    return out


# A truncation marker glued to the END of an author field rather than written as a
# separate '... and others' token: a literal 'et al.' or a bare 'al.' (the user
# dropped the 'et'). It rides on the last real author's token ('Pedram Roushan al.'),
# so without stripping it 'al.' becomes a phantom surname and misfires as a
# metadata_mismatch. Matched case-insensitively, with a LaTeX tie or run-together
# form ('et~al.', 'et.al.', 'etal'), at the very end of a token.
_TAIL_ETAL_RE = re.compile(r"[\s,]+(?:et[\s~.]*)?al\.?$", re.I)


def has_etal_marker(field):
    """True when the field carries a MALFORMED truncation: a literal 'et al.' (any
    written form) or a bare trailing 'al.' with the 'et' dropped. These are the
    WARN cases (a rendering baked into the data), distinct from the valid
    'and others' marker."""
    return bool(_TAIL_ETAL_RE.search(field)) \
        or bool(re.search(r"\bet[\s~.]+al\.?", field, re.I))


def is_truncated(field):
    """True when an author/editor field is truncated by ANY completeness marker:
    the valid 'and others' sentinel, a literal 'et al.', or a bare trailing 'al.'.
    The single predicate every layer shares so 'truncated' means the same thing in
    the rule, the record comparison, and the cross-source check."""
    return "others" in field.lower() or has_etal_marker(field)


def _author_tokens(field):
    """Yield the individual author strings of an author field, skipping the
    'others' sentinel and collaboration tokens (split on ' and ' outside braces).
    A trailing truncation marker ('et al.' / bare 'al.') glued to a token is
    stripped so it is not mistaken for a surname; a token that is ONLY the marker
    is dropped like 'others'."""
    for a in _split_on_and(field):
        a = _TAIL_ETAL_RE.sub("", a.strip()).strip()
        if not a or a.lower() in ("others", "et", "al", "al.") or is_collaboration(a):
            continue
        yield a


def split_authors(field):
    """Folded surname keys for each author in a BibTeX author field ('and'
    separated), skipping the 'others' sentinel and collaboration tokens. A
    trailing generational suffix (Jr/III/...) is dropped before folding."""
    return [fold_surname(_surname_token(a)) for a in _author_tokens(field)]


def author_surnames_display(field):
    """The original, human-readable surnames for each author (same order and
    filtering as split_authors), for showing in a finding message instead of the
    folded matching key. Keyed lookups still use split_authors' folded form; this
    is display-only, so 'Ali Furkan Biten' shows as 'Biten', not 'biten'."""
    return [clean_tex(_surname_token(a)).strip() for a in _author_tokens(field)]


def shouted_surnames(field):
    """Surnames written in ALL-CAPS in an author field ('CHEN', 'ZHANG'), the
    SHOUTING convention some publishers export. A surname is 'shouted' if it has
    two or more letters and every letter is uppercase after TeX is stripped; a
    deliberately brace-protected '{IBM}' or a one-letter token is not counted.
    Returns the offending surnames in order (empty if the list is sensibly cased).
    Lets a rule flag the casing without re-implementing author parsing."""
    out = []
    for a in _author_tokens(field):
        surname = clean_tex(_surname_token(a)).strip()
        letters = [c for c in surname if c.isalpha()]
        if len(letters) >= 2 and all(c.isupper() for c in letters) and surname not in out:
            out.append(surname)
    return out


def bib_given_names(field):
    """Map folded surname -> first given-name token from a BibTeX author field.
    Handles both 'Last, First' and 'First Last' forms. Used to verify given names
    against a record that carries them (Crossref)."""
    out = {}
    for a in _author_tokens(field):
        if "," in a:
            _, _, given = a.partition(",")
        else:
            parts = a.split()
            given = " ".join(parts[:-1]) if len(parts) > 1 else ""
        g = clean_tex(given).strip().split()
        if g:
            out[fold_surname(_surname_token(a))] = g[0]
    return out


# Nature/NPG 'Electronic Page' export: 'NNN EP -' (or 'NNN EP') means "first page
# NNN, electronic" -- it is a start page only, not a range. Reduce it to 'NNN' so
# it compares against a registry's full range as a start-page match, not a junk
# value. (re.I so a lowercased 'ep' is caught too.)
_EP_PAGE_RE = re.compile(r"^\s*(\d+)\s*EP\s*-?\s*$", re.I)


def norm_pages(p):
    """Normalize a page range: en/em dashes to '-', single separator, no spaces.
    The Nature 'NNN EP -' electronic-page form is reduced to its start page."""
    m = _EP_PAGE_RE.match(p)
    if m:
        return m.group(1)
    p = p.replace("–", "-").replace("—", "-")
    return re.sub(r"-+", "-", p).replace(" ", "").strip()


def biblatex_pages(p):
    """The biblatex-canonical written form of a page value, for a SUGGESTED edit (a
    value the consumer will paste into the .bib). A range uses the '--' separator
    (biblatex renders it as an en-dash); a single page or article number is left as
    is. Registries return '920-926' with a single hyphen, but the style rule treats
    a single hyphen as non-canonical -- so a suggestion must hand back '920--926',
    not a value that would itself trip the dash-style check."""
    norm = norm_pages(str(p or ""))
    if re.match(r"^\d+-\d+$", norm):
        return norm.replace("-", "--")
    return norm


def extract_arxiv_id(*values):
    """First arXiv id (new 1234.5678 or old quant-ph/9705052 form) found in any
    of the given field values, or None. A DOI-shaped value is skipped (so a
    conference DOI like 10.1109/FOCS54457.2022.00117 does not yield '4457.2022')
    unless it is an arXiv DOI (10.48550/arXiv.NNNN.NNNNN), which does carry one."""
    for v in values:
        if not v:
            continue
        if DOI_RE.search(v) and "arxiv" not in v.lower():
            continue
        m = ARXIV_RE.search(v) or ARXIV_OLD_RE.search(v)
        if m:
            return m.group(1)
    return None


def extract_isbn(*values):
    """First ISBN-13 found in any of the given values (e.g. embedded in a
    publisher URL), or ''. Only the unambiguous 978/979 13-digit form is mined."""
    for v in values:
        if not v:
            continue
        m = _ISBN13_RE.search(re.sub(r"[\s-]", "", v))
        if m:
            return m.group(1)
    return ""


def is_preprint(e):
    """True if the entry looks like an arXiv preprint (eprint field, or 'arxiv'
    in its journal/doi, or a @misc that mentions arxiv)."""
    return bool(e.get("eprint")
                or "arxiv" in e.get("journal", "").lower()
                or "arxiv" in e.get("doi", "").lower()
                or (e.etype == "misc" and "arxiv" in e.raw.lower()))


# DOI suffix fragments that denote a book/proceedings *series* rather than a
# journal: an @article carrying one is a chapter mis-typed as an article, and a
# DOI like 10.1090/conm/717 resolves to the *volume*, not the chapter. Kept to
# high-confidence, well-known book-series prefixes (AMS Contemporary Mathematics
# /conm and Proceedings of Symposia /pspum, Springer LNCS/book DOIs, SPIE
# proceedings) so an ordinary journal DOI is never matched. NB: AIP's
# 10.1063/1.<digits> is NOT a usable signal -- the same prefix serves both AIP
# journal articles (Phys. Fluids, J. Appl. Phys., ...) and conference
# proceedings, so it cannot identify a book series and would flag plain journals.
_BOOK_SERIES_DOI_RE = re.compile(
    r"10\.1090/conm|10\.1090/pspum|10\.1007/978|10\.1117/12\.",
    re.I)


def is_book_series_doi(doi):
    """True if a DOI looks like a book-series / proceedings-volume DOI (a chapter),
    not a journal-article DOI."""
    return bool(doi and _BOOK_SERIES_DOI_RE.search(doi))


def is_container_granularity(e):
    """True if the entry is a chapter-in-a-volume that an id will resolve to the
    *container* (book/proceedings volume), not the cited chapter: it carries an
    ISBN or a book-series DOI. Used to down-rank a 0%-overlap title 'mismatch'
    that is really a record-granularity artifact, not a wrong title."""
    return bool(e.get("isbn").strip() or is_book_series_doi(e.get("doi", "")))


# Entry-type predicates, kept together so the type vocabulary lives in one place
# (book resolution by ISBN, article-like locator checks, the verification roll-up).
_BOOK_TYPES = ("book", "inbook", "incollection", "mvbook", "collection")
_ARTICLE_LIKE_TYPES = ("article", "inproceedings", "conference")


def is_book(e):
    """True if the entry is a book-like type (resolvable by ISBN)."""
    return e.etype in _BOOK_TYPES


def is_article_like(e):
    """True if the entry is an article/proceedings type (a DOI is expected)."""
    return e.etype in _ARTICLE_LIKE_TYPES


# A leading doi.org / dx.doi.org URL wrapper around a bare DOI.
_DOI_URL_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.I)
# BibTeX backslash-escapes for characters that are literal in a DOI. A DOI suffix
# never contains a backslash, so a '\_' / '\&' / '\#' etc. is a TeX escape leaking
# from the .bib (e.g. '10.1007/978-3-031-25069-9\_19'); the backslash must be
# dropped before the DOI is resolved or it 404s and is wrongly reported as dead.
_DOI_TEX_ESCAPE = re.compile(r"\\([_&%#$~{}])")


def bare_doi(doi):
    """Strip a leading doi.org/dx.doi.org URL so the bare DOI remains (the entry
    may write 'https://doi.org/10.x/...'), and unescape BibTeX character escapes
    ('\\_' -> '_'); used for resolution, links and the JSON."""
    return _DOI_TEX_ESCAPE.sub(r"\1", _DOI_URL_PREFIX.sub("", doi))


# A DOI embedded in a publisher landing-page URL, e.g.
# 'iopscience.iop.org/article/10.1088/2515-7647/acb57b' or
# 'nature.com/articles/...' (no DOI) vs 'aps.org/.../10.1103/PRXQuantum.5.010328'.
# The suffix runs to the next URL delimiter (?#) or trailing slash/space; a final
# '/' or sentence punctuation is trimmed. Distinct from DOI_RE (which only finds the
# prefix) because here we must also CAPTURE the suffix and stop it at URL syntax.
_DOI_IN_URL_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.I)


def extract_doi_from_url(*values):
    """A DOI mined from a URL/note string (publisher landing pages carry the DOI in
    the path), or ''. Returned only when it is a complete, DOI-shaped value, so a
    fragment never resolves. The FIRST well-formed DOI across the given values wins.
    Used as a last resort when the entry records no 'doi' field: a DOI sitting in
    the url is the canonical identifier and should resolve directly, not via a fuzzy
    title search."""
    for v in values:
        if not v:
            continue
        m = _DOI_IN_URL_RE.search(v)
        if not m:
            continue
        # Trim trailing URL/sentence punctuation that is not part of the DOI.
        cand = bare_doi(m.group(1)).rstrip(").,;'\"/")
        if DOI_FULL_RE.match(cand):
            return cand
    return ""


# An INSPIRE-HEP record id in a 'inspirehep.net/literature/<recid>' URL. That recid
# resolves the full record (incl. document_type) via the INSPIRE API -- the way to
# verify a thesis/proceedings cited by its INSPIRE page alone (no DOI/arXiv id).
_INSPIRE_RECID_RE = re.compile(r"inspirehep\.net/(?:literature|record)/(\d+)", re.I)


def extract_inspire_recid(*values):
    """The INSPIRE-HEP record id from an inspirehep.net URL, or '' -- so an entry
    whose only identifier is its INSPIRE page can still be resolved."""
    for v in values:
        if not v:
            continue
        m = _INSPIRE_RECID_RE.search(v)
        if m:
            return m.group(1)
    return ""
