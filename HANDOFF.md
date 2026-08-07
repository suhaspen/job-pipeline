# Job Pipeline — Handoff

Read this, then `FEEDBACK.md` and `ASSUMPTIONS.md`, then start on the immediate
task.

## What this is

A job-posting pipeline for a UC Irvine CS student (graduating June 2027)
applying to off-cycle co-op internships (fall 2026, winter 2027, spring 2027)
and new grad 2027 roles, focused on AI/ML, backend, and full-stack. US citizen,
no sponsorship needed, no active clearance held. Timezone
`America/Los_Angeles`.

It polls job sources, dedupes, scores, and pushes phone notifications for
high-signal postings. Latency is the point: competitive reqs collect thousands
of applicants within 48 hours.

## Current state

- Deployed. Private repo `suhaspen/job-pipeline`. Runs on GitHub Actions.
  Secrets `NTFY_TOPIC` and `HEALTHCHECK_URL` are set.
- 506 tests passing, no network in tests.
- Baseline: **2,540 ids**. Export: **424 rows — 422 live** (3 tier 1, 135 tier
  2, 284 tier 3) **and 2 expired**.
- Measured inflow: ~70 new eligible postings/day, range 42–140.
- Four sources: Greenhouse/Lever/Ashby ATS boards (71 verified tokens),
  SimplifyJobs/New-Grad-Positions, speedyapply/2027-SWE-College-Jobs,
  speedyapply/2027-AI-College-Jobs.
- No `ANTHROPIC_API_KEY` by choice. Scoring runs heuristic-only. The LLM path
  exists and activates if a key is ever added. Do not add the key to workflow
  secrets.

> The earlier handoff quoted 2,543 baseline and 114 live. Those were the
> numbers before the first scheduled CI run, which added 308 postings and
> promoted 3 reposts out of baseline. The figures above are post-run and
> reconcile against both the export and the database.

Read `FEEDBACK.md` for build history, `ASSUMPTIONS.md` for judgment calls
A1–A12, B1–B10 and C1–C6, `docs/sources.md` for every endpoint and schema,
`docs/operations.md` for the runbook.

## Immediate task — done

The free-tier work is complete. See `ASSUMPTIONS.md` C1–C6 for the judgment
calls and `tests/test_workflows.py` for what is now asserted rather than
assumed.

Summary of what changed and why:

- **`poll.yml` already had its cron.** The first-run summary showing only
  `workflow_dispatch` was the manual dispatch, not the file.
- **Setup was never the problem.** `duration_s: 324.57` of a 350s wall clock —
  the pipeline was 93% of the job, and ~302s of that was serial link
  validation of 300 cold-start postings. Fetches were 22.6s combined.
- Link validation fans out 8 wide, capped at 3 per registrable domain, on a 5s
  timeout, failing open per posting.
- `export.restore()` was silently dropping four columns. Fixed, and the class
  is closed by a round-trip parity test rather than four added names.
- ETag validators moved to `data/http-cache.db`, carried between runs by
  `actions/cache`. Before this, no CI run ever sent a conditional request.
- Shallow checkout, pip cache keyed on `pyproject.toml`, artifacts on failure
  only, all actions on their Node 24 majors.
- Cron is time-windowed with the `timezone:` field. **~683 poll runs/month.**
  At 1 billed minute each that is 717 min/month all-in; at 2 it is 1,400.
  Either fits the 2,000 free allowance. The previous uniform `*/30` was ~1,461
  runs and ~8,766 billed minutes.

## Then, in order

**Tier 1 new grad gap.** Tier 1 currently resolves to "off-cycle co-op at a
target company" — `term:winter-2027` is +40, `target-company` +20, threshold
75. A new grad role at a non-target company can't reach tier 1, so the user's
primary category never interrupts; it waits for the 07:00 digest, up to 24
hours late. Of the 135 live tier 2 postings, 43 are `new-grad` — report how
many of those are at target companies. If under ~8/day, add a tier 1 path for
them.

**`jobpipe serve` — deferred to the backlog, not cancelled.** The Sheets mirror
covers it: sorting, filtering, mobile and the user's own notes columns, for
none of the build time. Revisit only if Sheets turns out not to.

**Recruiter surfacing (deferred, low priority).** Three columns only — recruiter
name, title, LinkedIn profile URL — sourced from the posting body first, then a
search-engine API query (`site:linkedin.com/in "University Recruiter"
"{company}"`). Optionally a drafted note. Never any LinkedIn request,
connection, or message. The user sends manually. Before building, check what
fraction of tier 1 postings already name a recruiter in the JD — that's the
free half of the feature.

