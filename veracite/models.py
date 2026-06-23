"""The normalized bibliographic record -- the one shape every source resolves to.

Crossref, arXiv, INSPIRE and the ISBN lookups each return a `Record` so the
comparison layer has a single, documented contract to read instead of a bag of
dicts whose keys vary by source. `Record` still supports `.get()` and item access
(`rec["abstract"] = ...`) so the existing consumers and the test fixtures that
build record-shaped dicts keep working unchanged.

Note: the OpenAlex lookup is a *status* payload (retraction + abstract), not a
bibliographic record, so it stays a plain dict -- it is a different contract.
"""

from dataclasses import dataclass, field


@dataclass
class Record:
    """A bibliographic record normalized across sources. Every field defaults to
    empty so a source that does not carry one simply leaves it blank; this is the
    contract the comparison/verification layers read."""
    authors: list = field(default_factory=list)   # folded surname keys, in order
    # The authors' original, human-readable surnames (same order as `authors`), kept
    # only for display in a finding message -- matching uses the folded `authors`.
    # A source may leave this empty, in which case the folded key is shown instead.
    authors_display: list = field(default_factory=list)
    given: dict = field(default_factory=dict)      # surname -> first given token
    year: object = None                            # int, or None if unknown
    volume: str = ""
    number: str = ""
    pages: str = ""
    title: str = ""
    journal: str = ""
    abstract: str = ""
    doi: str = ""                                  # a DOI the record itself carries
    # arXiv-only: the published version it links to, when one exists.
    published_doi: str = ""
    journal_ref: str = ""
    # Crossref-only: related-work links carried in the SAME work response
    # (relation / updated-by), so the related-works check needs no second fetch.
    # A list of (relationship_label, target_doi) pairs.
    relations: list = field(default_factory=list)

    def get(self, name, default=""):
        """Dict-style read, so consumers can stay agnostic of dict vs Record."""
        return getattr(self, name, default)

    def __getitem__(self, name):
        return getattr(self, name)

    def __setitem__(self, name, value):
        setattr(self, name, value)
