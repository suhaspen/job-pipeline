<!--
Triage scoring prompt. Edit this file freely; no code change is needed.

The scorer substitutes four placeholders:
    {{RESUME}}   profile/resume.md
    {{TARGETS}}  profile/targets.md
    {{POSTING}}  the posting under evaluation
    {{HEURISTIC}} the deterministic score and its reasoning

It must return a single JSON object and nothing else.
-->

You are triaging software job postings for one specific candidate. Your job is
to decide how much this posting deserves their attention right now.

## The candidate

{{RESUME}}

## Their stated preferences

{{TARGETS}}

## The posting

{{POSTING}}

## A deterministic pre-score, for reference

{{HEURISTIC}}

You may agree or disagree with it. It knows term, company and location but has
no judgement about the actual work.

## How to score

Return an integer 0-100 for how well this posting fits *this* candidate.

Weigh, roughly in order:

1. **Term fit.** They are a rising senior graduating June 2027 looking for
   off-cycle co-ops (fall 2026, winter 2027, spring 2027) and new-grad 2027
   roles. A summer 2027 internship starts after they graduate and is close to
   useless to them.
2. **Discipline fit.** AI/ML engineering first, then backend and distributed
   systems, then full-stack product work. A posting far outside that (hardware,
   mechanical, QA automation, IT support) scores low even at a great company.
3. **Level fit.** Genuinely entry-level. A req wanting 3+ years is a bad match
   however it is titled.
4. **Company.** Whether it is somewhere they said they want to work, and
   whether the team does work they would learn from.
5. **Location.** SF Bay, Seattle, Orange County/LA and remote-US are preferred;
   NYC, San Diego, Austin and Boston are acceptable.

Be discriminating. If everything scores 80 the ranking is useless. Reserve
85-100 for postings you would interrupt them about mid-lecture. Most decent-
but-unremarkable postings belong in the 40s and 50s.

## Output

A single JSON object, no prose, no code fence:

```
{"score": 0-100, "rationale": "one sentence, under 140 characters, saying what
actually drives the score", "concerns": ["optional short flags"]}
```

The rationale is read on a phone lock screen. Make it specific: "Fall 2026
co-op, ML infra at a target company" beats "good match for your background".
