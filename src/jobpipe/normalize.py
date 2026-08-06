"""Normalization and dedupe-key derivation.

Everything here is pure and offline. It is the layer the whole pipeline's
correctness rests on, because a bad dedupe key shows up as either duplicate
pushes (which train you to ignore notifications) or silently swallowed postings
(which is worse - you never learn what you missed).

Where the two failure modes trade off, this module biases toward *collapsing*.
Reposts are common and a missed distinct posting still lands in the digest via
its other fields, whereas a stream of duplicate interrupting pushes destroys the
signal value of the whole system.

The dedupe key is `(company_norm, title_norm, location_norm, term)`. The
source's own posting id is deliberately not part of it: a company that closes a
req and relists it gets a fresh source id for the same job.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from jobpipe.models import Posting, RawPosting, Term, utcnow

# --------------------------------------------------------------------------
# Company
# --------------------------------------------------------------------------

# Trailing legal-entity suffixes. Stripped only from the end, and only as whole
# tokens, so "Scale AI" and "Cohere" keep their real names intact.
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp",
    "corporation", "co", "plc", "gmbh", "ag", "sa", "nv", "bv", "pty",
    "sarl", "oy", "ab", "as", "kk",
}

# Companies that publish under more than one name across sources. Keys are the
# post-normalization form; values are the canonical form.
_COMPANY_ALIASES = {
    "facebook": "meta",
    "meta platforms": "meta",
    "alphabet": "google",
    "google llc": "google",
    "x corp": "x",
    "twitter": "x",
    "square": "block",
    "amazon web services": "amazon",
    "aws": "amazon",
    "amazon com": "amazon",
    "microsoft corporation": "microsoft",
    "linkedin corporation": "linkedin",
    "apple inc": "apple",
    "alphabet google": "google",
    "deepmind": "google deepmind",
    "openai opco": "openai",
    "hewlett packard enterprise": "hpe",
    "international business machines": "ibm",
    "jpmorgan chase": "jpmorgan",
    "jp morgan": "jpmorgan",
    "jpmorganchase": "jpmorgan",
    "goldman sachs group": "goldman sachs",
    "capital one financial": "capital one",
    "walmart global tech": "walmart",
    "nvidia corporation": "nvidia",
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _basic(s: str) -> str:
    """Lowercase, drop accents and punctuation, collapse whitespace."""
    s = _strip_accents(s).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_company(company: str | None) -> str:
    s = _basic(company or "")
    if not s:
        return ""
    if s.startswith("the "):
        s = s[4:]
    tokens = s.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    s = " ".join(tokens)
    return _COMPANY_ALIASES.get(s, s)


# --------------------------------------------------------------------------
# Title
# --------------------------------------------------------------------------

_SEASONS = ("fall", "autumn", "winter", "spring", "summer")

# Season + year in any of the shapes sources actually publish.
_SEASON_YEAR_RE = re.compile(
    r"\b(fall|autumn|winter|spring|summer)\s*(?:of\s*)?"
    r"(?:'|20)?(\d{2})(?:\s*[-/]\s*(?:'|20)?\d{2})?\b"
)
_BARE_YEAR_RE = re.compile(r"\b20(2[4-9]|3[0-9])\b")
_CLASS_OF_RE = re.compile(r"\bclass of\s*(?:'|20)?\d{2}\b")

# Requisition ids: "req-12345", "#4821", "job id 9912", "(R-10422)".
_REQ_ID_RE = re.compile(r"\b(?:req(?:uisition)?|job)?\s*(?:id|#)?\s*[-#]?\s*r?\d{3,}\b")

# Trailing level markers. Roman numerals up to VII, and the ladder shorthands
# ("L4", "E3", "T5", "IC3", "SDE 2", "Level 4", or a bare trailing digit).
_LEVEL_TAIL_RE = re.compile(
    r"\s+(?:"
    r"(?:level|lvl|grade|band)\s*\d{1,2}"
    r"|[lept]\s?\d{1,2}"
    r"|ic\s?\d{1,2}"
    r"|i{1,3}|iv|vi{0,2}"
    r"|\d{1,2}"
    r")$"
)

# Tokens that carry no distinguishing information for a dedupe key. Term-bearing
# words go here because `term` is already a separate key component - leaving
# them in the title would stop "SWE Intern, Fall 2026" from matching the same
# req relisted as plain "SWE Intern".
_NOISE_TOKENS = {
    # term / cohort wording (term is its own key component)
    "new", "grad", "graduate", "graduates", "grads", "university", "college",
    "campus", "student", "students", "entry", "level", "early", "career",
    "careers", "recent", "class", "cohort", "batch", "intake", "cycle",
    "season", "start", "starting", "starts", "date", "dates",
    # employment shape. "full", "part", "time" and "site" are deliberately
    # absent - dropping them as bare tokens mangles real role names
    # ("Full Stack Engineer", "Site Reliability Engineer"). The multi-word
    # forms are removed as phrases by _TITLE_PHRASE_STRIP instead.
    "temporary", "contract", "permanent", "ft", "pt", "hybrid",
    # geography words that duplicate the location component
    "us", "usa", "based", "multiple", "various",
    # generic posting boilerplate
    "position", "positions", "opening", "openings", "role", "roles", "job",
    "jobs", "hiring", "opportunity", "opportunities", "program", "apply",
    "application", "req", "requisition",
    # stopwords
    "and", "or", "the", "a", "an", "for", "of", "to", "in", "at", "with",
    "by", "our", "we", "you", "your",
}

# Multi-word noise removed as whole phrases, before any token-level filtering.
# These must not be handled by dropping their individual words: "site",
# "full" and "time" all appear inside real role names.
_TITLE_PHRASE_STRIP = [
    r"\bfull time\b", r"\bpart time\b", r"\bon site\b", r"\bwork from home\b",
    r"\bunited states\b", r"\bclass of\b",
]

# Vocabulary collapse. Applied token-wise after punctuation stripping, so
# "Co-op" has already become "co op" by the time these run.
_TITLE_PHRASE_MAP = [
    (r"\bco op\b", "intern"),
    (r"\bcoop\b", "intern"),
    (r"\binternship\b", "intern"),
    (r"\binterns\b", "intern"),
    (r"\bsoftware development engineer\b", "software engineer"),
    (r"\bsoftware developer\b", "software engineer"),
    (r"\bsoftware engineering\b", "software engineer"),
    (r"\bsde\b", "software engineer"),
    (r"\bswe\b", "software engineer"),
    (r"\bmle\b", "machine learning engineer"),
    (r"\bml\b", "machine learning"),
    (r"\bnlp\b", "natural language processing"),
    (r"\bdeveloper\b", "engineer"),
    (r"\bdev\b", "engineer"),
    (r"\bengineering\b", "engineer"),
    (r"\bengineer engineer\b", "engineer"),
]

_PARENTHETICAL_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")


def _parenthetical_is_noise(inner: str) -> bool:
    """True when a parenthetical carries nothing that distinguishes the req.

    "(Fall 2026)" and "(Remote, US)" are noise. "(Backend)" and "(Computer
    Vision)" are not - stripping those would collapse genuinely different reqs
    onto one row, which is the one dedupe error that loses postings.
    """
    s = _basic(inner)
    if not s:
        return True
    s = _SEASON_YEAR_RE.sub(" ", s)
    s = _BARE_YEAR_RE.sub(" ", s)
    s = _REQ_ID_RE.sub(" ", s)
    residue = {
        t for t in s.split()
        if t not in _NOISE_TOKENS
        and t not in _SEASONS
        and t not in {"remote", "anywhere", "multiple", "locations", "various", "or", "and"}
    }
    return not residue


def normalize_title(title: str | None) -> str:
    raw = title or ""

    # Drop noise-only parentheticals; unwrap meaningful ones into the title.
    def _paren(m: re.Match[str]) -> str:
        return " " if _parenthetical_is_noise(m.group(1)) else f" {m.group(1)} "

    s = _PARENTHETICAL_RE.sub(_paren, raw)

    s = _basic(s)
    s = _CLASS_OF_RE.sub(" ", s)
    s = _SEASON_YEAR_RE.sub(" ", s)
    s = _BARE_YEAR_RE.sub(" ", s)
    s = _REQ_ID_RE.sub(" ", s)

    for pattern in _TITLE_PHRASE_STRIP:
        s = re.sub(pattern, " ", s)
    for pattern, repl in _TITLE_PHRASE_MAP:
        s = re.sub(pattern, repl, s)

    tokens = [t for t in s.split() if t not in _NOISE_TOKENS and t not in _SEASONS]
    s = " ".join(tokens)

    # Levels sit at the tail; strip repeatedly for "Engineer II L4".
    prev = None
    while prev != s:
        prev = s
        s = _LEVEL_TAIL_RE.sub("", s).strip()

    # Collapse duplicate adjacent tokens introduced by the phrase map
    # ("engineer engineer" from "Software Engineering Engineer").
    out: list[str] = []
    for t in s.split():
        if not out or out[-1] != t:
            out.append(t)
    return " ".join(out)


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

# canonical -> aliases. Matched against the normalized primary location.
_LOCATION_BUCKETS: dict[str, tuple[str, ...]] = {
    "sf-bay": (
        "san francisco", "sf", "south san francisco", "ssf", "bay area",
        "san francisco bay area", "palo alto", "mountain view", "menlo park",
        "sunnyvale", "san jose", "santa clara", "cupertino", "redwood city",
        "foster city", "san mateo", "burlingame", "belmont", "los altos",
        "fremont", "berkeley", "oakland", "emeryville", "milpitas",
        "brisbane", "newark ca", "pleasanton", "walnut creek", "alameda",
    ),
    "seattle": ("seattle", "bellevue", "redmond", "kirkland", "renton", "tacoma", "bothell"),
    "nyc": (
        "new york", "new york city", "nyc", "manhattan", "brooklyn", "queens",
        "jersey city", "hoboken", "newark nj", "long island city",
    ),
    "la": (
        "los angeles", "santa monica", "culver city", "pasadena", "el segundo",
        "venice", "burbank", "playa vista", "glendale", "manhattan beach",
    ),
    "orange-county": (
        "irvine", "costa mesa", "newport beach", "anaheim", "santa ana",
        "orange county", "aliso viejo", "tustin", "huntington beach",
    ),
    "san-diego": ("san diego", "la jolla", "carlsbad", "sorrento valley"),
    "boston": (
        "boston", "cambridge", "somerville", "waltham", "burlington ma",
        "watertown", "needham", "lexington ma",
    ),
    "austin": ("austin", "round rock"),
    "dallas": ("dallas", "plano", "irving", "richardson", "fort worth", "frisco"),
    "houston": ("houston",),
    "chicago": ("chicago", "evanston", "naperville"),
    "denver": ("denver", "boulder", "broomfield", "louisville co"),
    "atlanta": ("atlanta", "alpharetta", "sandy springs"),
    "rtp": ("raleigh", "durham", "chapel hill", "research triangle", "cary", "morrisville"),
    "portland": ("portland", "hillsboro", "beaverton"),
    # Bare "washington" is deliberately absent: it is a state name, and
    # "Seattle, Washington" would otherwise bucket as DC.
    "dc-metro": (
        "washington dc", "washington d c", "arlington", "reston", "mclean",
        "bethesda", "herndon", "alexandria", "chantilly", "tysons", "vienna va",
        "annapolis junction", "columbia md", "fort meade",
    ),
    "pittsburgh": ("pittsburgh",),
    "philadelphia": ("philadelphia", "conshohocken", "malvern"),
    "phoenix": ("phoenix", "tempe", "chandler", "scottsdale", "mesa"),
    "salt-lake": ("salt lake city", "lehi", "draper", "provo", "south jordan"),
    "minneapolis": ("minneapolis", "st paul", "saint paul", "eden prairie"),
    "detroit": ("detroit", "ann arbor", "dearborn"),
    "madison": ("madison",),
    "nashville": ("nashville",),
    "miami": ("miami", "fort lauderdale", "boca raton"),
    "toronto": ("toronto", "waterloo", "mississauga", "ottawa", "kitchener"),
    "vancouver": ("vancouver", "burnaby", "richmond bc"),
    "montreal": ("montreal", "quebec"),
    "london": ("london", "cambridge uk", "oxford"),
    "dublin": ("dublin",),
    "zurich": ("zurich", "zug"),
    "munich": ("munich", "muenchen"),
    "berlin": ("berlin",),
    "paris": ("paris",),
    "amsterdam": ("amsterdam",),
    "tel-aviv": ("tel aviv", "herzliya", "haifa"),
    "bangalore": ("bangalore", "bengaluru"),
    "hyderabad": ("hyderabad",),
    "india-other": ("pune", "gurgaon", "gurugram", "noida", "chennai", "mumbai", "delhi"),
    "tokyo": ("tokyo", "yokohama"),
    "singapore": ("singapore",),
    "sydney": ("sydney", "melbourne"),
    "taipei": ("taipei", "hsinchu"),
    "sao-paulo": ("sao paulo",),
}

# Longest alias first so "south san francisco" wins over "san francisco".
_LOCATION_LOOKUP: list[tuple[str, str]] = sorted(
    ((alias, canon) for canon, aliases in _LOCATION_BUCKETS.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)

_REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh", "distributed", "virtual")

# Placeholders that carry no geography. Bucketing these as their own metro
# would let unrelated reqs from different cities collide on one key.
_UNKNOWN_LOCATIONS = {
    "multiple locations", "various locations", "multiple", "various",
    "several locations", "tbd", "n a", "none", "unspecified", "flexible",
    "worldwide", "global", "unknown",
}

# Non-geographic placeholders seen in live ATS data: "Flexible - Any SpaceX
# Site", "In-Office". They name a working arrangement, not a place, and
# slugifying them invents a metro that unrelated reqs then collide inside.
_PLACEHOLDER_RE = re.compile(
    r"\b(?:flexible|in office|any [a-z]* ?site|any of our|tbd|to be determined"
    r"|unspecified|worldwide|global|various|multiple)\b"
)

# Trailing country names, stripped so "Connecticut, USA" reaches the state
# fallback as "connecticut" instead of slugifying to "ct-usa". Bare "ca" is
# deliberately absent - it is California far more often than Canada.
_COUNTRY_TAIL = {
    "usa": "us", "us": "us", "u s a": "us", "u s": "us",
    "united states": "us", "united states of america": "us",
    "uk": "uk", "united kingdom": "uk", "great britain": "uk", "england": "uk",
    "canada": "canada", "can": "canada",
    "india": "india", "germany": "germany", "france": "france",
    "ireland": "ireland", "netherlands": "netherlands", "japan": "japan",
    "australia": "australia", "singapore": "singapore", "israel": "israel",
    "switzerland": "switzerland", "spain": "spain", "poland": "poland",
    "mexico": "mexico", "brazil": "brazil", "china": "china", "taiwan": "taiwan",
}
_COUNTRY_TAIL_RE = re.compile(
    r"\s*\b(" + "|".join(sorted((re.escape(c) for c in _COUNTRY_TAIL), key=len, reverse=True)) + r")\s*$"
)


def _strip_country(primary: str) -> tuple[str, str | None]:
    """Peel a trailing country name off, returning the rest and its code."""
    match = _COUNTRY_TAIL_RE.search(primary)
    if not match:
        return primary, None
    return primary[: match.start()].strip(), _COUNTRY_TAIL[match.group(1)]

# Two-letter state codes, used to bucket an unrecognized city by state rather
# than letting every small town become its own key.
_STATE_RE = re.compile(r"\b([a-z]{2})\b$")
_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

_MULTI_LOC_SPLIT = re.compile(r"\s*(?:;|\||/| or |,? and |\+)\s*", re.IGNORECASE)

# US states each metro bucket can legitimately sit in. Used to veto a city-name
# match that contradicts an explicit state code: "Brooklyn, OH" is a Cleveland
# suburb, not New York, and "Portland, ME" is not Oregon. Buckets outside the
# US are absent and are never vetoed.
_BUCKET_STATES: dict[str, frozenset[str]] = {
    "sf-bay": frozenset({"ca"}),
    "seattle": frozenset({"wa"}),
    "nyc": frozenset({"ny", "nj"}),
    "la": frozenset({"ca"}),
    "orange-county": frozenset({"ca"}),
    "san-diego": frozenset({"ca"}),
    "boston": frozenset({"ma"}),
    "austin": frozenset({"tx"}),
    "dallas": frozenset({"tx"}),
    "houston": frozenset({"tx"}),
    "chicago": frozenset({"il"}),
    "denver": frozenset({"co"}),
    "atlanta": frozenset({"ga"}),
    "rtp": frozenset({"nc"}),
    "portland": frozenset({"or"}),
    "dc-metro": frozenset({"dc", "va", "md"}),
    "pittsburgh": frozenset({"pa"}),
    "philadelphia": frozenset({"pa"}),
    "phoenix": frozenset({"az"}),
    "salt-lake": frozenset({"ut"}),
    "minneapolis": frozenset({"mn"}),
    "detroit": frozenset({"mi"}),
    "madison": frozenset({"wi"}),
    "nashville": frozenset({"tn"}),
    "miami": frozenset({"fl"}),
}

# Spelled-out states collapse to their code before city matching, so
# "Santa Clara, California" and "Santa Clara, CA" produce the same key and
# "Seattle, Washington" never collides with a DC-metro alias.
_STATE_NAMES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct",
    "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me",
    "maryland": "md", "massachusetts": "ma", "michigan": "mi",
    "minnesota": "mn", "mississippi": "ms", "missouri": "mo",
    "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh",
    "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
    "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington state": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}
# "new york" is both a city and a state; it is resolved by the city table, so
# it must not be rewritten here.
_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(n) for n in _STATE_NAMES), key=len, reverse=True)) + r")\b"
)


def primary_location(location: str | None) -> str:
    """Take the first listed office from a multi-location string.

    Sources disagree about how many offices they list for the same req and in
    what order, so keying on the full set would let a repost that drops one city
    escape dedupe. First-listed is the stable choice in practice: it is the
    primary office, and it survives the other offices changing.
    """
    s = (location or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in _MULTI_LOC_SPLIT.split(s) if p.strip()]
    if not parts:
        return ""
    # "New York, NY" splits on the comma into ["New York", "NY"]; rejoin a bare
    # trailing state/country code onto the city it belongs to.
    first = parts[0]
    if len(parts) > 1 and len(parts[1]) <= 3 and parts[1].isalpha():
        first = f"{first} {parts[1]}"
    return first


def is_remote(location: str | None, remote_hint: bool | None = None) -> bool:
    if remote_hint is not None:
        return remote_hint
    s = _basic(location or "")
    if "hybrid" in s:
        return False
    return any(m in s for m in _REMOTE_MARKERS)


def normalize_location(location: str | None, remote_hint: bool | None = None) -> str:
    """Bucket a location string into a canonical metro, `remote`, or `unknown`.

    A recognizable city wins over a remote marker: "San Francisco (Remote)" and
    "San Francisco, CA" are the same req, and remoteness is already carried
    separately by the `remote` boolean. Only when no city is identifiable does
    the bucket itself become `remote`.
    """
    if not _basic(location or ""):
        return "unknown"

    primary = _basic(primary_location(location))
    if not primary or primary in _UNKNOWN_LOCATIONS:
        return "unknown"

    primary = _STATE_NAME_RE.sub(lambda m: _STATE_NAMES[m.group(1)], primary)
    core, country = _strip_country(primary)
    if not core:
        # The string was nothing but a country ("United States").
        return country or "unknown"

    # An explicit state code vetoes a city match that contradicts it. Without
    # this, every same-named suburb collapses onto the famous city. It is read
    # off the country-stripped form so "Brooklyn, OH, USA" still sees "oh".
    state_match = _STATE_RE.search(core)
    state = state_match.group(1) if state_match and state_match.group(1) in _US_STATES else None

    # Match against the full string before the stripped one: some aliases carry
    # their own country to disambiguate ("cambridge uk" is London, plain
    # "cambridge" is Boston), and stripping first would lose that.
    for candidate in (primary, core):
        for alias, canon in _LOCATION_LOOKUP:
            if not re.search(rf"\b{re.escape(alias)}\b", candidate):
                continue
            allowed = _BUCKET_STATES.get(canon)
            if state and allowed and state not in allowed:
                continue
            return canon

    if state:
        return f"us-{state}"

    if is_remote(location, remote_hint) or any(m in core for m in _REMOTE_MARKERS):
        return "remote"

    # No city, no state, no country: if what is left only describes a working
    # arrangement, it is not a place.
    if _PLACEHOLDER_RE.search(core):
        return "unknown"

    return f"{core.replace(' ', '-')}-{country}" if country else core.replace(" ", "-")


# --------------------------------------------------------------------------
# Term
# --------------------------------------------------------------------------

_SEASON_TO_TERM = {
    ("fall", 2026): Term.FALL_2026,
    ("autumn", 2026): Term.FALL_2026,
    ("winter", 2027): Term.WINTER_2027,
    ("spring", 2027): Term.SPRING_2027,
    ("summer", 2027): Term.SUMMER_2027,
}

_NEW_GRAD_MARKERS = (
    "new grad", "new graduate", "newgrad", "university graduate",
    "university grad", "college graduate", "entry level", "early career",
    "recent graduate", "campus hire", "graduate engineer", "grad engineer",
    "early in career", "early talent",
)


def infer_term(
    title: str | None, description: str | None = None, default: str | None = None
) -> Term:
    """Resolve the hiring cycle, or return UNKNOWN.

    Never guesses. A term guessed wrong poisons the dedupe key, so an unlabeled
    "Software Engineer Intern" stays UNKNOWN rather than becoming a plausible
    season. Explicit season+year beats new-grad wording, and the title beats the
    description (job bodies often mention other programs in passing).

    `default` is a source-level fallback applied only when the text says
    nothing - a feed that only ever carries new-grad roles can declare that,
    but an explicit marker in the posting itself always wins.
    """
    for text in (title or "", description or ""):
        s = _basic(text)
        if not s:
            continue
        for m in _SEASON_YEAR_RE.finditer(s):
            season = m.group(1)
            year = 2000 + int(m.group(2))
            term = _SEASON_TO_TERM.get((season, year))
            if term:
                return term
        if any(marker in s for marker in _NEW_GRAD_MARKERS):
            return Term.NEW_GRAD
        # "Class of 2027" / bare "2027" alongside grad wording.
        if re.search(r"\bclass of (?:'|20)?27\b", s):
            return Term.NEW_GRAD

    if default:
        try:
            return Term(default.strip().lower())
        except ValueError:
            pass
    return Term.UNKNOWN


# --------------------------------------------------------------------------
# Key + id
# --------------------------------------------------------------------------


def make_dedupe_key(company_norm: str, title_norm: str, location_norm: str, term: Term) -> str:
    return "|".join((company_norm, title_norm, location_norm, term.value))


def make_id(dedupe_key: str) -> str:
    return hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16]


def normalize_raw(raw: RawPosting) -> Posting:
    """Turn a source's `RawPosting` into a storable `Posting`."""
    company_norm = normalize_company(raw.company)
    title_norm = normalize_title(raw.title)
    location_norm = normalize_location(raw.location, raw.remote_hint)
    term = infer_term(raw.title, raw.description, raw.term_default)

    key = make_dedupe_key(company_norm, title_norm, location_norm, term)
    now = utcnow()

    return Posting(
        id=make_id(key),
        dedupe_key=key,
        company=(raw.company or "").strip(),
        title=(raw.title or "").strip(),
        term=term,
        location=(raw.location or "").strip() or "Unknown",
        remote=is_remote(raw.location, raw.remote_hint),
        apply_url=(raw.apply_url or "").strip(),
        source_url=(raw.apply_url or "").strip() or None,
        source=raw.source,
        first_seen_at=now,
        last_seen_at=now,
        posted_at=raw.posted_at,
        company_norm=company_norm,
        title_norm=title_norm,
        location_norm=location_norm,
        source_id=raw.source_id,
    )
