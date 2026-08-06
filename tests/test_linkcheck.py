"""Apply-link classification and precedence. Pure functions, no network."""

from __future__ import annotations

import pytest

from jobpipe.linkcheck import (
    DEFAULT_RANK,
    LinkStatus,
    classify,
    has_job_id,
    looks_like_index,
    prefer_url,
    source_rank,
)


class TestJobIdDetection:
    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/cloudflare/jobs/8052785?gh_jid=8052785",
            "https://job-boards.greenhouse.io/togetherai/jobs/5157559007",
            "https://jobs.ashbyhq.com/notion/6ccbc30c-2de0-4395-af14-3641cd15961b",
            "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c",
            "https://salesforce.wd12.myworkdayjobs.com/en-US/x/job/CA/SWE_JR328085",
            "https://acme.com/careers?jobId=4471",
        ],
    )
    def test_real_req_urls(self, url):
        assert has_job_id(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/cloudflare",
            "https://jobs.ashbyhq.com/notion",
            "https://jobs.lever.co/palantir",
            "https://acme.com/careers",
            "https://acme.com/",
            "https://acme.com/jobs/search",
        ],
    )
    def test_index_urls(self, url):
        assert has_job_id(url) is False


class TestIndexDetection:
    def test_the_exact_url_that_caused_the_bad_notification(self):
        # A board root with no job id: this is what sent a push to a careers
        # index instead of a req.
        assert looks_like_index("https://boards.greenhouse.io/cloudflare") is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://jobs.ashbyhq.com/notion",
            "https://jobs.lever.co/palantir",
            "https://acme.com/careers",
            "https://acme.com/jobs",
            "https://acme.com/",
        ],
    )
    def test_index_shapes(self, url):
        assert looks_like_index(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/cloudflare/jobs/8052785",
            "https://jobs.ashbyhq.com/notion/6ccbc30c-2de0-4395-af14-3641cd15961b",
            "https://acme.com/careers/engineering/backend-intern-2027",
        ],
    )
    def test_req_urls_are_not_indexes(self, url):
        assert looks_like_index(url) is False


class TestClassify:
    def test_healthy_link(self):
        url = "https://boards.greenhouse.io/acme/jobs/991"
        assert classify(url, url, 200).status is LinkStatus.OK

    def test_domain_migration_keeping_the_job_id_is_fine(self):
        """Greenhouse moved boards.greenhouse.io -> job-boards.greenhouse.io.

        The redirect is legitimate and the req survives, so this must not be
        flagged - it is the single most common redirect in the whole corpus.
        """
        result = classify(
            "https://boards.greenhouse.io/cloudflare/jobs/8052785?gh_jid=8052785",
            "https://job-boards.greenhouse.io/cloudflare/jobs/8052785?gh_jid=8052785",
            200,
        )
        assert result.status is LinkStatus.OK

    def test_redirect_to_board_root_is_flagged(self):
        result = classify(
            "https://boards.greenhouse.io/acme/jobs/991",
            "https://boards.greenhouse.io/acme",
            200,
        )
        assert result.status is LinkStatus.REDIRECTED_TO_INDEX
        assert "redirected" in result.note

    @pytest.mark.parametrize("code", [404, 410, 451])
    def test_gone_statuses_are_dead(self, code):
        url = "https://x/jobs/1"
        assert classify(url, url, code).status is LinkStatus.DEAD

    @pytest.mark.parametrize("code", [401, 403, 429])
    def test_bot_protection_is_blocked_not_dead(self, code):
        """Citadel and SmartRecruiters answer 403 to any non-browser client.

        A live audit had 9 of 280 links in this state. Filing them as dead
        would have expired live postings - the worst outcome this check has.
        """
        url = "https://www.citadel.com/careers/details/swe-intern/"
        result = classify(url, url, code)
        assert result.status is LinkStatus.BLOCKED
        assert result.is_expiry_signal is False

    @pytest.mark.parametrize("code", [500, 502, 503])
    def test_server_errors_are_unreachable_not_dead(self, code):
        result = classify("https://x/jobs/1", "https://x/jobs/1", code)
        assert result.status is LinkStatus.UNREACHABLE
        assert result.is_expiry_signal is False

    def test_only_real_evidence_expires_a_posting(self):
        expiring = {LinkStatus.DEAD, LinkStatus.REDIRECTED_TO_INDEX}
        for status in LinkStatus:
            assert status.is_expiry_signal is (status in expiring), status

    @pytest.mark.parametrize("status", [LinkStatus.DEAD, LinkStatus.REDIRECTED_TO_INDEX])
    def test_bad_statuses_are_expiry_signals(self, status):
        from jobpipe.linkcheck import LinkResult

        assert LinkResult(status).is_expiry_signal is True

    def test_ok_is_not_an_expiry_signal(self):
        from jobpipe.linkcheck import LinkResult

        assert LinkResult(LinkStatus.OK).is_expiry_signal is False


