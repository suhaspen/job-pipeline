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