> The restore path is ready for this now. `export.restore()` used to drop all
> four recruiter fields, which would have cost every lookup on every CI run;
> it builds its INSERT from `FIELDS` instead, and `tests/test_export.py`
> asserts the round trip in both directions. See `ASSUMPTIONS.md` C9.

## Invariants — do not undo these

Each of these was reached by fixing a real bug. A fresh reading of the code
will suggest the opposite in several cases.

### Data safety

- No test may write to committed data files. There's an autouse fixture
  enforcing this. It exists because a test silently overwrote production data
  with a two-row store. A second fixture guards `postings.db` and
  `http-cache.db`; it caught a real case the day it was added.
- `data/applications.jsonl`: the pipeline reads, never writes. It's the only
  data in the system with no upstream source — every posting can be re-fetched,
  but nothing knows what the user applied to except that file. It's
  deliberately separate from `postings.jsonl` so the Actions runner's commits
  can never clobber local status writes.
- `postings.db` is gitignored. The committed JSONL export is the source of
  truth; the DB is rebuilt from it at run start. SQLite binaries don't delta in
  git. **Do not put `postings.db` in `actions/cache`** — `run()` skips the
  restore when the database is non-empty, so a restored cache would silently
  become the source of truth.
- Journal mode is DELETE, not WAL. WAL sidecar files beside a git-tracked
  database either need committing or silently drop writes. The same applies to
  `http-cache.db`: a WAL written after `actions/cache` snapshots the file is a
  dropped write.
- Skip the commit when the export is unchanged. An all-304 run should produce
  no commit. Change detection ignores the timestamp line.

### Dedupe and baseline

- Dedupe key excludes `source` — that's what makes cross-source dedupe work.
- Dedupe key includes numeric level. Software Engineer 1 ≠ Software Engineer 2.
  Level spellings collapse (1 = I = One = Level 0 = unlevelled entry rung);
  real levels don't. Post-cutover, an over-collapse means a genuinely new
  posting is silently baselined and never seen again.
- Baseline lookup is global, not per-source. A per-source seen-set has been the
  cause of two separate bugs in this codebase already.
- Reposts promote out of baseline when the source reports `first_published`
  after cutover. The user explicitly wants to see reposts.
- Changing the dedupe key changes every id. Any key change is a migration with
  count-parity assertions and re-derivation from `data/backlog-review.csv`, not
  a config edit.

### Source quirks

- Greenhouse: use `first_published`, not `updated_at`. `updated_at` moves on
  every edit, so a typo fix would make a six-month-old req look fresh.
- speedyapply: parse by header name. Column layout differs between sections
  (FAANG+/Quant carry a Salary column that Other omits). Fixed-index parsing
  silently shifts apply URLs into the age column.
- Simplify: `.github/scripts/listings.json` on the `dev` branch. Not `main`,
  not the README.
- `boards.greenhouse.io` → `job-boards.greenhouse.io` is a legitimate migration
  that preserves the job id. It's the most common redirect in the corpus and
  must not be flagged.
- 403 bot-blocks and timeouts are NOT expiry signals. Known bot-protected
  domains (Citadel, SmartRecruiters, amazon.jobs, Roblox, big-tech career
  sites) are never contacted at all. Treating a block as "gone" expires live
  postings.
- Big-tech boards (Google/Meta/Amazon/Apple/Microsoft/NVIDIA) are deliberately
  absent from `companies.json` — no public ATS. They arrive via the curated
  GitHub feeds.

### Filtering and scoring

- Discipline gate matches the role head only. ATS titles are
  `<function> - <team>`; full-string matching rejects real work like "Machine
  Learning Engineer Graduate - Brand Ads". `ai`/`ml` are weak signals, not
  strong ones, or "Public Policy and AI Innovation Intern" passes.
- Allowlisted phrases are removed before the seniority check. "Member of
  Technical Staff" was being rejected on the word staff — 80 postings at
  exactly the frontier labs the user most wants. "Senior Member of Technical
  Staff" still drops.
- Three separate clearance checks, never one boolean. Citizenship/US-person
  required → not disqualifying. Active clearance already held → disqualifying.
  Eligible to obtain → not disqualifying. "Must be able to obtain an active
  security clearance" contains the literal string "active security clearance";
  naive matching discards 1,334 eligible reqs.
- Zero-yield alarms on per-source fetch volume, not new-row count. A broken
  source returns zero postings; a quiet weekend returns the usual volume with
  zero new. New-row count cannot distinguish them.
