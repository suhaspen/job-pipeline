"""The schedule is a billing decision, so it gets asserted like one.

Two cron entries that match the same minute do not merge - GitHub fires the
workflow once per entry, the concurrency group queues the second, and the
month's minute budget quietly pays for both. That failure is invisible in the
YAML and invisible in a green run, which is exactly the kind that needs a test.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# YAML 1.1 reads a bare `on:` as the boolean true, so the trigger block is not
# under the string key you wrote.
ON_KEY = True

# Actions still shipping a Node 20 entrypoint. Node 20 leaves the runner in
# September 2026; anything pinned below these majors is a countdown.
MIN_MAJORS = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/upload-artifact": 6,
    "actions/cache": 5,
}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def schedules(wf: dict) -> list[dict]:
    return wf[ON_KEY]["schedule"]


def _field_matches(spec: str, value: int, lo: int, hi: int) -> bool:
    """Enough cron for the subset these workflows use: `*`, `a,b`, `a-b`, `*/n`."""
    for part in spec.split(","):
        if part == "*":
            return True
        if part.startswith("*/"):
            if (value - lo) % int(part[2:]) == 0:
                return True
        elif "-" in part:
            start, end = (int(x) for x in part.split("-"))
            if start <= value <= end:
                return True
        elif int(part) == value:
            return True
    assert lo <= value <= hi
    return False


def matches(cron: str, when: datetime) -> bool:
    minute, hour, dom, month, dow = cron.split()
    # Sunday is 0 in cron; Monday is 0 in datetime.weekday().
    cron_dow = (when.weekday() + 1) % 7
    return (
        _field_matches(minute, when.minute, 0, 59)
        and _field_matches(hour, when.hour, 0, 23)
        and _field_matches(dom, when.day, 1, 31)
        and _field_matches(month, when.month, 1, 12)
        and _field_matches(dow, cron_dow, 0, 6)
    )


def fire_times(crons: list[str], *, days: int = 7) -> list[tuple[datetime, int]]:
    """Every (minute, number-of-entries-matching) over a full week from a Monday."""
    start = datetime(2026, 8, 10)  # a Monday
    out = []
    for offset in range(days * 24 * 60):
        when = start + timedelta(minutes=offset)
        n = sum(1 for c in crons if matches(c, when))
        if n:
            out.append((when, n))
    return out


@pytest.fixture
def poll_crons() -> list[str]:
    return [e["cron"] for e in schedules(load("poll.yml"))]


class TestPollSchedule:
    def test_no_two_entries_fire_in_the_same_minute(self, poll_crons):
        doubled = [w for w, n in fire_times(poll_crons) if n > 1]
        assert doubled == [], (
            f"{len(doubled)} minutes match more than one cron entry, e.g. "
            f"{doubled[:3]}. Each one bills a duplicate run."
        )

    def test_weekly_run_count_is_what_the_budget_assumes(self, poll_crons):
        # 27 in-window + 2 off-window on each of 5 weekdays, 6 on each weekend
        # day. Change this number and change the projection in HANDOFF.md.
        assert len(fire_times(poll_crons)) == 27 * 5 + 2 * 5 + 6 * 2

    def test_the_working_window_polls_every_30_minutes(self, poll_crons):
        monday = [w for w, _ in fire_times(poll_crons, days=1)]
        in_window = [w for w in monday if 6 <= w.hour < 19 or (w.hour == 19 and w.minute == 0)]
        assert len(in_window) == 27
        assert in_window[0].hour == 6 and in_window[0].minute == 0
        assert in_window[-1].hour == 19 and in_window[-1].minute == 0
        assert {w.minute for w in in_window} == {0, 30}

    def test_no_gap_longer_than_four_hours(self, poll_crons):
        times = [w for w, _ in fire_times(poll_crons)]
        # Wrap the week so the Sunday-night to Monday-morning seam is covered.
        gaps = [b - a for a, b in zip(times, times[1:])]
        gaps.append((times[0] + timedelta(days=7)) - times[-1])
        assert max(gaps) <= timedelta(hours=4), f"largest gap is {max(gaps)}"

    def test_every_entry_names_a_timezone(self, poll_crons):
        for entry in schedules(load("poll.yml")):
            assert entry.get("timezone") == "America/Los_Angeles"

    def test_nothing_is_scheduled_in_the_dst_shadow(self, poll_crons):
        """01:00-03:00 local is either doubled or missing on the two DST nights.

        A poll landing there is not a correctness bug, but a heartbeat that
        skips or repeats twice a year is the kind of thing that gets debugged
        for an hour six months later.
        """
        for when, _ in fire_times(poll_crons):
            assert not (1 <= when.hour < 3), f"{when} falls in the DST shadow"


class TestOtherWorkflows:
    def test_digest_fires_at_seven_local(self):
        entries = schedules(load("digest.yml"))
        assert len(entries) == 1
        # The bug this pins: the comment claimed 07:00 America/Los_Angeles
        # while the entry was a bare UTC cron, so it ran at midnight PT.
        assert entries[0]["cron"] == "0 7 * * *"
        assert entries[0]["timezone"] == "America/Los_Angeles"

    def test_keepalive_still_clones_full_history(self):
        """It reads the last commit date; a shallow clone reports the clone."""
        wf = load("keepalive.yml")
        step = wf["jobs"]["keepalive"]["steps"][0]
        assert step["with"]["fetch-depth"] == 0

    def test_poll_clones_shallow(self):
        wf = load("poll.yml")
        step = wf["jobs"]["poll"]["steps"][0]
        assert step["with"]["fetch-depth"] == 1


class TestActionVersions:
    @pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
    def test_no_action_is_pinned_below_its_node24_major(self, name):
        wf = load(name)
        for job in wf["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses")
                if not uses:
                    continue
                action, _, ref = uses.partition("@")
                floor = MIN_MAJORS.get(action)
                if floor is None:
                    continue
                assert ref.startswith("v"), f"{name}: {uses} is not pinned to a major"
                assert int(ref[1:].split(".")[0]) >= floor, (
                    f"{name}: {uses} still ships a Node 20 entrypoint"
                )


class TestCommitCoverage:
    """Every file the pipeline regenerates has to be in the `git add` line.

    A generated file that is written but never staged is invisible: the run
    looks green, the repo never changes, and the omission surfaces as "why is
    this file always stale" weeks later.
    """

    def test_every_regenerated_path_is_staged(self):
        from jobpipe import config as cfg

        wf = load("poll.yml")
        commit = [
            s for s in wf["jobs"]["poll"]["steps"]
            if "git add" in (s.get("run") or "")
        ][0]["run"]
        regenerated = [
            cfg.EXPORT_PATH, cfg.BASELINE_PATH, cfg.RUN_REPORT_PATH,
            cfg.INDEX_PATH, cfg.INDEX_BY_SCORE_PATH,
        ]
        for path in regenerated:
            rel = path.relative_to(cfg.REPO_ROOT).as_posix()
            assert rel in commit, f"{rel} is regenerated every run but never staged"
