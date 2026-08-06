# FEEDBACK — Phase 1

Tier A sources, end to end, writing to SQLite. No notifications yet.

**276 tests passing, 0.73s, no network in tests.** Live run: **2,514 postings
from 4 sources in 23s**, 2.0 MB database.

```
source               ok    fetched    new  filtered      ms
simplify-newgrad     ok       1268   1160       626     355
speedyapply-swe      ok        767    645        10      45
speedyapply-ai       ok        491    427       100      46
ats                  ok        339    282     15629   20221
total                         2865   2514     16365
deduped out     351
```

Re-running immediately: **6.2s, every GitHub source 304, zero new.** The
pipeline is idempotent and cheap when nothing has changed.

---

## Built

- **`simplify-newgrad`** — path discovered, not assumed: `.github/scripts/
  listings.json` on branch `dev`. 18,216 records → 2,576 live → 1,268 after
  category and prefilter. ETag conditional; 304 verified.
- **`speedyapply-swe` / `speedyapply-ai`** — raw markdown, header-driven table
  parsing, `+N` location suffix stripped, `Age` → `posted_at`.
- **`ats`** — Greenhouse, Lever and Ashby across 71 boards. Per-vendor parsers,
  per-board failure isolation, 0.15s politeness gap.
- **`jobpipe verify-companies [--write]`** — probes every board, reports live /
  empty / dead, and rewrites `companies.json`. This is what turned the seeded
  guesses into a checked list.
- **Prefilter** (`triage/prefilter.py`) — deterministic eligibility gate with
  lenient and strict modes. Not in the brief; see Deviations.
- **Runner** — per-source timing, failure isolation, `data/run-report.json` in
  exactly the brief's shape, zero-yield alarm, JSON-lines logs.
- **`--dry-run`** — full pipeline, no writes, no ETag updates, and faithful
  cross-source dedupe counts.
- **Conditional-request cache** — ETags persisted in the database, because every
  Actions run is a fresh container.
- **Fixture-based tests for every source** from captured real payloads.

## Not built

- **Notifications (Phase 2), triage scoring (Phase 3), Actions workflow
  (Phase 4), Tier B aggregators / recruiter / Sheets (Phase 5).** Untouched.
- **`--replay`** — storage is built and tested (compressed, last 3 runs), but
  the flag exits 2. It replays *triage*, which does not exist yet.
- **`make eval`** — still exits 2. One run of history and no scores.
- **`profile/*.md` are still templates.** Nothing reads them until Phase 3.
- **International speedyapply files** — deliberately skipped (B5).

## Assumptions

Full list in [ASSUMPTIONS.md](ASSUMPTIONS.md) (B1–B10 this phase). The ones
that would change the design:

| | Assumption | Would have asked |
|---|---|---|
| **B1** | A prefilter runs before storage | Is 19k rows/run in a git-committed DB acceptable? |
| **B2** | ATS boards need an early-career signal; curated feeds do not | Trade recall on unlabeled ATS reqs for an 8x smaller DB? |
| **B4** | speedyapply's `Age` column becomes `posted_at` | Does a stated relative age count as "provided"? |
| **B6** | Dead tokens deleted, not disabled | Keep them for manual repair instead? |

## Source health

| Source | Reachable | Schema as expected | Rows | Notes |
|---|---|---|---|---|
| `simplify-newgrad` | yes | yes | 1,268 | 12 MB payload; ETag 304 verified |
| `speedyapply-swe` | yes | **no** | 767 | No `INTERN_USA.md`; non-uniform columns |
| `speedyapply-ai` | yes | **no** | 491 | Same |
| `ats` | 68/89 | yes | 339 | 18 tokens dead and removed |
| `newgrad-jobs.com` | **not attempted** | — | — | Phase 5; robots.txt still unread |

**Reverse-engineered, with evidence:**

1. **Simplify path and branch.** `GET api.github.com/repos/SimplifyJobs/
   New-Grad-Positions` → `default_branch: "dev"`. `GET .../contents/
   .github/scripts?ref=dev` → `listings.json` alongside `update_readmes.py`.
   The brief was right that it is not `main`.
2. **`INTERN_USA.md` does not exist.** Both repos contain exactly
   `INTERN_INTL.md`, `NEW_GRAD_INTL.md`, `NEW_GRAD_USA.md`, `README.md`. The USA
   internship tables are inside `README.md`. Your "if present" hedge was right.
