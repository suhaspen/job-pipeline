# Sources

One module per source, each implementing `fetch() -> list[RawPosting]`. A
failure in one source is caught, logged, reported and stepped over — it never
aborts a run.

**Status: none built. Phase 1.** This file is the contract they will be built
against, plus the place where reverse-engineered endpoints get documented as
required by the brief.

## Tier A — structured

### `SimplifyJobs/New-Grad-Positions`
- Machine-readable `listings.json` maintained by CI. **Exact path to be
  discovered from `.github/scripts/`, not assumed.**
- Active branch is `dev`, not `main`.
- Parse the JSON. Do not scrape the README.
- Conditional request with `If-None-Match`; **304 is a successful no-op, not an
  error**, and must be reported as `ok: true`.

### `speedyapply/2027-SWE-College-Jobs`
- Raw markdown from `raw.githubusercontent.com`, not rendered HTML.
- Targets `NEW_GRAD_USA.md` and `INTERN_USA.md` if present.
- Tables have a stable column layout — parse column-wise. Do not regex the file
  as a blob.

### `speedyapply/2027-AI-College-Jobs`
- Same treatment. Directly on-target for AI/ML roles.

### ATS public APIs
All unauthenticated. Tokens from [`companies.json`](../companies.json).

| ATS | Endpoint |
|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Lever | `https://api.lever.co/v0/postings/{company}?mode=json` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{company}` |

Use `updated_at` for change detection. Greenhouse and Ashby job bodies often
name the recruiter outright — that is the first place the recruiter feature
looks, before any search API.

## Tier B — aggregators, best-effort

### `newgrad-jobs.com`
Client-side rendered. The build order is fixed and non-negotiable:

1. **Fetch and read `robots.txt` first.** If the listing paths are disallowed:
   stop, do not build the source, record it in `ASSUMPTIONS.md` as
   blocked-by-robots. No workaround.
2. If allowed, find the JSON endpoint the page calls rather than parsing DOM.
   Document the endpoint and its response shape *here*.
3. If no JSON endpoint is findable, **do not fall back to a headless browser.**
   Ship the module as a stub returning `[]`, log `NEEDS_MANUAL_SETUP` in the run
   report, and write up what was found in `FEEDBACK.md` for a decision between
   DOM parsing and their email-alert channel.
4. Rate limit: one request per source per hour, with a real `User-Agent`
   identifying the project (`jobpipe/0.1 ...`, set in `config.py`).

**Aggregators are primarily a company-discovery channel.** They overlap heavily
with the ATS feeds. Any company appearing in aggregator results but absent from
`companies.json` is written to `data/candidate-companies.csv` for manual review.
That inversion is intentional.

## Not sources

No LinkedIn scraping. No headless browsers against any site. No CAPTCHA
handling. No auto-submitting applications. If a task appears to require any of
these, stop and ask.

## Reverse-engineered endpoints

*(Nothing yet. Anything discovered by inspection rather than documentation gets
recorded here with the evidence, per the brief's source-health requirement.)*
