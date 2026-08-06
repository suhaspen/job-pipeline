# Assumptions

Every ambiguity resolved without asking. Format: the call, why, and what would
change if it is wrong. Reverse-chronological by phase.

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
