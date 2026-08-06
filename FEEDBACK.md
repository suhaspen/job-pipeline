# FEEDBACK — Phase 0

Repo skeleton, config, `Store` interface + SQLite implementation, normalization
and dedupe. No network in any code path.

**144 tests passing, 0.31s, no network.** 2,323 lines across `src/` and `tests/`.

---

## Built

- **`Store` protocol + `SqliteStore`** — `upsert` / `seen` / `recent` as the
  core three, behind a `typing.Protocol`. A test asserts `SqliteStore` satisfies
  it, so drift is caught before a second backend is ever written.
- **Normalization** — company (legal suffixes, aliases, accents), title (level
  suffixes, req ids, term wording, vocabulary collapse), location (~45 metro
  buckets + `us-{state}` fallback), term inference.
- **Dedupe** — key is `(company_norm, title_norm, location_norm, term)`; id is
  `sha256(key)[:16]`. The source's own posting id is never part of the key.
- **Fixture-driven dedupe proof** — 6 `must_collapse` groups (reposts,
  cross-source overlap, level suffixes, vocabulary, req ids, company aliases)
  and 5 `must_not_collapse` groups (meaningful parentheticals, different metro,
  different term, Seattle-vs-DC, different discipline).
- **Repost lifecycle semantics** — a relist refreshes `last_seen_at`,
  `apply_url`, `location`, `source`; it preserves `first_seen_at`, `status`,
  `tier`, `score`, `applied_at`. Tested explicitly, because the failure it
  prevents is re-notifying you about a job you already applied to.
- **`companies.json`** — 89 companies (63 Greenhouse, 17 Ashby, 9 Lever), 32
  flagged `target`. All `verified: false`; see Source health.
- **Schema tables beyond the brief's row spec** — `raw_payloads` (for
  `--replay`), `runs` (for `EVAL.md`), `notifications` (for the hourly rate cap,
  which cannot work off in-memory state when every run is a fresh container).
- **CLI** — `stats`, `recent`, `normalize`, `applied`. `normalize` prints the
  key and its components, which is the tool for debugging a dedupe complaint.
- **JSON-lines logging**, `Makefile`, `.env.example`, profile templates.

## Not built

- **All sources.** Phase 1. `jobpipe run` exits 2 with an explanation rather
  than pretending to work.
- **Notifications, triage, GitHub Actions, recruiter lookup, Sheets mirror.**
  Phases 2–5, untouched.
- **`make eval`** exits 2 with a message. `EVAL.md` aggregates run reports and
  triage precision; with no runs and no scores, generating it would produce a
  table of zeros that looks like data. It is wired to auto-activate once
  `jobpipe.eval` exists.
- **`--dry-run` / `--replay`** are declared in the argument parser but not
  functional — there is no fetch to skip yet. I fixed the flag surface now so
  Phase 1 does not change it.
- **`profile/resume.md` and `profile/targets.md` are templates, not content.**
  Triage will score against placeholders until you fill them in.

## Assumptions

Full list with reasoning and blast radius in
[ASSUMPTIONS.md](ASSUMPTIONS.md) (A1–A12). The four that would cost the most to
reverse later:

| | Assumption | Would have asked |
|---|---|---|
| **A1** | `term` is never guessed — no explicit season/new-grad marker means `unknown` | Should an unlabeled intern posting be inferred from the calendar? |
| **A2** | Multi-location postings key on the **first** listed office | Is a req open in 3 metros one row or three? |
| **A4** | Reposts preserve `status`/`score`/`first_seen_at` | Should a genuine relist re-surface? |
| **A8** | Tier-1 target list lives in `companies.json`, not `targets.md` | Where do you want companies that gate tier 1 but have no pollable board? |

A2 and A4 both change every existing id or lifecycle state if reversed — cheap
now, expensive once the database has history.

## Source health

**No source was contacted. Phase 0 is specified as network-free, and no code in
this commit can make a request.** Nothing below is measured; it is what Phase 1
will check.

| Source | Status | Notes |
|---|---|---|
| Simplify New-Grad-Positions | not built | Must discover `listings.json` path from `.github/scripts/`; active branch is `dev`, not `main`. Conditional `If-None-Match`; a 304 is a successful no-op. |
| speedyapply 2027-SWE-College-Jobs | not built | Raw markdown from `raw.githubusercontent.com`, column-wise table parse. |
| speedyapply 2027-AI-College-Jobs | not built | Same. |
| ATS (Greenhouse/Lever/Ashby) | not built | 89 tokens seeded, **0 verified**. |
| newgrad-jobs.com (Tier B) | not built | `robots.txt` unread — that check gates whether the module gets written at all. |

**The 89 ATS tokens are conventional-slug guesses, not verified endpoints.**
Companies migrate ATS vendors and change slugs, so some fraction will 404. Phase
1 probes each, flips `verified`, and reports dead tokens here rather than
failing the run. Treat the file as a starting point to prune, not a checked list.

Large employers (Google, Meta, Amazon, Apple, Microsoft, NVIDIA) are excluded
from `companies.json` on purpose: they run proprietary career sites, not public
ATS boards. Listing them yields permanent 404s, not coverage. The Simplify and
speedyapply feeds are what actually cover them.

## Deviations from this brief

