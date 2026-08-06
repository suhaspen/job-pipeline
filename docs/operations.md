# Operations

## Schedule

`*/30 * * * *` on the default branch. **Do not go below this.** GitHub enforces
a 5-minute floor, recommends no more often than 15 minutes on the free tier,
and the GitHub-sourced feeds only update daily — polling faster buys nothing.

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

The digest workflow uses a `cron` entry evaluated in UTC. **07:00 America/
Los_Angeles is what matters**, so if GitHub's `timezone:` field is available on
your plan, set it next to the cron entry rather than hand-offsetting UTC — a
fixed offset drifts by an hour twice a year at DST boundaries. The 30-minute
poll does not care; the digest does.

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

| | |
|---|---|
| Mean run | ~25s (warm, mostly 304s) to ~60s (cold) |
| Runs/day | 48 |
| Projected | ~550 min/month |
| Headroom | ~3.5x |

Billing rounds each job up to the nearest minute, so 48 runs/day is closer to
**~1,440 billed minutes/month** in the worst case. That is still inside the
allowance but the headroom is thinner than the raw seconds suggest. `make eval`
warns above a projected 1,200 min/month.

If it gets tight: split the ATS source into a fast tier (targets, every run)
and a slow tier (the rest, hourly). The ~70 boards are ~20s of every run.

## Secrets

Set these in **Settings → Secrets and variables → Actions**:

| Secret | Required | Notes |
|---|---|---|
| `NTFY_TOPIC` | yes | Treat as a password — ntfy topics are world-readable |
| `HEALTHCHECK_URL` | recommended | Dead-man's switch; a *missed* ping is the alarm |
| `ANTHROPIC_API_KEY` | no | Absent by design. Scoring runs heuristic-only; add it to enable the LLM path |

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
| `INDEX.md` | Human view of live postings |

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
