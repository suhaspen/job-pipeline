# Sources

One module per source, each implementing `fetch() -> list[RawPosting]`. A
failure in one source is caught, logged, reported and stepped over — it never
aborts a run.

**Status: Tier A built and live (Phase 1). Tier B not started (Phase 5).**

Everything below marked *(verified)* was confirmed against the live endpoint on
2026-08-05, not assumed.

## Tier A — structured

### `simplify-newgrad` — SimplifyJobs/New-Grad-Positions

*(verified)* The brief said to discover the path rather than assume it:

```
GET https://api.github.com/repos/SimplifyJobs/New-Grad-Positions
    -> default_branch = "dev"        # brief was right: not main
GET .../contents/.github/scripts?ref=dev
    -> listings.json  (alongside update_readmes.py, util.py)
```

Fetched from
`raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json`.

| | |
|---|---|
| Payload | ~12 MB, 18,216 records |
| Live after `active AND is_visible` | ~2,600 |
| After category + prefilter | ~1,270 |
| ETag | supported, returns 304 *(verified)* |

Record shape:

```json
{"company_name": "", "title": "", "url": "", "locations": ["San Francisco, CA"],
 "date_posted": 1767841111, "date_updated": 0, "active": true, "is_visible": true,
 "category": "Software", "sponsorship": "Other", "degrees": ["Bachelor's"], "id": ""}
```

Notes:
- `date_posted` is real epoch seconds, so `posted_at` is never guessed here.
- `category` values seen: Software, AI/ML/Data, Hardware, Quant, Product,
  Software Engineering, "Data Science, AI & Machine Learning", Product
  Management. Hardware and Product are dropped as off-target.
- `sponsorship` is mostly the useless `"Other"`, but `"U.S. Citizenship is
  Required"` and `"Does Not Offer Sponsorship"` appear and map straight onto
  disqualifiers. `degrees` containing only `PhD` maps to `phd-required`. Both
  are carried through on `RawPosting.raw` for Phase 3.
- The repo is new-grad by construction, so the module passes
  `term_default="new-grad"` — but a title naming a season still wins.

The ETag is not an optimization. Unconditional fetching on a `*/30` cron would
be ~576 MB/day.

### `speedyapply-swe` / `speedyapply-ai`

*(verified)* Two findings that differ from what the brief anticipated:

**1. There is no `INTERN_USA.md`.** Both repos contain exactly:

```
INTERN_INTL.md  NEW_GRAD_INTL.md  NEW_GRAD_USA.md  README.md
```

The USA *internship* tables live inside `README.md`. That is where this module
reads them from. The brief's "if present" hedge was the right call.

**2. The column layout is not uniform.** FAANG+ and Quant carry a `Salary`
column that the `Other` section omits:

```
| Company | Position | Location | Salary | Posting | Age |    <- FAANG+, Quant
| Company | Position | Location | Posting | Age |             <- Other
```

So each table's header row is parsed and columns are addressed by name.
Reading by fixed index would shift apply URLs into the age column for the whole
`Other` section — which is the largest one.

Cell formats:

| Column | Shape |
|---|---|
| Company | `<a href="..."><strong>Name</strong></a>` |
| Position | plain text |
| Location | `City, ST` or `City, ST +10` (the `+N` means "and N more") |
| Posting | `<a href="APPLY_URL"><img alt="Apply"/></a>` |
| Age | `1d`, `12d`, `2mo`, `3h` |

The `+N` suffix is stripped before normalization; left in place it defeats
location bucketing entirely (`California, USA +10` -> `ca-usa-10`).

`Age` is converted to `posted_at` as `now - age`. This is the source's own
stated age rather than an inference, but it is only day-granular, so treat it as
approximate to within a day.

International files are not read: the profile targets US roles, and `INTL`
coverage would roughly triple volume for postings that fail triage anyway.

### `ats` — Greenhouse / Lever / Ashby

*(verified)* All unauthenticated. Tokens from [`companies.json`](../companies.json).

| ATS | Endpoint | Envelope |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `{"jobs": [...]}` |
| Lever | `api.lever.co/v0/postings/{token}?mode=json` | bare array |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{token}` | `{"jobs": [...]}` |

Field mapping:

| | Greenhouse | Lever | Ashby |
|---|---|---|---|
| title | `title` | `text` | `title` |
| url | `absolute_url` | `hostedUrl` | `jobUrl` |
| location | `location.name` | `categories.location` | `location` |
| posted | `first_published` | `createdAt` (epoch **ms**) | `publishedAt` |
| body | `content` (2x-escaped HTML) | `descriptionPlain` | `descriptionPlain` |
| remote | — | `workplaceType` | `isRemote` |
| hidden | — | — | `isListed: false` |

Gotchas found the hard way:
- Greenhouse `content` is **double-escaped** HTML (`&lt;div&gt;`), so it needs
  unescaping twice before tag-stripping.
- Greenhouse `updated_at` moves on every edit; `first_published` is used for
  `posted_at` so an edited old req does not look freshly posted.
- Lever timestamps are epoch **milliseconds** while Simplify uses seconds. The
  shared parser discriminates by magnitude.
- Lever returns a bare array, unlike the other two.

**Token verification.** Of 89 seeded tokens, 54 worked as guessed, 14 more were
recovered by probing vendor and slug variants, and 18 were removed as dead. Run
`jobpipe verify-companies` to re-check; `--write` updates the file.

Companies that had **moved ATS vendor** since their conventional slug: Cohere,
Snowflake, Notion and Confluent (Greenhouse/Lever -> Ashby), Zoox and
Wealthfront (Greenhouse -> Lever), Together AI and Nuro (Ashby/Lever ->
Greenhouse). Renamed slugs: DoorDash is `doordashusa`, Cursor is `cursor` not
`anysphere`, Anduril is `andurilindustries`, Chroma is `trychroma`.

Raw ATS boards run the **strict** prefilter: they list every open req a company
has, at every level. See `triage/prefilter.py`.

## Tier B — aggregators, best-effort

### `newgrad-jobs.com` — NOT STARTED (Phase 5)

Build order is fixed and non-negotiable:

1. **Fetch and read `robots.txt` first.** If listing paths are disallowed: stop,
   do not build the source, record it in `ASSUMPTIONS.md` as blocked-by-robots.
   No workaround.
2. If allowed, find the JSON endpoint the page calls rather than parsing DOM.
   Document the endpoint and response shape *here*.
3. If no JSON endpoint is findable, **do not fall back to a headless browser.**
   Ship a stub returning `[]`, log `NEEDS_MANUAL_SETUP` in the run report, and
   write up findings in `ASSUMPTIONS.md` for a decision between DOM parsing
   and their email-alert channel.
4. Rate limit to one request per source per hour, with a real `User-Agent`.

**Aggregators are primarily a company-discovery channel.** Any company in
aggregator results but absent from `companies.json` goes to
`data/candidate-companies.csv` for review. That inversion is intentional.

## Not sources

No LinkedIn scraping. No headless browsers against any site. No CAPTCHA
handling. No auto-submitting applications.

## Adding a source

1. Implement `fetch() -> list[RawPosting]` and expose a `stats: FetchStats`.
2. Set `strict_prefilter = True` if the feed is an unfiltered job board.
3. Never raise for an expected condition — record it in `stats.errors` and
   return what you have.
4. Return `[]` and set `stats.not_modified` on a 304.
5. Capture a trimmed real payload into `tests/fixtures/` and add a test.
6. Register it in `sources/build_sources`.