class TestSourcePrecedence:
    def test_ranking(self):
        assert source_rank("ats") > source_rank("simplify-newgrad")
        assert source_rank("simplify-newgrad") > source_rank("some-aggregator")
        assert source_rank("unknown-source") == DEFAULT_RANK

    def test_ats_wins(self):
        url, src, changed = prefer_url(
            "https://agg/r?u=1", "speedyapply-swe", "https://gh/jobs/9", "ats"
        )
        assert url == "https://gh/jobs/9" and src == "ats" and changed

    def test_aggregator_cannot_clobber_ats(self):
        url, src, changed = prefer_url(
            "https://gh/jobs/9", "ats", "https://agg/r?u=1", "speedyapply-swe"
        )
        assert url == "https://gh/jobs/9" and src == "ats" and not changed

    def test_equal_precedence_keeps_incumbent(self):
        url, _, changed = prefer_url(
            "https://a/jobs/1", "speedyapply-swe", "https://b/jobs/2", "speedyapply-ai"
        )
        assert url == "https://a/jobs/1" and not changed

    def test_same_source_refreshes(self):
        # A repost from the feed that owns the row: the old req has closed.
        url, _, changed = prefer_url(
            "https://gh/jobs/1", "ats", "https://gh/jobs/2", "ats"
        )
        assert url == "https://gh/jobs/2" and changed

    def test_empty_incumbent_is_filled(self):
        url, src, changed = prefer_url("", "none", "https://gh/jobs/9", "ats")
        assert url == "https://gh/jobs/9" and changed

    def test_empty_new_url_is_ignored(self):
        url, _, changed = prefer_url("https://gh/jobs/9", "ats", "", "ats")
        assert url == "https://gh/jobs/9" and not changed


class TestKnownBlockedDomains:
    """Domains behind permanent bot protection are never checked.

    A warning that fires on every Citadel push is a warning you stop reading -
    and then it fails to work when a link is genuinely dead.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.citadel.com/careers/details/swe-intern-us/",
            "https://www.citadelsecurities.com/careers/details/x/",
            "https://jobs.smartrecruiters.com/RedBull/744000139168339",
            "https://careers.roblox.com/jobs/7992558",
            "https://www.amazon.jobs/jobs/10468069/apply",
        ],
    )
    def test_recognised(self, url):
        from jobpipe.linkcheck import is_known_blocked

        assert is_known_blocked(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/acme/jobs/1",
            "https://jobs.ashbyhq.com/notion/uuid",
            "https://jobs.lever.co/palantir/uuid",
        ],
    )
    def test_normal_domains_are_not_blocked(self, url):
        from jobpipe.linkcheck import is_known_blocked

        assert is_known_blocked(url) is False

    def test_subdomains_are_covered(self):
        from jobpipe.linkcheck import is_known_blocked

        assert is_known_blocked("https://careers.citadel.com/x") is True

    def test_check_short_circuits_without_a_request(self):
        from jobpipe.linkcheck import check

        class ExplodingSession:
            def head(self, *a, **k):
                raise AssertionError("must not make a request to a known-blocked domain")

            get = head

        result = check(
            "https://www.citadel.com/careers/details/x/", session=ExplodingSession()
        )
        assert result.status is LinkStatus.BLOCKED
        assert result.is_expiry_signal is False
        assert "not checked" in result.note

    def test_blocked_never_annotates_a_notification(self):
        """Only real evidence the req is gone should reach the push body."""
        from jobpipe.linkcheck import LinkResult

        assert LinkResult(LinkStatus.BLOCKED).is_expiry_signal is False
        assert LinkResult(LinkStatus.UNREACHABLE).is_expiry_signal is False
