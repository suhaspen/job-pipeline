# Operations

## Schedule

Time-windowed, on the default branch, all entries in `America/Los_Angeles`:

| When | Cadence | Runs |
|---|---|---|
| Weekdays 06:00–19:00 | every 30 min | 27/day |
| Weekdays, the rest (23:00, 03:00) | every 4 h | 2/day |
| Weekends (03:00 → 23:00) | every 4 h | 6/day |

**157 runs/week, ~683/month.** Uniform `*/30` was 48/day; the overnight and
weekend slots were buying nothing, because US reqs do not go up at 03:00 on a
Sunday. **Do not go below 30 minutes in the window.** GitHub enforces a
5-minute floor, recommends no more often than 15 minutes on the free tier, and
the GitHub-sourced feeds only update daily — polling faster buys nothing.

Two rules the schedule has to keep, both asserted in `tests/test_workflows.py`:

- **No two entries may match the same minute.** GitHub fires the workflow once
  per matching entry. The concurrency group serialises them instead of merging
  them, so the second is a full duplicate run, billed. This is why the weekday
  off-hours entry is `0 3,23` rather than `0 */4` — `09/13/17` would collide
  with the in-window entry.
- **Nothing between 01:00 and 03:00 local.** That hour repeats on the fall-back
  night and does not exist on spring-forward.

### Scheduled runs are not punctual

There is no SLA. Delays of 5–30 minutes are routine and longer at peak (the top
of the hour is the worst — `*/30` lands on :00 and :30, which are the busiest
slots). Treat **30 minutes as nominal and 30–60 as the realistic p50**.

Every run records `scheduled_for` and `schedule_delay_s` in
`data/run-report.json`, so the true distribution is measurable rather than
assumed. Check it after a week:

```bash
jq -r '[.scheduled_for, .schedule_delay_s] | @tsv' data/run-report.json
```

### Timezone

`timezone:` next to the cron entry is a real field as of March 2026, and every
entry in this repo uses it. Never hand-offset from UTC — a fixed offset drifts
by an hour twice a year at DST boundaries.

This was live for the digest: the entry was a bare `0 7 * * *` under a comment
claiming it meant 07:00 America/Los_Angeles. It meant 07:00 UTC, so the daily
tier 2 roundup arrived at midnight PT. Fixed, and pinned by a test.

On spring-forward GitHub advances a schedule that lands in a skipped hour to
the next valid time; nothing here is scheduled there anyway.

### Default branch only

Scheduled workflows fire **only on the repository's default branch**. If you
work on a branch, the schedule silently does nothing. Confirm with:

```bash
git branch --show-current
```

### The 60-day silent disable

**GitHub disables scheduled workflows after 60 days of repository inactivity —
no error, no log entry, no email. They just stop.**

The skip-commit-when-unchanged rule makes this genuinely reachable: a quiet
stretch produces no commits at all. Two independent guards:

1. `keepalive.yml` forces a commit if the repo has been untouched for 45 days.
2. The dead-man's switch catches it regardless — a missed healthcheck ping is
   what should alarm you, and it fires whether the cause is a disabled
   workflow, a broken run or a deleted repo.

## Budget

Private repos get **2,000 free Linux minutes/month**.

Billing rounds **each job** up to the nearest minute, so the run count matters
more than the seconds.

| | poll | digest | keepalive | total |
|---|--:|--:|--:|--:|
| Runs/month | 683 | 30 | 4 | 717 |
| At 1 billed min | 683 | 30 | 4 | **717** |
| At 2 billed min | 1,366 | 30 | 4 | **1,400** |

Both fit 2,000. For comparison, the uniform `*/30` schedule was ~1,461 poll
runs/month, which at the 6 billed minutes the first live run actually cost is
~8,766 — four times the allowance. `make eval` warns above a projected 1,200
min/month.

### Where the time goes

Measured on the first live run (`run_id: 20260807T014406Z`):

| | |
|---|--:|
| Wall clock | 350s |
| Pipeline (`duration_s`) | 324.6s |
| — four source fetches | 22.6s |
| — link validation, 300 postings, serial | ~302s |
| Setup: checkout + Python + install + artifact | ~25s |

Setup was never the problem. Two changes followed:

