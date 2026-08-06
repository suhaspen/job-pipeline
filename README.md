# jobpipe

A polling pipeline for co-op and new-grad SWE / AI-ML postings. Runs on GitHub
Actions cron — no server, no always-on machine. Every run fetches, normalizes,
dedupes, triages, stores, notifies and reports in a single pass.

**Status: Phase 1 complete.** All Tier A sources run end to end into SQLite —
2,514 postings from 4 sources in 23s. Notifications land in Phase 2. See
[FEEDBACK.md](FEEDBACK.md) for what is and is not built, and
[ASSUMPTIONS.md](ASSUMPTIONS.md) for every judgment call.

## Quick start

```bash
make install
make test
```

Run the pipeline:

```bash
make dry-run
```

Inspect how any posting would be deduped:

```bash
.venv/bin/python -m jobpipe.cli normalize --company "Stripe, Inc." --title "Software Engineer Intern, Fall 2026 (Co-op)" --location "South San Francisco, CA"
```

```
{
  "id": "d93ecfbbf9230857",
  "dedupe_key": "stripe|software engineer intern|sf-bay|fall-2026",
  ...
}
```

That same id is produced by `Software Engineer, Co-Op - Fall 2026` at
`Stripe` in `San Francisco, CA` — which is the whole point.

## Layout

```
src/jobpipe/
  models.py       RawPosting (what sources emit) -> Posting (what is stored)
  normalize.py    company/title/location/term normalization + dedupe key
  store/          Store protocol + SQLite implementation
  sources/        simplify, speedyapply, ats (greenhouse/lever/ashby)
  triage/         prefilter (eligibility rules); scoring lands in Phase 3
  runner.py       one pass: fetch -> prefilter -> dedupe -> store -> report
  config.py       env + companies.json
  logging_.py     JSON-lines run logs
  cli.py          jobpipe entry point
companies.json    ATS board tokens; `target: true` gates tier 1
profile/          resume.md + targets.md — the candidate side of triage
data/postings.db  source of truth, committed to the repo
data/run-report.json  machine-readable outcome of the latest run
docs/sources.md   every endpoint and schema, with what was verified how
tests/            fixture-driven, no network
```

## Sources

| Source | What it reads |
|---|---|
| `simplify-newgrad` | `listings.json` on branch `dev` — JSON, not the README |
| `speedyapply-swe` | `NEW_GRAD_USA.md` + `README.md` raw markdown |
| `speedyapply-ai` | same, AI/ML-focused repo |
| `ats` | 71 Greenhouse / Lever / Ashby boards from `companies.json` |

Full endpoint and schema notes, including what was reverse-engineered and how,
are in [docs/sources.md](docs/sources.md).

## Design notes

**One run does everything.** Storage and notification are written by the same
run, not chained as a downstream trigger. On Google Sheets specifically, API
writes do not fire `onEdit`/`onChange` triggers at all, so a trigger-based
design would silently never notify.

**Dedupe keys on `(company, title, location, term)`, never on the source's
posting id.** A company that closes a req and relists it gets a fresh source id
for the same job. Reposts are common; collapsing them is what keeps the
notifications worth reading.

**Ids are sha256, not Python's `hash()`.** The database is committed to git, so
a per-process-salted id would orphan every row between runs.

**Nothing is guessed.** `posted_at` stays null when a source does not publish
one, and `term` stays `unknown` without an explicit marker — a guessed term
poisons the dedupe key.

**A prefilter runs before storage.** ATS boards return every open req a company
has: 15,968 fetched per run, 15,629 of them sales, recruiting or senior. The
gate is an eligibility question ("is this the right kind of job"), never a score.
Curated feeds run lenient; raw company boards run strict. See
[ASSUMPTIONS.md](ASSUMPTIONS.md) B1-B2.

**One broken source never kills a run.** Every source is wrapped; failures land
in `data/run-report.json` with `ok: false`. A run that dies because one feed
changed its schema is a silent outage while you believe you are covered.

## Commands

| | |
|---|---|
| `make test` | full suite, no network |
| `make stats` | database + configuration summary |
| `make recent` | list stored postings |
| `make dry-run` | full pipeline, no writes, no pushes |
| `make feedback` | copy-paste block + latest EVAL summary |
| `jobpipe run --source ats` | limit a run to named sources |
| `jobpipe verify-companies --write` | probe every ATS board and repair `companies.json` |

## Not sources

No LinkedIn scraping, no headless browsers, no CAPTCHA handling, no
auto-submitting applications. The recruiter feature stores a name, title and
profile URL from a search API and nothing else — you open the link and act
manually.
