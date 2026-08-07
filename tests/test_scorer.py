"""Scoring tests. No network — the API is stubbed.

The three properties that matter operationally: fail open, score once, never
score the backlog.
"""

from __future__ import annotations

import json

import pytest

from jobpipe.config import ATSCompany, Config
from jobpipe.models import Disqualifier, Posting, Status, Term, Tier, utcnow
from jobpipe.store import SqliteStore
from jobpipe.triage import scorer as S
from jobpipe.triage.eligibility import EligibilityProfile


def posting(company="Anthropic", title="Software Engineer Intern", term=Term.FALL_2026,
            location_norm="sf-bay", pid="p1") -> Posting:
    now = utcnow()
    from jobpipe.normalize import normalize_company, normalize_title

    return Posting(
        id=pid, dedupe_key=f"{pid}|k", company=company, title=title, term=term,
        location="San Francisco, CA", remote=False, apply_url="https://x/jobs/1",
        source="ats", first_seen_at=now, last_seen_at=now, posted_at=now,
        company_norm=normalize_company(company), title_norm=normalize_title(title),
        location_norm=location_norm,
    )


@pytest.fixture
def cfg(tmp_path):
    return Config(
        db_path=tmp_path / "t.db",
        http_cache_path=tmp_path / "http-cache.db",
        export_path=tmp_path / "postings.jsonl",
        baseline_path=tmp_path / "baseline.txt",
        index_path=tmp_path / "INDEX.md",
        index_by_score_path=tmp_path / "INDEX-by-score.md",
        companies=[ATSCompany(name="Anthropic", ats="greenhouse", token="a", target=True)],
    )


@pytest.fixture
def profile():
    from jobpipe.triage.eligibility import WorkAuthorization

    return EligibilityProfile(
        work_authorization=WorkAuthorization(us_person=True),
        wanted_terms={"fall-2026": True, "summer-2027": False, "new-grad": True},
        configured=True,
    )


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def api(score=88, rationale="Fall 2026 ML co-op at a target company"):
    return FakeResponse(200, {
        "content": [{"type": "text", "text": json.dumps(
            {"score": score, "rationale": rationale})}]
    })


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        r = self.responses.pop(0) if self.responses else self.responses
        if isinstance(r, Exception):
            raise r
        return r


class TestHeuristic:
    def test_offcycle_coop_at_a_target_scores_high(self, cfg, profile):
        score, trace = S.heuristic_score(
            posting(), target_companies=cfg.target_companies, profile=profile
        )
        assert score >= S.TIER1_SCORE
        assert "fall-2026" in trace and "target company" in trace

    def test_summer_2027_is_heavily_penalised(self, cfg, profile):
        # Graduating June 2027: a summer 2027 internship starts after that.
        score, _ = S.heuristic_score(
            posting(term=Term.SUMMER_2027), target_companies=cfg.target_companies,
            profile=profile,
        )
        assert score < S.TIER2_SCORE

    def test_non_target_company_scores_lower(self, cfg, profile):
        target, _ = S.heuristic_score(
            posting(), target_companies=cfg.target_companies, profile=profile
        )
        other, _ = S.heuristic_score(
            posting(company="Nowhere Inc"), target_companies=cfg.target_companies,
            profile=profile,
        )
        assert other < target

    def test_ml_beats_frontend(self, cfg, profile):
        ml, _ = S.heuristic_score(
            posting(title="Machine Learning Engineer Intern"),
            target_companies=cfg.target_companies, profile=profile,
        )
        fe, _ = S.heuristic_score(
            posting(title="Frontend Engineer Intern"),
            target_companies=cfg.target_companies, profile=profile,
        )
        assert ml > fe

    def test_bounded(self, cfg, profile):
        for term in Term:
            score, _ = S.heuristic_score(
                posting(term=term), target_companies=cfg.target_companies, profile=profile
            )
            assert 0 <= score <= 100

    def test_trace_explains_the_number(self, cfg, profile):
        _, trace = S.heuristic_score(
            posting(), target_companies=cfg.target_companies, profile=profile
        )
        assert "+" in trace


class TestTierAssignment:
    def test_thresholds(self, cfg):
        p = posting()
        targets = cfg.target_companies
        assert S.assign_tier(80, p, targets, []) is Tier.INTERRUPTING
        assert S.assign_tier(60, p, targets, []) is Tier.SILENT
        assert S.assign_tier(20, p, targets, []) is Tier.DIGEST

    def test_tier1_requires_a_target_company(self, cfg):
        other = posting(company="Nowhere Inc")
        assert S.assign_tier(95, other, cfg.target_companies, []) is Tier.SILENT

    def test_disqualifiers_force_tier3_regardless_of_score(self, cfg):
        assert S.assign_tier(
            100, posting(), cfg.target_companies, [Disqualifier.CLEARANCE]
        ) is Tier.DIGEST


class TestResponseParsing:
    def test_plain_json(self):
        assert S.parse_response('{"score": 72, "rationale": "ok"}') == (72, "ok")

    def test_inside_a_code_fence(self):
        text = '```json\n{"score": 40, "rationale": "meh"}\n```'
        assert S.parse_response(text) == (40, "meh")

    def test_with_surrounding_prose(self):
        text = 'Here is my answer:\n{"score": 55, "rationale": "fine"}\nHope that helps.'
        assert S.parse_response(text) == (55, "fine")

    def test_concerns_are_appended(self):
        got = S.parse_response(
            '{"score": 60, "rationale": "decent", "concerns": ["summer only"]}'
        )
        assert "summer only" in got[1]

    def test_clamped(self):
        assert S.parse_response('{"score": 500, "rationale": "x"}')[0] == 100
        assert S.parse_response('{"score": -20, "rationale": "x"}')[0] == 0

    @pytest.mark.parametrize("bad", ["", "no json here", "{broken", '{"rationale": "no score"}'])
    def test_unparseable_returns_none(self, bad):
        assert S.parse_response(bad) is None


