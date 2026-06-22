"""VeraCite -- a bibliography health checker for LaTeX projects.

Public API: parse a .bib, run the checks, render a report. See cli.main for the
command-line entry point.
"""

from .config import VERSION, load_settings
from .parser import parse_bib
from .report import Finding, Report, Severity
from .rules import run_static, syntax_pass

__version__ = VERSION
__all__ = [
    "parse_bib", "run_static", "syntax_pass",
    "Report", "Finding", "Severity", "load_settings", "__version__",
]