- **Link validation fans out 8 wide, 3 per registrable domain, on a 5s
  timeout.** That run was a cold start — 308 new postings, so 300 links.
  Steady state is one or two per run at ~70 new postings/day over 157
  runs/week, so this is insurance against backfills and cold containers rather
  than the common case. The per-domain cap is not tuning: `blocked` is not an
  expiry signal, so a throttled runner IP degrades silently and nothing
  downstream can detect it.
- **ETag validators persist across runs.** They used to live in `postings.db`,
  which is rebuilt from the committed JSONL every run and therefore always
  started empty in CI — no request was ever conditional, so every run pulled
  ~12 MB of Simplify listings and all 71 boards in full and re-derived 16k
  exclusion rows from unchanged payloads. They now live in
  `data/http-cache.db`, restored by `actions/cache`. **Do not cache
  `postings.db` instead:** `run()` skips the restore-from-JSONL when the
  database is non-empty, so the cache would silently become the source of
  truth.

Every run writes a fetch-vs-everything-else split to the job summary, so the
next real measurement is one click rather than another profiling session.

If it gets tight: split the ATS source into a fast tier (targets, every run)
and a slow tier (the rest, hourly). The ~70 boards are ~22s of a cold run.

## Secrets

Set these in **Settings → Secrets and variables → Actions**:

| Secret | Required | Notes |
|---|---|---|
| `NTFY_TOPIC` | yes | Treat as a password — ntfy topics are world-readable |
| `HEALTHCHECK_URL` | recommended | Dead-man's switch; a *missed* ping is the alarm |
| `ANTHROPIC_API_KEY` | no | Absent by design. Scoring runs heuristic-only; add it to enable the LLM path |
| `GOOGLE_SHEET_ID` | no | Sheets mirror. Both this and the key, or neither |
| `GOOGLE_SA_KEY` | no | Service-account JSON, base64. See below |

`profile/local.yml` is gitignored and therefore **not present in CI**. The
work-authorization disqualifiers are inert there and postings are scored
without them. If you want them applied in CI, add the file's contents as a
secret and write it out in a workflow step.

## Repo setup

Run these yourself — they need your credentials.

```bash
gh repo create job_search --private --source=. --remote=origin --push
```

Then set each secret:

```bash
gh secret set NTFY_TOPIC --body "$(grep '^NTFY_TOPIC=' .env | cut -d= -f2-)"
```

```bash
gh secret set HEALTHCHECK_URL --body "https://hc-ping.com/YOUR-UUID"
```

Confirm the schedule is live after the first hour:

```bash
gh run list --workflow=poll.yml --limit 5
```

**Keep the repo private.** It contains your application status, and the JSONL
export is a live record of where you are applying.

## What is committed

| Path | Why |
|---|---|
| `data/postings.jsonl` | Source of truth. Sorted by id, stable key order, diffs line-by-line |
| `data/baseline.txt` | Ids seen before cutover; one per line |
| `data/run-report.json` | Latest run outcome |
| `INDEX.md` | Live postings, newest first (score breaks ties) |
| `INDEX-by-score.md` | The same rows ordered by fit |
| `data/sheet-status.json` | Last known copy of your Sheets status column |

`data/postings.db` is **not** committed — it is a rebuildable cache. SQLite
rewrites a multi-megabyte blob on every commit even for a one-row change, so
committing it made repo growth track run count rather than data. A CI container
starts with no database and restores from the JSONL.

Replay payloads and JSON logs go to **Actions artifacts, 30-day retention**,
not to git.

## Runbook

**No notifications for a day.** Check the healthcheck first — if pings stopped,
the workflow is not running (disabled schedule, or a failing job). If pings are
fine, the pipeline is running and finding nothing new: check `make eval` for a
source volume drop.

**A source volume warning.** `jobpipe run --source <name>` and read the
warnings. Usually a schema change; the fixture tests in `tests/test_sources.py`
will confirm.

**Notification fatigue.** Tier 2 is already digest-only. If tier 1 is still too
loud, raise `TIER1_SCORE` in `triage/scorer.py` or prune `companies.json` —
tier 1 requires target-company membership, so the target list is the blunt
instrument.

**Nothing in INDEX.md after a cutover.** Expected. The table starts empty and
fills as new postings arrive.