1. **Schema has more columns than specified.** Added `company_norm`,
   `title_norm`, `location_norm` (so a dedupe decision stays auditable after a
   title changes), `source_id` (debugging; explicitly *not* in the key), and
   `draft_note` (the brief's optional outreach column).
2. **`Store` has more than three methods.** The brief names `upsert`/`seen`/
   `recent`; I added lifecycle and bookkeeping methods. The rate cap, zero-yield
   alarm and `--replay` all need cross-run persistence, and GitHub Actions gives
   each run a fresh container — there is nowhere else for that state to live.
3. **Three extra tables** (`raw_payloads`, `runs`, `notifications`) for the same
   reason.
4. **Journal mode is `DELETE`, not WAL.** WAL sidecars beside a git-committed
   database either need committing (conflicts, corruption) or silently drop the
   newest writes. A test asserts no sidecar survives a run.
5. **`make eval` fails loudly instead of emitting an empty report.** A zeros
   table reads as a real measurement.
6. **Tier-1 target membership is a flag in `companies.json`** rather than a list
   in `targets.md`, so one file drives both polling and tier gating (A8).

## Open questions for review

Ranked by how much the answer changes the design.

1. **What is your work authorization?** The `citizenship` and `no-sponsorship`
   disqualifiers cannot fire without it. Guessing "US citizen" silently keeps
   postings you cannot take; guessing the reverse silently discards defense,
   aerospace and a chunk of finance. This is the single largest scoring lever
   and I did not want to assume it — `profile/resume.md` has the field blank.
2. **Is a req open in three metros one row or three?** (A2) Changes every id.
   Cheap now, expensive after the database accumulates history.
3. **Should `term: unknown` postings notify, or go digest-only?** Many ATS
   titles carry no season marker, so this could be a large share of volume.
   Digest-only risks burying a real co-op; notifying risks noise.
4. **Public or private repo?** The design commits your resume, target-company
   list and full application history to git. I would default to private, but
   `data/postings.db` in a public repo is a live feed of where you are applying.
5. **48 commits a day acceptable?** `*/30` cron committing state back. I plan to
   commit only when the database actually changes, which cuts it sharply — but
   confirm you want state in git rather than a release artifact.
6. **Summer 2027 — exclude entirely?** You graduate June 2027, so a summer 2027
   internship starts after graduation. The brief's enum includes it and lists
   `summer-only` as a disqualifier, which reads as a contradiction. I templated
   `targets.md` to exclude it; say if that is wrong.
7. **Should a 90-score non-target company really be silent?** Tier 1 requires
   score ≥ 75 **and** target membership, so a superb match at an unlisted
   company gets a silent push. That is what makes the aggregator company
   discovery loop matter, but it may be stricter than you want.

---

```
jobpipe — Phase 0 complete (store + normalization + dedupe). 144 tests, no network.

BUILT
- Store protocol + SqliteStore (upsert/seen/recent), Protocol conformance tested.
- Normalization: company (legal suffixes, aliases), title (level suffixes, req
  ids, term wording, SWE/SDE/co-op vocabulary collapse), location (~45 metros +
  us-{state} fallback), term inference.
- Dedupe key = (company_norm, title_norm, location_norm, term); id = sha256[:16].
  Source posting id deliberately excluded so reposts collapse.
- 6 must-collapse fixture groups (reposts, cross-source overlap, II/L4/Level 4,
  SWE=SDE=co-op, req ids, Facebook=Meta) and 5 must-not-collapse groups
  (meaningful parentheticals, metro, term, Seattle-vs-DC, discipline).
- Repost preserves status/score/first_seen_at, refreshes apply_url/last_seen_at.
- companies.json: 89 companies (63 GH / 17 Ashby / 9 Lever), 32 target.
- CLI: stats, recent, normalize (prints the key — the dedupe debugging tool).

NOT BUILT
- All sources (Phase 1). `run` exits 2 rather than faking success.
- Notifications, triage, Actions, recruiter, Sheets (Phases 2-5).
- make eval exits 2: with no runs and no scores it would print a zeros table
  that reads like data.
- --dry-run/--replay parse but are inert; flag surface fixed early on purpose.
- profile/*.md are templates, not your content.

DEVIATIONS
- Extra columns (company_norm/title_norm/location_norm/source_id/draft_note) and
  3 extra tables (raw_payloads/runs/notifications). The hourly rate cap,
  zero-yield alarm and --replay need cross-run state; every Actions run is a
  fresh container, so there is nowhere else for it.
- Store has more than the 3 named methods, same reason.
- SQLite journal mode DELETE not WAL — WAL sidecars beside a git-committed db
  corrupt or drop writes. Asserted by test.

SOURCE HEALTH
Nothing contacted; Phase 0 is network-free by spec. The 89 ATS tokens are
conventional-slug GUESSES, all verified:false — some will 404. Phase 1 probes
and prunes. Google/Meta/Amazon/Apple/MSFT/NVIDIA excluded on purpose: no public
ATS boards, they arrive via Simplify/speedyapply.

TOP QUESTIONS
1. Work authorization? citizenship/no-sponsorship disqualifiers cannot fire
   without it — largest single scoring lever, left blank rather than guessed.
2. Req open in 3 metros: one row or three? Changes every id; cheap now.
3. term=unknown postings — notify or digest-only? Possibly large volume share.
4. Private repo? The design commits resume + full application history to git.
5. Summer 2027: brief's enum includes it but you graduate June 2027.
```