- Excluded postings are demoted to an `excluded` table, not dropped. Otherwise
  the false-negative rate is unmeasurable, because the evidence is exactly the
  data that wasn't kept.
- Scoring fails open. Any error, 429, timeout or unparseable reply falls back
  to the heuristic score, stamps `tier_source: heuristic-fallback`, and
  notifies anyway. Fallbacks are never cached. The backlog never reaches the
  live scorer.
- Link checking fails open too, and now has to: under a thread pool a single
  raised exception discards the whole batch's results, not one posting's.
- Link checks are capped at 3 concurrent per registrable domain under the
  global 8. `blocked` is not an expiry signal, so a throttled runner IP is
  silent degradation — statuses keep arriving and stop meaning anything.
  Nothing downstream can detect it. Do not raise this to "use the whole pool".
- `export.restore()` derives its columns from `FIELDS`. Never hand-write a
  second column list: the two drifted by four columns once already, and the
  database is rebuilt from the export every run, so a dropped column is data
  deleted every 30 minutes rather than data lost once.

### Volume and notifications

- Fetch and normalize: uncapped. Storage: forward-only by date, never by row
  count. Notifications: capped deliberately.
- Tier 2 is digest-only. No push under any condition. Tier 1 keeps the
  interrupting push and a 3/hour cap. A quiet-hours downgrade must not consume
  a rate-cap slot.
- Quiet hours 22:00–06:00 PT downgrade, never drop. Use `zoneinfo`, not a
  hand-rolled UTC offset.

### Schedule and budget

- Every `cron:` entry carries a `timezone:`. The field is real as of March
  2026; before that these were hand-offset from UTC and drifted an hour at each
  DST boundary. `digest.yml` carried a comment claiming a timezone it did not
  have, and fired at midnight PT for its entire life.
- No two cron entries may match the same minute. GitHub fires the workflow once
  per matching entry; the concurrency group queues the duplicate and the month
  pays for it. This is asserted, not assumed — `tests/test_workflows.py`.
- Nothing is scheduled between 01:00 and 03:00 local. Those hours repeat or
  vanish at the DST boundaries.
- The weekly run count is asserted against the budget projection. If you change
  the schedule, change the number in the test and re-derive the projection.

### The Sheets mirror

- **Columns A-H are the pipeline's. Everything from I is the user's.** No code
  path writes, clears, reorders or resizes past H. Rows are matched by posting
  id and updated in place; there is no full-sheet rewrite and no
  `values.clear` in `jobpipe/sheets/`, because his notes are the only data in
  this system with no upstream copy.
- **Rows are never deleted.** An expired posting stops being updated and keeps
  its row. Deleting it takes the notes beside it.
- **A reordered header row halts the write.** Writing A-H by position into a
  sheet whose columns have moved puts a company name wherever B now is.
- **The extent read is `A:J`, not `A:A`.** Sheets truncates trailing empty rows
  from a response, so column A alone reports the last row the *pipeline*
  filled - and an append then lands on top of a user row that has a note in I
  and nothing in A.
- **Every text cell is formula-escaped.** `USER_ENTERED` is required for the
  date column and evaluates anything starting `= + - @`; the titles come from
  third-party feeds into a spreadsheet he opens.
- **The read fails open and cannot invent a status.** Outage falls back to
  `data/sheet-status.json`; no cache means no statuses, which changes nothing.
  A blank cell is "undecided", never "un-apply".
- **`SheetsClient._request` is the only network call in the package.** The test
  guard patches exactly that one function. A second HTTP path defeats it.
- Grid height is a hard ceiling - Sheets rejects a write past the last row
  rather than growing. `sheets setup` grows Live with `appendDimension`, which
  cannot shrink; setting `gridProperties.rowCount` can, and a shrink deletes.

### Security

- ntfy topics are world-readable. No credential of any kind goes into a
  notification payload — that rules out the obvious "Mark applied" button
  implementation (a PAT in an action header would be a published credential).
- Secrets never go in scripts. The user sets them in GitHub's UI himself. Don't
  write a setup script that handles secret values.
- No LinkedIn automation of any kind. Surfacing a profile URL is in scope;
  requests, connections, and messages are not.
- No headless-browser scraping. If a source needs it, stub the source and
  report rather than building it.

## Working style

Stop and report at checkpoints rather than running to completion. Ask at most 5
blocking questions — only ones where guessing wrong wastes a phase — and log
every other ambiguity in `ASSUMPTIONS.md`. Surface deviations from instructions
explicitly rather than burying them; several of the best catches in this build
came from doing that. When tests pass first try, probe the code with cases you
didn't write fixtures for — that habit has found real bugs here three separate
times.
