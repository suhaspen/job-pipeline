"""speedyapply/2027-SWE-College-Jobs and 2027-AI-College-Jobs.

Raw markdown from `raw.githubusercontent.com`, not rendered HTML.

Two things about these repos were established by inspection rather than
assumption, and both differ from what the brief anticipated:

1. **There is no `INTERN_USA.md`.** Each repo contains `INTERN_INTL.md`,
   `NEW_GRAD_INTL.md`, `NEW_GRAD_USA.md` and `README.md`. The USA *internship*
   tables live inside `README.md`, so that is where this module reads them
   from. The brief's "if present" hedge was the right call.

2. **The column layout is not uniform across the file.** The FAANG+ and Quant
   sections carry a `Salary` column that the `Other` section omits:

       | Company | Position | Location | Salary | Posting | Age |
       | Company | Position | Location | Posting | Age |

   So the header row of each table is parsed and columns are addressed by
   name. Reading by fixed index would silently shift apply URLs into the age
   column for every posting in the largest section.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Iterator

from jobpipe.models import RawPosting, utcnow
from jobpipe.sources.base import FetchStats, HttpClient

RAW_BASE = "https://raw.githubusercontent.com/speedyapply/{repo}/main/{path}"

# (path, source-level default term). README.md holds the USA internship tables,
# whose titles carry their own season markers, so it declares no default.
FILES: tuple[tuple[str, str | None], ...] = (
    ("NEW_GRAD_USA.md", "new-grad"),
    ("README.md", None),
)

_HREF_RE = re.compile(r'href="([^"]+)"')
_STRONG_RE = re.compile(r"<strong>(.*?)</strong>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# Trailing "+10" means "and 10 more locations". Left in place it defeats
# location bucketing entirely ("California, USA +10" -> "ca-usa-10").
_PLUS_N_RE = re.compile(r"\s*\+\s*\d+\s*$")
_AGE_RE = re.compile(r"^\s*(\d+)\s*(h|d|w|mo|yr)\s*$", re.IGNORECASE)

_AGE_UNITS = {
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
    "mo": timedelta(days=30),
    "yr": timedelta(days=365),
}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def iter_tables(markdown: str) -> Iterator[tuple[list[str], list[list[str]]]]:
    """Yield `(header, rows)` for every pipe table in the document.

    A header is recognized by its own content (a `Company` and a `Position`
    column) rather than by position in the file, so new sections or reordered
    tables do not need a code change.
    """
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|"):
            header = [c.lower() for c in _cells(line)]
            if "company" in header and "position" in header:
                # Next line must be the |---|---| delimiter.
                if i + 1 < len(lines) and set(lines[i + 1].replace("|", "").strip()) <= set("-: "):
                    rows: list[list[str]] = []
                    j = i + 2
                    while j < len(lines) and lines[j].startswith("|"):
                        rows.append(_cells(lines[j]))
                        j += 1
                    yield header, rows
                    i = j
                    continue
        i += 1


def _text(cell: str) -> str:
    m = _STRONG_RE.search(cell)
    inner = m.group(1) if m else cell
    return _TAG_RE.sub("", inner).replace("&amp;", "&").strip()


def _apply_url(cell: str) -> str:
    m = _HREF_RE.search(cell)
    return m.group(1).strip() if m else ""


def _posted_at(age_cell: str):
    """Convert the repo's relative age column to an absolute timestamp.

    This is the source's own stated age, not an inference - but it is only
    day-granular, so the result is approximate to within a day. That is
    accurate enough for the "am I an early applicant" decision and far better
    than discarding the only recency signal these tables carry.
    """
    m = _AGE_RE.match(_TAG_RE.sub("", age_cell))
    if not m:
        return None
    return utcnow() - int(m.group(1)) * _AGE_UNITS[m.group(2).lower()]


class SpeedyApplySource:
    def __init__(self, http: HttpClient, repo: str, name: str):
        self.http = http
        self.repo = repo
        self.name = name
        self.stats = FetchStats()
        self.raw_payload: dict[str, Any] = {}

    def fetch(self) -> list[RawPosting]:
        out: list[RawPosting] = []
        unchanged = 0

        for path, term_default in FILES:
            url = RAW_BASE.format(repo=self.repo, path=path)
            try:
                text = self.http.get_text(url, conditional=True)
            except Exception as exc:  # one missing file must not lose the other
                self.stats.errors.append(f"{path}: {exc}")
                continue

            if text is None:
                unchanged += 1
                continue

            self.raw_payload[path] = text
            before = len(out)
            for header, rows in iter_tables(text):
                out.extend(self._parse_table(header, rows, path, term_default))
            if len(out) == before:
                self.stats.warnings.append(
                    f"{path}: parsed 0 rows - table format may have changed"
                )

        # Every file 304'd: nothing to do, and not an error.
        if unchanged == len(FILES):
            self.stats.not_modified = True

        self.stats.fetched = len(out)
        return out

    def _parse_table(
        self, header: list[str], rows: list[list[str]], path: str, term_default: str | None
    ) -> list[RawPosting]:
        try:
            i_company = header.index("company")
            i_title = header.index("position")
        except ValueError:
            self.stats.warnings.append(f"{path}: table missing company/position column")
            return []
        i_loc = header.index("location") if "location" in header else None
        i_url = header.index("posting") if "posting" in header else None
        i_age = header.index("age") if "age" in header else None

        out: list[RawPosting] = []
        for cells in rows:
            if len(cells) < len(header):
                continue
            company = _text(cells[i_company])
            title = _text(cells[i_title])
            if not company or not title:
                continue

            location = None
            if i_loc is not None:
                location = _PLUS_N_RE.sub("", _text(cells[i_loc])) or None

            apply_url = _apply_url(cells[i_url]) if i_url is not None else ""
            if not apply_url:
                # Without a URL the row is not actionable from a notification.
                continue

            out.append(
                RawPosting(
                    source=self.name,
                    company=company,
                    title=title,
                    apply_url=apply_url,
                    location=location,
                    term_default=term_default,
                    posted_at=_posted_at(cells[i_age]) if i_age is not None else None,
                    raw={"file": path, "salary": _text(cells[header.index("salary")])
                         if "salary" in header and len(cells) > header.index("salary") else None},
                )
            )
        return out


def swe_source(http: HttpClient) -> SpeedyApplySource:
    return SpeedyApplySource(http, "2027-SWE-College-Jobs", "speedyapply-swe")


def ai_source(http: HttpClient) -> SpeedyApplySource:
    return SpeedyApplySource(http, "2027-AI-College-Jobs", "speedyapply-ai")
