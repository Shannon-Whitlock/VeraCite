"""Title normalization and comparison -- the single source of truth.

Both the record-comparison layer (is the bib's title the record's title?) and the
DOI search (does this Crossref hit match the entry?) need to decide whether two
titles denote the same work up to stylistic variation. They share one normalizer
here so the two never drift apart: Greek letters are spelled out, TeX/HTML/math is
stripped, accents are folded, '&' becomes 'and', and the result is lowercase
alphanumeric words. Comparisons (exact key, dropped-subtitle prefix, word-token
overlap) are built on that one normal form.
"""

import re

from .normalize import clean_tex, deaccent, strip_math

# Greek letters (TeX macro, Unicode lower/upper) -> their spelled-out names, so a
# title comparison sees the same token whether the bib wrote '$\alpha$', 'α' or
# 'alpha'. Applied before math/markup is stripped. BOTH TeX macro cases are matched:
# the lowercase letter macro ('\delta' = δ) and the capitalized macro ('\Delta' = Δ,
# a DISTINCT TeX macro for the uppercase letter) -- without the capitalized form, a
# title's '$\Delta$' was stripped as unmatched math, losing the word and producing a
# false 'title differs' against a record that carried the Unicode 'Δ' (the atz2022
# '$\Delta$' vs 'Δ' near-FP). Both fold to the same spelled-out token.
_GREEK = {
    "alpha": "α Α", "beta": "β Β", "gamma": "γ Γ", "delta": "δ Δ",
    "epsilon": "ε Ε", "zeta": "ζ Ζ", "eta": "η Η", "theta": "θ Θ",
    "iota": "ι Ι", "kappa": "κ Κ", "lambda": "λ Λ", "mu": "μ Μ", "nu": "ν Ν",
    "xi": "ξ Ξ", "pi": "π Π", "rho": "ρ Ρ", "sigma": "σ Σ", "tau": "τ Τ",
    "phi": "φ Φ", "chi": "χ Χ", "psi": "ψ Ψ", "omega": "ω Ω",
}
_GREEK_SUB = [(re.compile(rf"\\{name}\b|\\{name.capitalize()}\b|[{chars.replace(' ', '')}]"),
               f" {name} ")
              for name, chars in _GREEK.items()]


def title_key(t):
    """A title reduced to comparable lowercase words: spell out Greek letters,
    de-TeX, drop math/markup, deaccent, normalize '&'->'and', keep alphanumerics.
    So 'Schr{\\"o}dinger'=='Schrodinger' and '$\\alpha$-decay'=='alpha decay'."""
    for rx, name in _GREEK_SUB:
        t = rx.sub(name, t)
    t = strip_math(clean_tex(t))           # clean_tex decodes entities + accents
    t = deaccent(t).lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def title_words(t):
    """The set of word tokens of a normalized title (for overlap measures)."""
    return set(title_key(t).split())


def title_overlap(a, b):
    """Jaccard overlap (0-1) of two titles' word-token sets, on the shared normal
    form. 1.0 == identical token sets; 0.0 == disjoint."""
    wa, wb = title_words(a), title_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def title_is_shortened(a, b):
    """True if one normalized title is a clean leading prefix of the other on a
    word boundary -- a dropped subtitle (e.g. 'Combinatorial Optimization' vs
    'Combinatorial Optimization: Theory and Algorithms'), not a different paper.
    Compares whole words, so this never fires on a mere character prefix."""
    aw, bw = title_key(a).split(), title_key(b).split()
    if not aw or not bw or aw == bw:
        return False
    short, long = (aw, bw) if len(aw) <= len(bw) else (bw, aw)
    return long[:len(short)] == short


def title_is_fragment(a, b):
    """True if all words of the shorter title appear as a contiguous subsequence
    inside the longer one. This is the case where a bib author truncated/paraphrased
    a title by quoting only the descriptive tail (e.g. bib has 'Spectroscopy of
    Single Trapped Molecules' while the full title is 'Quantum-nondemolition state
    detection and spectroscopy of single trapped molecules'). Prefix/suffix are
    already handled by title_is_shortened; this catches interior fragments.
    Requires the shorter side to be at least 4 words to avoid short-phrase
    false positives."""
    aw, bw = title_key(a).split(), title_key(b).split()
    if not aw or not bw or aw == bw:
        return False
    short, long = (aw, bw) if len(aw) <= len(bw) else (bw, aw)
    if len(short) < 4:
        return False
    n = len(short)
    return any(long[i:i + n] == short for i in range(len(long) - n + 1))


def title_similar(a, b, threshold=0.90):
    """Whether two titles are the same work up to style: equal after
    normalization, one a clean prefix (dropped subtitle) of the other, or word-token
    overlap >= `threshold`. Robust to accents/math/punctuation/casing without
    matching genuinely different titles."""
    ka, kb = title_key(a).replace(" ", ""), title_key(b).replace(" ", "")
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if title_is_shortened(a, b):           # dropped subtitle
        return True
    return title_overlap(a, b) >= threshold
