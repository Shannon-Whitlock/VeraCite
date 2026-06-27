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
    # surname -> the FULL given string ('A. Lecavelier'). Crossref sometimes mis-splits
    # a compound surname, leaving its leading part in `given` (family 'des Etangs',
    # given 'A. Lecavelier' for the surname 'Lecavelier des Etangs'); the full string
    # lets author matching reconstruct the real surname. Empty when not carried.
    given_full: dict = field(default_factory=dict)
    year: object = None                            # int, or None if unknown
    # arXiv-only: the year of the LATEST version (vN), when it differs from `year`
    # (which is v1's year). Lets the comparison treat a bib year that matches any
    # version in [year, updated_year] as a version-pinning note, not a wrong year.
    updated_year: object = None
    volume: str = ""
    number: str = ""
    pages: str = ""
    title: str = ""
    journal: str = ""
    abstract: str = ""
    doi: str = ""                                  # a DOI the record itself carries
    # The work's document type when the source reports it (INSPIRE: 'thesis',
    # 'proceedings', 'article', ...). Lets the entry-type check confirm e.g. a
    # @article that is really a thesis. Empty when the source does not report it.
    document_type: str = ""
    # DataCite-only: the release version a software/dataset record carries
    # (attributes.version, e.g. 'v0.1.2'), so a software entry's `version` field can
    # be checked against the record. Empty when the source does not report it.
    software_version: str = ""
    # arXiv-only: the published version it links to, when one exists.
    published_doi: str = ""
    journal_ref: str = ""
    # arXiv-only: the bare id this record was fetched under (no 'vN' suffix), so the
    # comparison layer can lazily fetch per-version titles to tell an honest
    # "cited an earlier version that was later renamed" from a genuine title mismatch.
    arxiv_id: str = ""
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
