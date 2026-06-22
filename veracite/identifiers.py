"""Check-digit validators for bibliographic identifiers (ISBN/ISSN/ORCID).

Pure functions, so they are easy to unit-test in isolation. A failing check digit
means the identifier was mistranscribed and will not resolve.
"""

import re


def isbn_valid(s):
    """Validate an ISBN-10 or ISBN-13 by its check digit (hyphens/spaces ignored)."""
    d = re.sub(r"[\s-]", "", s).upper()
    if len(d) == 10 and re.match(r"^\d{9}[\dX]$", d):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
        return total % 11 == 0
    if len(d) == 13 and d.isdigit():
        total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(d))
        return total % 10 == 0
    return False


def issn_valid(s):
    """Validate an ISSN (NNNN-NNNC) by its mod-11 check digit."""
    d = re.sub(r"[\s-]", "", s).upper()
    if len(d) != 8 or not re.match(r"^\d{7}[\dX]$", d):
        return False
    total = sum((8 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
    return total % 11 == 0


def orcid_valid(s):
    """Validate an ORCID (dddd-dddd-dddd-dddC) by its ISO-7064 mod-11-2 check digit."""
    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", s.upper())
    if not m:
        return False
    d = m.group(1).replace("-", "")
    total = 0
    for c in d[:-1]:
        total = (total + int(c)) * 2
    check = (12 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return d[-1] == expected
