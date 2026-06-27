"""VeraCite -- a deterministic verifier for BibTeX/biblatex bibliographies.

Public API: parse a .bib, run the checks, render a report. See cli.main for the
command-line entry point.
"""

from .config import VERSION, load_settings
from .parser import parse_bib
from .report import Finding, Report, Severity
from .rules import run_static, syntax_pass
from .webcheck import check_bib_text

__version__ = VERSION
__all__ = [
    "parse_bib", "run_static", "syntax_pass", "check_bib_text",
    "Report", "Finding", "Severity", "load_settings", "__version__",
]