3. **speedyapply column layout is not uniform.** FAANG+/Quant carry a `Salary`
   column the `Other` section omits. Parsing by fixed index would have shifted
   apply URLs into the age column for the largest section. Columns are addressed
   by header name.
4. **Greenhouse `content` is double-escaped HTML** (`&lt;div&gt;`), so it needs
   unescaping twice. **Greenhouse `updated_at` moves on every edit** — I use
   `first_published` so an edited old req does not look freshly posted.
   **Lever sends epoch milliseconds** where Simplify sends seconds; the parser
   discriminates by magnitude. **Lever returns a bare array**, not `{"jobs":[]}`.
5. **35 of 89 seeded tokens were wrong.** 14 recovered by probing variants:
   Cohere, Snowflake, Notion and Confluent had **moved to Ashby**; Zoox and
   Wealthfront to Lever; Together AI and Nuro to Greenhouse. DoorDash is
   `doordashusa`; Cursor is `cursor`, not `anysphere`. 18 stayed dead and were
   removed — including Netflix, HashiCorp, Two Sigma, Hugging Face, Weights &
   Biases, Grammarly, Atlassian, Unity. Full list in `companies.json`'s header.

**Four normalization bugs found by running against live data**, each now with a
regression test:

- `Brooklyn, OH` → `nyc`. Brooklyn, Ohio is a Cleveland suburb. An explicit
  state code now vetoes a contradicting city match, which also fixes
  `Portland, ME` and `Arlington, TX`.
- `Connecticut, USA` → `ct-usa`. A trailing country name blocked the state
  fallback. Now `us-ct`.
- `Washington, D.C.` → `washington-d-c`, its own phantom metro.
- `Flexible - Any SpaceX Site` and `In-Office` became metros. They name a
  working arrangement, not a place; now `unknown`.

**A dry-run fidelity bug:** the seen-set was scoped per source, so cross-source
dedupe was invisible and `--dry-run` overstated `new` by ~35%. Now run-scoped,
with a test asserting dry and real runs agree.

**Cross-source overlap is only 351/2,865 (12%)** — lower than I expected, so I
checked rather than assumed. It is legitimate: same company and title in
*different metros* is two applications, two rows. Of 86 shared
(company, title) pairs between Simplify and speedyapply, 23 differ only by
location and all 23 are genuinely different cities.

## Deviations from this brief

1. **A prefilter runs before storage** (B1, B2). The largest deviation. Without
   it the run stores 19,230 postings, of which the ATS portion is 95% sales,
   recruiting and senior roles. I sampled 341 strict-mode drops across ten
   target boards and found no genuine new-grad reqs.
2. **`Age` → `posted_at`** for speedyapply (B4), against a strict reading of
   "do not guess". A stated relative age is provided data, day-granular.
3. **Replay stores prefiltered postings, not raw bytes** (B7), zlib-compressed,
   last 3 runs, with `VACUUM`. Raw storage was 10.4 MB *per run* in a database
   committed every 30 minutes.
4. **`term_hint` became `term_default` with inverted precedence.** A source-level
   default now applies only when the posting's own text is silent. Simplify is a
   new-grad repo, but a title there reading "Fall 2026" must still mean
   fall-2026 or it splits from the same req seen elsewhere.
5. **`SqliteStore` gained `http_cache`, `prune_raw` and `vacuum`;** schema
   version 2.
6. **International speedyapply files skipped** (B5).

## Open questions for review

1. **Work authorization?** Still the largest scoring lever, still blank. Now
   concrete: the Simplify feed carries a `sponsorship` field and I am storing
   it, so `citizenship` and `no-sponsorship` disqualifiers are ready to fire in
   Phase 3 the moment you tell me which way. Related: 2,164 Anduril and 2,126
   SpaceX postings are in reach, nearly all US-person-only.
2. **Is the strict ATS prefilter the right trade?** It cut ATS from 3,689 rows
   to 339. My audit found no false negatives, but this is the rule most likely
   to silently cost you a posting, and it is the one I would most like a second
   opinion on.
3. **`term: unknown` is 463 of 2,514 rows (18%).** Notify or digest-only? These
   are mostly ATS titles with no season marker. Unchanged from Phase 0 but now
   with a real number attached.
4. **2.0 MB database, committed every 30 minutes.** Postings churn slowly but
   replay payloads change every run. Over a year that is real repo growth. I can
   drop replay storage entirely, or move the DB to a release artifact, if you
   would rather keep git clean.