class TestFailOpen:
    """A posting is never silently dropped because the scorer had a bad day."""

    def _scorer(self, cfg, profile, session):
        cfg.anthropic_api_key = "test-key"
        store = SqliteStore(cfg.db_path)
        return S.Scorer(cfg, store, profile, session=session), store

    @pytest.mark.parametrize("failure", [
        FakeResponse(429), FakeResponse(500), FakeResponse(401),
        FakeResponse(200, {"content": [{"type": "text", "text": "not json"}]}),
    ])
    def test_api_failure_falls_back_to_heuristic(self, cfg, profile, failure):
        sc, store = self._scorer(cfg, profile, FakeSession(failure))
        result = sc.score(posting(), None, [])

        assert result.tier_source == S.TierSource.FALLBACK
        assert result.score > 0, "the posting keeps a usable score"
        assert result.tier is Tier.INTERRUPTING, "and still notifies"
        assert sc.stats.fallbacks == 1
        store.close()

    def test_network_exception_falls_back(self, cfg, profile):
        sc, store = self._scorer(cfg, profile, FakeSession(RuntimeError("boom")))
        result = sc.score(posting(), None, [])
        assert result.tier_source == S.TierSource.FALLBACK
        assert result.tier is Tier.INTERRUPTING
        store.close()

    def test_a_fallback_is_never_cached(self, cfg, profile):
        """Otherwise a 429 would pin the posting to its heuristic score forever."""
        sc, store = self._scorer(cfg, profile, FakeSession(FakeResponse(429), api(91)))
        first = sc.score(posting(), None, [])
        assert first.tier_source == S.TierSource.FALLBACK

        second = sc.score(posting(), None, [])
        assert second.tier_source == S.TierSource.LLM
        assert second.score == 91
        store.close()

    def test_no_api_key_uses_the_heuristic_without_calling(self, cfg, profile):
        store = SqliteStore(cfg.db_path)
        session = FakeSession()
        sc = S.Scorer(cfg, store, profile, session=session)
        result = sc.score(posting(), None, [])
        assert result.tier_source == S.TierSource.HEURISTIC
        assert session.calls == 0
        store.close()


class TestCaching:
    def test_second_score_does_not_call_the_api(self, cfg, profile):
        cfg.anthropic_api_key = "k"
        store = SqliteStore(cfg.db_path)
        session = FakeSession(api(88))
        sc = S.Scorer(cfg, store, profile, session=session)

        first = sc.score(posting(), None, [])
        assert first.tier_source == S.TierSource.LLM
        assert session.calls == 1

        sc2 = S.Scorer(cfg, store, profile, session=FakeSession())
        second = sc2.score(posting(), None, [])
        assert second.tier_source == S.TierSource.CACHED
        assert second.score == 88
        assert sc2.stats.cached == 1
        store.close()

    def test_cache_survives_a_reopen(self, cfg, profile):
        cfg.anthropic_api_key = "k"
        with SqliteStore(cfg.db_path) as store:
            S.Scorer(cfg, store, profile, session=FakeSession(api(77))).score(
                posting(), None, []
            )
        with SqliteStore(cfg.db_path) as store:
            session = FakeSession()
            result = S.Scorer(cfg, store, profile, session=session).score(posting(), None, [])
            assert result.score == 77
            assert session.calls == 0

    def test_disqualified_postings_never_reach_the_api(self, cfg, profile):
        cfg.anthropic_api_key = "k"
        store = SqliteStore(cfg.db_path)
        session = FakeSession()
        sc = S.Scorer(cfg, store, profile, session=session)
        result = sc.score(posting(), None, [Disqualifier.CLEARANCE])
        assert result.tier is Tier.DIGEST
        assert session.calls == 0
        store.close()

    def test_low_heuristic_scores_skip_the_api(self, cfg, profile):
        """Already destined for the digest; paying for a call adds nothing."""
        cfg.anthropic_api_key = "k"
        store = SqliteStore(cfg.db_path)
        session = FakeSession()
        sc = S.Scorer(cfg, store, profile, session=session)
        result = sc.score(
            posting(company="Nowhere", title="Frontend Engineer", term=Term.SUMMER_2027,
                    location_norm="unknown"),
            None, [],
        )
        assert session.calls == 0
        assert result.tier is Tier.DIGEST
        store.close()


class TestStats:
    def test_reported_shape(self, cfg, profile):
        cfg.anthropic_api_key = "k"
        store = SqliteStore(cfg.db_path)
        sc = S.Scorer(cfg, store, profile, session=FakeSession(api(80)))
        sc.score(posting(), None, [])
        stats = sc.stats.to_dict()
        for key in ("calls", "cached", "fallbacks", "latency_ms_total", "latency_ms_mean"):
            assert key in stats
        assert stats["calls"] == 1
        store.close()


class TestPromptTemplate:
    def test_all_placeholders_are_substituted(self):
        text = S.build_prompt(
            posting(), None, (70, "trace"), resume="RESUME TEXT",
            targets="TARGETS TEXT", template=S._load(S.PROMPT_PATH),
        )
        assert "{{" not in text
        assert "RESUME TEXT" in text and "TARGETS TEXT" in text
        assert "Anthropic" in text and "fall-2026" in text

    def test_prompt_file_is_editable_without_touching_code(self):
        assert S.PROMPT_PATH.exists()
        body = S.PROMPT_PATH.read_text()
        for token in ("{{RESUME}}", "{{TARGETS}}", "{{POSTING}}", "{{HEURISTIC}}"):
            assert token in body