## Google Sheets mirror

Optional. With neither secret set the pipeline does nothing Sheets-related and
says so in the run report — that is not an error.

### What the pipeline owns

| | |
|---|---|
| **A–H**, pipeline | `id`, `company`, `title`, `term`, `tier`, `location`, `posted_date`, `apply_url` |
| **I onward**, yours | status, date applied, notes, follow-up, referral |

**Nothing in the pipeline writes, clears, reorders or resizes a column past H.**
Rows are matched by posting id and updated in place; new postings are appended
below the last used row. There is no full-sheet rewrite and no `values.clear`
anywhere in `jobpipe/sheets/`, because your notes are the only data in this
system with no upstream copy — the same reason `data/applications.jsonl` is
read and never written.

Two things follow from that and should not be "fixed":

- **A reordered header row halts the write.** If A–H are not the eight names
  above, `sync_live` raises instead of writing, because writing by position
  into a reordered sheet puts a company name wherever B now is.
- **Rows are never deleted.** An expired posting stops being updated and keeps
  its row, notes intact. The tab grows; filter it.

### Setup — you run these, they need your credentials

1. **Create a service account** at
   `console.cloud.google.com` → IAM & Admin → Service Accounts. No roles are
   needed; it gets access from the sheet share in step 4, not from IAM.
2. **Enable the Sheets API** for the project: APIs & Services → Library →
   Google Sheets API → Enable.
3. **Create a JSON key** on the service account → Keys → Add key → JSON.
   Base64 it and put it in the secret. This is the only handling step, and it
   never leaves your machine:

   ```bash
   base64 -i ~/Downloads/your-key.json | tr -d '\n' | pbcopy
   ```

   Paste into **Settings → Secrets and variables → Actions → `GOOGLE_SA_KEY`**.
   Plain JSON is accepted too, if base64 is more trouble than it is worth.

4. **Share the sheet with the service account address** (Editor). This is the
   step that is always the problem: without it every call returns
   `PERMISSION_DENIED` and nothing else is wrong. The address is in the key
   JSON as `client_email`, and `jobpipe sheets doctor` prints it.
5. **`GOOGLE_SHEET_ID`** is the id in the sheet URL:
   `docs.google.com/spreadsheets/d/`**`<this part>`**`/edit`.
6. Put both in `.env` locally as well, then:

   ```bash
   jobpipe sheets doctor
   ```

   ```bash
   jobpipe sheets setup
   ```

   `setup` is idempotent: it creates the Live / Backlog / Stats tabs, writes
   header rows **only into empty ones**, grows the grid, and rebuilds the
   conditional formatting. Re-running it never overwrites data.

### The backlog import

One-time, local only — `data/backlog-review.csv` is gitignored and therefore
absent in CI. 2,511 rows, off-cycle first: the 67 fall/winter/spring co-op reqs
are the reason to look, and sorted any other way they are invisible.

```bash
jobpipe sheets import-backlog
```

```bash
jobpipe sheets import-backlog --write
```

### Status column

Column I understands **Applied, Interviewing, Rejected, Skipped**,
case-insensitively. Anything else is left alone and logged — it is your column,
and an unrecognised word there is a note, not a mistake to correct. There is no
data validation dropdown for the same reason: adding one would be the pipeline
modifying a column it does not own.

What it feeds:

- **Applied / Interviewing** clears the posting from the unapplied backlog.
- **Applied with a date in column J**, seven days ago or more, with nothing
  since, turns the row pink — the follow-up flag.
- **Tier 1** rows are highlighted amber.

The read fails open. A Sheets outage falls back to `data/sheet-status.json`,
the last known copy; no cache means no statuses, which leaves everything
exactly as the store already has it. **Nothing in the read path can invent a
status, clear one, or block a run** — `sheets.read_from` in the run report says
which happened, and a persistent `"cache"` is the thing to look at.

### Grid capacity

Sheets rejects a write past the last grid row rather than growing the tab. A
new spreadsheet is 1,000 rows, which at ~70 new postings a day is about eight
days. `sheets setup` grows Live to 20,000, and a run reports `room_low` once
fewer than 500 rows remain, so the fix is a command run at leisure rather than
a failed poll.
