"""The schedule is a billing decision, so it gets asserted like one.

Two cron entries that match the same minute do not merge - GitHub fires the
workflow once per entry, the concurrency group queues the second, and the
month's minute budget quietly pays for both. That failure is invisible in the
YAML and invisible in a green run, which is exactly the kind that needs a test.
"""

from __future__ import annotations

import subprocess
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
        # Per weekday: 16 at */15 across 08:00-11:59, 18 at */30 across
        # 06:00-07:59 and 12:00-18:59, 1 at 19:05, 2 off-window. Plus 6 on each
        # weekend day. Change this number and change the projection in
        # HANDOFF.md and docs/operations.md - it is what bounds the budget.
        assert len(fire_times(poll_crons)) == (16 + 18 + 1 + 2) * 5 + 6 * 2

    def test_the_peak_band_polls_every_15_minutes(self, poll_crons):
        """09:00-13:00 local - measured, not assumed.

        GitHub delivers about half of what is scheduled, so 15 minutes asked
        for is ~30 minutes received - which is the cadence this system was
        designed around in the first place.
        """
        monday = [w for w, _ in fire_times(poll_crons, days=1)]
        peak = [w for w in monday if 9 <= w.hour < 13]
        assert len(peak) == 16
        assert {w.minute for w in peak} == {5, 20, 35, 50}

    def test_the_rest_of_the_working_day_polls_every_30_minutes(self, poll_crons):
        monday = [w for w, _ in fire_times(poll_crons, days=1)]
        shoulder = [
            w for w in monday
            if 6 <= w.hour < 9 or 13 <= w.hour < 19 or (w.hour == 19 and w.minute == 5)
        ]
        assert len(shoulder) == 19
        assert shoulder[0].hour == 6 and shoulder[0].minute == 5
        assert shoulder[-1].hour == 19 and shoulder[-1].minute == 5
        # :05 and :35, offset from Simplify's :01/:31 publish cadence. Moving
        # these back to the hour re-introduces a full cycle of latency on the
        # largest feed whenever GitHub happens to be punctual.
        assert {w.minute for w in shoulder} == {5, 35}

    def test_the_peak_band_never_overlaps_the_shoulder_band(self, poll_crons):
        """Both bands use :05 and :35, so they are kept apart by hour alone.
        An overlapping hour would put two entries on the same minute and bill
        a duplicate run - the failure this whole file exists to catch."""
        monday = [w for w, _ in fire_times(poll_crons, days=1)]
        assert len(monday) == len({(w.hour, w.minute) for w in monday})

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
            cfg.INDEX_PATH, cfg.INDEX_BY_SCORE_PATH, cfg.SHEET_STATUS_CACHE,
            cfg.AUDIT_DIR,
        ]
        for path in regenerated:
            rel = path.relative_to(cfg.REPO_ROOT).as_posix()
            assert rel in commit, f"{rel} is regenerated every run but never staged"


class TestSheetsSecrets:
    def test_the_poll_passes_both_sheets_secrets_or_neither(self):
        """One without the other silently disables the mirror with no error -
        `sheets_mirror_enabled` needs both."""
        wf = load("poll.yml")
        step = [s for s in wf["jobs"]["poll"]["steps"] if s.get("name") == "Run pipeline"][0]
        env = step["env"]
        assert ("GOOGLE_SHEET_ID" in env) == ("GOOGLE_SA_KEY" in env)

    def test_the_service_account_key_is_never_written_to_a_file(self):
        """It goes in as an env var and stays there. A key echoed into a file
        in the workspace is one `git add -A` away from being committed."""
        for name in ("poll.yml", "digest.yml", "keepalive.yml"):
            body = (WORKFLOWS / name).read_text()
            assert "GOOGLE_SA_KEY" not in body or ">" not in body.split("GOOGLE_SA_KEY")[1][:80]


class TestStagingIsTolerantOfAbsentOutputs:
    """`git add` aborts the whole step on a pathspec that matches nothing.

    It does not skip the missing path and carry on. Listing
    `data/sheet-status.json` unconditionally - a file that only exists once the
    Sheets mirror is configured - failed every run for four hours while the
    pipeline itself was completing fine and the healthcheck stayed green.

    This runs the real staging script against a repo where the optional
    outputs are missing, because reading it was not enough to catch it.
    """

    def _staging_script(self) -> str:
        wf = load("poll.yml")
        step = [
            s for s in wf["jobs"]["poll"]["steps"]
            if "git add" in (s.get("run") or "")
        ][0]["run"]
        # Everything up to the change check; the commit and push need a remote.
        return step.split("# An all-304")[0]

    def _repo(self, tmp_path, present: list[str]):
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        for rel in present:
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n")
        return tmp_path

    def _run(self, cwd) -> subprocess.CompletedProcess:
        import subprocess

        return subprocess.run(
            ["bash", "-e", "-c", self._staging_script()],
            cwd=cwd, capture_output=True, text=True,
        )

    def test_succeeds_when_the_optional_outputs_are_absent(self, tmp_path):
        """The exact shape of the outage: no sheet-status.json, no audit dir."""
        repo = self._repo(tmp_path, [
            "data/postings.jsonl", "data/baseline.txt", "data/run-report.json",
            "INDEX.md", "INDEX-by-score.md",
        ])
        result = self._run(repo)
        assert result.returncode == 0, result.stderr

    def test_stages_everything_that_is_present(self, tmp_path):
        import subprocess

        files = [
            "data/postings.jsonl", "data/baseline.txt", "data/run-report.json",
            "data/sheet-status.json", "data/audit/2026-08-07.jsonl",
            "INDEX.md", "INDEX-by-score.md",
        ]
        repo = self._repo(tmp_path, files)
        assert self._run(repo).returncode == 0
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True,
        ).stdout.split()
        assert sorted(staged) == sorted(files)

    def test_succeeds_when_nothing_at_all_was_produced(self, tmp_path):
        assert self._run(self._repo(tmp_path, [])).returncode == 0


class TestFailureAlarm:
    def test_a_red_workflow_pings_the_healthcheck(self):
        """The ping inside `jobpipe run` answers "did the pipeline run", not
        "did the workflow succeed" - so a failing commit step left
        healthchecks.io green for four hours."""
        wf = load("poll.yml")
        step = [s for s in wf["jobs"]["poll"]["steps"] if s.get("name") == "Alarm on failure"][0]
        assert step["if"] == "failure()"
        assert "/fail" in step["run"]

    def test_the_secret_goes_through_env_not_the_condition(self):
        """The `secrets` context is not dependable in a step-level `if:`."""
        wf = load("poll.yml")
        step = [s for s in wf["jobs"]["poll"]["steps"] if s.get("name") == "Alarm on failure"][0]
        assert "secrets" not in step["if"]
        assert "HEALTHCHECK_URL" in step["env"]
