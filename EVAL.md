# EVAL

Generated 2026-08-06 19:43 UTC from 7 run report(s).

## Summary

- **Daily new postings:** not enough history yet (needs one full day).
- **Triage precision (tier 1):** no resolved tier-1 pushes yet. This is the number that says whether the threshold is right; it needs you to mark postings applied or skipped.
- **Live postings:** 93
- **Baseline suppressions on record:** 2487

## Daily new postings per source

Counts are *new* rows, after dedupe and after the eligibility gate.
Today's row is still accumulating.

| Day | ats | simplify-newgrad | speedyapply-ai | speedyapply-swe | Total |
|---|---|---|---|---|---|
| 2026-08-06 | 282 | 1174 | 456 | 694 | **2606** |

## Tier volume per day

Tier 1 interrupts (capped at 3/hour). Tier 2 and 3 are digest-only —
tier 2 stopped pushing after 30 notifications landed in a single run.

| Day | tier 1 | tier 2 | tier 3 |
|---|---|---|---|
| 2026-08-06 | 41 | 2142 | 792 |

## What is driving tier 1

1 tier-1 posting(s). Signals below are the deterministic contributions; scoring is heuristic-only until an API key is configured.

| signal | postings | share | mean points |
|---|---|---|---|
| `term:winter-2027` | 1 | 100% | +40 |
| `discipline:AI/ML` | 1 | 100% | +22 |
| `target-company` | 1 | 100% | +20 |
| `remote` | 1 | 100% | +4 |
| `recency:stale` | 1 | 100% | -8 |

## Notifications

| sent | rate-capped | quiet-hours | backpressure |
|---|---|---|---|
| 32 | 0 | 0 | 0 |

## Source health

| Source | runs | 304s | raw fetched (median) | new |
|---|---|---|---|---|
| ats | 7 | 0 | 0 | 282 |
| simplify-newgrad | 7 | 3 | 1890 | 1174 |
| speedyapply-ai | 7 | 5 | 306 | 456 |
| speedyapply-swe | 7 | 5 | 398 | 694 |

## Baseline over-collapse

20 baseline id(s) absorbed more than one distinct title. Review with `jobpipe audit-suppressions`.

| baseline id | distinct titles | hits |
|---|---|---|
| 1deb3ec0a079d42b | 3 | 7 |
| 138ac42030903c09 | 3 | 6 |
| b57e0d18a71d75bf | 3 | 6 |
| 321d92e16f0a0d57 | 3 | 5 |
| ed6d059f29942681 | 3 | 5 |
| 25a78f9b8630facd | 3 | 4 |
| e752ad3c8b40766d | 3 | 4 |
| 838a43b88b15c6d1 | 2 | 8 |
| fb3bbf748a059f01 | 2 | 8 |
| 2f2fef13de7a0f20 | 2 | 6 |
| 03a37b8cdabe3f38 | 2 | 5 |
| 482fc25a8beb080c | 2 | 5 |
| 6433a06a3542959b | 2 | 5 |
| ad23ea7b375bf74f | 2 | 5 |
| bcb5cf9714e4258d | 2 | 5 |
| bd61651cf22d6d04 | 2 | 5 |
| cf76128d6f756bdb | 2 | 5 |
| 086cfef95b0fe972 | 2 | 4 |
| 0a22ab4173b65116 | 2 | 4 |
| 1e1757641045c38e | 2 | 4 |

## CI budget

Mean run 0.0s; projected 0 min/month at */30. Private-repo allowance is 2,000 min/month.