5. **68 boards cost ~20s of the 23s run.** Fine at `*/30`. If you ever want
   tighter polling, the ATS source should split into a fast tier (targets, every
   run) and a slow tier (rest, hourly). Worth doing now or later?
6. **Should I try harder on the 18 dead boards?** Netflix, HashiCorp, Two Sigma,
   Hugging Face and Weights & Biases all have no public ATS board — they are on
   Workday or bespoke sites. Recovering them means a Workday parser, which is a
   real project.
7. **Quant firms are in `companies.json` but not flagged `target`.** Given the
   AI/ML focus, is that right, or do you want Jane Street / Citadel / HRT in the
   tier-1 gate?

---

```
jobpipe — Phase 1 complete (Tier A sources, end to end). 276 tests, no network in tests.

LIVE RUN: 2,514 postings from 4 sources in 23s, 2.0 MB db. Re-run: 6.2s, all
GitHub sources 304, zero new. Idempotent.
  simplify-newgrad 1268 | speedyapply-swe 767 | speedyapply-ai 491 | ats 339
  deduped out 351 | filtered by prefilter 16,365

BUILT
- All 4 Tier A sources + `jobpipe verify-companies` (probes/repairs companies.json).
- ETags persisted in the DB (fresh container each run); 304 = success, verified.
- Runner: per-source failure isolation, run-report.json in the brief's shape,
  zero-yield alarm, JSON-lines logs, faithful --dry-run.
- Fixture tests per source from captured real payloads.

REVERSE-ENGINEERED (evidence in docs/sources.md)
- Simplify: .github/scripts/listings.json on branch dev (confirmed via API).
- INTERN_USA.md DOES NOT EXIST in either speedyapply repo; USA internships are
  inside README.md. Your "if present" hedge was right.
- speedyapply columns are NOT uniform: FAANG+/Quant have a Salary column that
  the Other section lacks. Fixed-index parsing would have shifted apply URLs
  into the age column for the largest section. Parsed by header name.
- Greenhouse content is DOUBLE-escaped HTML; updated_at moves on every edit so
  I use first_published. Lever sends epoch ms (Simplify sends s) and returns a
  bare array.
- 35/89 seeded tokens were wrong. 14 recovered: Cohere/Snowflake/Notion/
  Confluent MOVED TO ASHBY, Zoox/Wealthfront to Lever, DoorDash="doordashusa",
  Cursor="cursor" not "anysphere". 18 dead and removed (Netflix, HashiCorp,
  Two Sigma, Hugging Face, W&B — all now on Workday/bespoke, no public board).

BUGS FOUND BY RUNNING AGAINST LIVE DATA (all now regression-tested)
- "Brooklyn, OH" bucketed as NYC; state codes now veto contradicting cities.
- "Connecticut, USA" -> "ct-usa"; trailing country blocked the state fallback.
- "Washington, D.C." and "Flexible - Any SpaceX Site" became phantom metros.
- --dry-run scoped its seen-set per source, hiding cross-source dedupe and
  overstating `new` ~35%.

BIGGEST DEVIATION
Added a deterministic prefilter BEFORE storage (not in the brief). ATS feeds
return every open req at every level: 15,968 fetched, 15,629 ineligible. Two
modes — curated feeds lenient, raw ATS boards strict (require an early-career
signal). Cut ATS from 3,689 rows to 339. I audited 341 strict-mode drops across
10 target boards and found no genuine new-grad reqs, but this is the rule most
likely to silently cost a posting and the one I'd most like reviewed.

Also: Age column -> posted_at (day-granular, stated not guessed); replay stores
prefiltered postings zlib-compressed for 3 runs (raw was 10.4 MB/run in a
git-committed db); term_hint -> term_default with inverted precedence.

TOP QUESTIONS
1. Work authorization? Simplify's sponsorship field is stored and ready; the
   disqualifiers just need your answer. (Anduril 2,164 + SpaceX 2,126 reqs are
   nearly all US-person-only.)
2. Is the strict ATS prefilter the right trade? 3,689 -> 339 rows.
3. term=unknown is 463/2,514 (18%) — notify or digest-only?
4. 2.0 MB db committed every 30 min; replay payloads churn every run. Keep in
   git, or move to a release artifact?
5. Quant firms are in companies.json but not target-flagged — right call?
```
