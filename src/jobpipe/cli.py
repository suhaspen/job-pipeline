"""Command line entry point.

Phase 0 exposes inspection and dedupe-debugging commands only. `run` is wired
in Phase 1 once sources exist; it is declared here so the flag surface
(`--dry-run`, `--replay`) is fixed before anything depends on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from jobpipe.config import DEFAULT_COMPANIES, REPO_ROOT, load_config
from jobpipe.index_md import SORT_DATE, SORTS
from jobpipe.models import RawPosting, Status, Tier, utcnow
from jobpipe.normalize import (
    infer_term,
    is_remote,
    normalize_company,
    normalize_location,
    normalize_title,
)
from jobpipe.store import SqliteStore


def _cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        total = len(store.recent(limit=10**6))
        print(f"database        {cfg.db_path}")
        print(f"postings        {total}")
        if total:
            for tier in Tier:
                n = len(store.recent(limit=10**6, tier=tier))
                print(f"  tier {int(tier)}        {n}")
            for status in Status:
                n = len(store.recent(limit=10**6, status=status))
                if n:
                    print(f"  {status.value:<13} {n}")
        print(f"backlog         {store.backlog_unapplied()}")
        last = store.last_new_posting_at()
        print(f"last new        {last.isoformat() if last else 'never'}")
        print(f"pushes (1h)     {store.notifications_since(utcnow() - timedelta(hours=1))}")
        print(f"runs recorded   {len(store.runs())}")

    print(f"companies       {len(cfg.companies)} ({len(cfg.target_companies)} target)")
    print(f"notifications   {'on' if cfg.ntfy_topic else 'unconfigured'}")
    print(f"llm triage      {'on' if cfg.llm_triage_enabled else 'rules-only (no API key)'}")
    print(f"recruiter lookup{'  on' if cfg.recruiter_lookup_enabled else '  unconfigured'}")
    return 0


def _cmd_normalize(args: argparse.Namespace) -> int:
    """Explain how a posting would be keyed. The dedupe debugging tool."""
    posting = RawPosting(
        source="cli",
        company=args.company,
        title=args.title,
        apply_url="",
        location=args.location,
        description=args.description,
        term_default=args.term,
    ).normalize()

    out = {
        "id": posting.id,
        "dedupe_key": posting.dedupe_key,
        "components": {
            "company_norm": normalize_company(args.company),
            "title_norm": normalize_title(args.title),
            "location_norm": normalize_location(args.location),
            "term": infer_term(args.title, args.description, args.term).value,
        },
        "remote": is_remote(args.location),
    }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_recent(args: argparse.Namespace) -> int:
    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        rows = store.recent(
            limit=args.limit,
            tier=Tier(args.tier) if args.tier else None,
            status=Status(args.status) if args.status else None,
        )
    if args.json:
        print(json.dumps([p.as_dict() for p in rows], indent=2))
        return 0
    if not rows:
        print("no postings")
        return 0
    for p in rows:
        age = p.posted_at.strftime("%Y-%m-%d") if p.posted_at else "unknown"
        print(
            f"[t{int(p.tier)}] {p.score:>3}  {p.company[:22]:<22} {p.title[:42]:<42} "
            f"{p.location_norm:<14} {p.term.value:<12} {p.status.value:<9} posted={age}"
        )
    return 0


def backlog_line(count: int) -> str:
    """The unapplied backlog, as a sentence rather than a field.

    "backlog: 23 unapplied" reads as instrumentation and gets skimmed past.
    The count is the one number in the digest that is about the reader rather
    than about the pipeline, so it says what it means and it goes first.
    """
    if count == 0:
        return "Nothing waiting on you."
    if count == 1:
        return "1 posting you haven't decided on."
    return f"{count} postings you haven't decided on."


def _cmd_digest(args: argparse.Namespace) -> int:
    """Daily 07:00 PT digest: everything from the last 24h that did not push."""
    from datetime import timedelta

    from jobpipe.index_md import TERM_HEADING, TERM_ORDER
    from jobpipe.models import Status, Tier, utcnow
    from jobpipe.notify import NtfyClient

    cfg = load_config()
    since = utcnow() - timedelta(hours=args.hours)
    with SqliteStore(cfg.db_path) as store:
        rows = [
            p for p in store.recent(limit=10**6, since=since)
            if p.status not in (Status.EXPIRED, Status.SKIPPED)
        ]
        backlog = store.backlog_unapplied()
        report = (store.runs(since=since) or [{}])[-1]

    by_tier: dict[int, list] = {1: [], 2: [], 3: []}
    for p in rows:
        by_tier[int(p.tier)].append(p)

    # The backlog count leads, in words. Its job is to tell you something, not
    # to suppress anything: tier 2 became digest-only by decision, which left
    # `backpressure` reachable but inert. The mechanism stays where it is,
    # tested, for whenever tier 2 gains a push condition again - deleting it
    # and re-adding it later is how a rule loses the reason it existed. In the
    # meantime the number itself is the pressure.
    lines = [backlog_line(backlog), "", f"{len(rows)} new in the last {args.hours}h"]
    for tier in (1, 2, 3):
        group = sorted(by_tier[tier], key=lambda p: -p.score)
        if not group:
            continue
        label = {1: "TIER 1", 2: "TIER 2", 3: "digest"}[tier]
        lines.append(f"\n{label} ({len(group)})")
        for p in group[: args.limit]:
            lines.append(f"  {p.score:>3} {p.company[:18]} - {p.title[:40]} [{p.term.value}]")
        if len(group) > args.limit:
            lines.append(f"  ...and {len(group) - args.limit} more - see INDEX.md")

    notif = report.get("notifications") or {}
    suppressed = sum(v for k, v in notif.items() if k.startswith("suppressed"))
    if suppressed:
        lines.append(
            f"\nsuppressed: {notif.get('suppressed_rate_cap', 0)} rate-cap, "
            f"{notif.get('suppressed_quiet_hours', 0)} quiet-hours, "
            f"{notif.get('suppressed_backpressure', 0)} backpressure"
        )
    bad = [p for p in rows if p.link_status in ("dead", "redirected_to_index")]
    if bad:
        lines.append(f"{len(bad)} posting(s) with a dead or index-redirected link")
    lines.append("\nMark applied: jobpipe applied <id>")

    body = "\n".join(lines)
    if args.stdout or not cfg.ntfy_topic:
        print(body)
        return 0
    NtfyClient(cfg).send_text(
        # The title is what shows on a locked phone, so the backlog goes there
        # too - it is the part that is about you rather than about the feed.
        f"{len(rows)} new · {backlog} undecided", body, priority=2, tags=["newspaper"]
    )
    print("digest sent")
    return 0


def _cmd_migrate_levels(args: argparse.Namespace) -> int:
    """Re-key the corpus after splitting numeric levels out of the dedupe key."""
    from jobpipe.migrate import migrate

    cfg = load_config()
    csv_path = REPO_ROOT / "data" / "backlog-review.csv"
    if not csv_path.exists():
        print(f"STOP: {csv_path} is missing.", file=sys.stderr)
        print("Baseline ids cannot be re-derived without it - the baseline stores",
              file=sys.stderr)
        print("ids only. Restore the file before migrating.", file=sys.stderr)
        return 2

    with SqliteStore(cfg.db_path) as store:
        report = migrate(store, csv_path, write=args.write)

    print(f"{'APPLIED' if args.write else 'DRY RUN'}\n")
    print(f"  csv rows read              {report.csv_rows}")
    print(f"  distinct ids from csv      {len(report.csv_ids)}")
    print(f"  suppression ids carried    {len(report.suppression_ids)}")
    print(f"  old baseline               {report.old_baseline}")
    print(f"  old ids not re-derivable   {report.retained_unmapped}  (retained, not notified)")
    print(f"  new baseline               {report.new_baseline}")
    print(f"  delta                      {report.delta:+d}")
    print(f"  live postings re-keyed     {report.postings_id_changed} of {report.postings_remapped}")

    if report.collisions:
        print("\nABORTED - a finer key can only split rows, never merge them:", file=sys.stderr)
        for c in report.collisions[:10]:
            print(f"    {c}", file=sys.stderr)
        return 1
    for e in report.errors[:10]:
        print(f"  error: {e}", file=sys.stderr)

    if report.new_baseline < report.old_baseline:
        print("\nWARNING: baseline shrank. Some ids would stop suppressing.", file=sys.stderr)
        return 1
    if not args.write:
        print("\nRe-run with --write to apply.")
    return 0


def _cmd_audit_exclusions(args: argparse.Namespace) -> int:
    """Random sample of what the eligibility gate rejected.

    Exists so the false-negative rate is measurable: otherwise the only
    evidence a filter rule is wrong is exactly the data the rule discarded.
    """
    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        counts = store.exclusion_counts()
        rows = store.sample_exclusions(args.sample, reason=args.reason)

    total = sum(counts.values())
    print(f"{total} excluded postings on record (14-day window)\n")
    for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:<30} {n:>6}  ({n / total:.0%})" if total else f"  {reason}")
    if not rows:
        print("\nnothing to sample.")
        return 0

    print(f"\n=== random sample of {len(rows)} ===")
    print("Scan for anything you would actually have applied to.\n")
    for r in rows:
        print(f"  [{r['filter_reason']:<26}] {(r['company'] or '')[:20]:<20} {r['title'][:52]}")
        if args.urls and r.get("apply_url"):
            print(f"        {r['apply_url']}")
    return 0


def _cmd_audit_suppressions(args: argparse.Namespace) -> int:
    """Show what the cutover baseline swallowed.

    The one failure mode that is otherwise unmeasurable: a genuinely new
    posting normalizing onto a baselined id disappears with no row, no push
    and no log, looking exactly like a quiet day.
    """
    from datetime import timedelta

    from jobpipe.models import utcnow

    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        total = store.suppression_count()
        since = utcnow() - timedelta(days=args.days)
        rows = store.recent_suppressions(limit=args.sample, since=since)
        collapse = store.suppression_collapse(limit=args.top)

    print(f"{total} distinct (baseline id, title, source) suppressions on record")
    print(f"showing {len(rows)} from the last {args.days} day(s)\n")

    if not rows:
        print("nothing suppressed in that window.")
    else:
        print(f"{'seen':>5}  {'source':<18} {'company':<22} {'title':<44} baseline")
        print("-" * 118)
        for r in rows:
            print(
                f"{r['times_seen']:>5}  {r['source'][:18]:<18} "
                f"{(r['company'] or '')[:22]:<22} {r['title'][:44]:<44} {r['baseline_id']}"
            )

    print(f"\n=== over-collapse check: top {args.top} baseline ids by distinct titles ===")
    print("An id absorbing several DIFFERENT titles means the dedupe key is too")
    print("coarse. Repeats of one title are just the same job seen every run.\n")
    multi = [c for c in collapse if c["n_titles"] > 1]
    if not multi:
        print("  no baseline id has absorbed more than one distinct title.")
    for c in collapse[: args.top]:
        marker = "  <-- REVIEW" if c["n_titles"] > 1 else ""
        print(f"  {c['baseline_id']}  titles={c['n_titles']}  hits={c['hits']}{marker}")
        if c["n_titles"] > 1:
            for title in (c["titles"] or "").split(",")[:6]:
                print(f"        {title[:88]}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """Show live postings, optionally filtered by term."""
    from jobpipe.index_md import TERM_HEADING, TERM_ORDER, sort_key
    from jobpipe.models import Term
    from jobpipe.notify.ntfy import posted_age

    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        rows = store.recent(limit=10**6)

    hidden = {Status.EXPIRED, Status.SKIPPED} if not args.all else set()
    rows = [p for p in rows if p.status not in hidden]
    if args.term:
        rows = [p for p in rows if p.term.value == args.term]
    if args.tier:
        rows = [p for p in rows if int(p.tier) == args.tier]

    if not rows:
        print("no live postings" + (f" for term {args.term}" if args.term else ""))
        return 0

    by_term: dict[Term, list] = {}
    for p in rows:
        by_term.setdefault(p.term, []).append(p)

    # Term grouping survives both sorts: off-cycle co-ops are scarce enough to
    # stay above new grad whatever the ordering inside each group is.
    key = sort_key(args.sort)
    for term in TERM_ORDER:
        group = by_term.get(term)
        if not group:
            continue
        group.sort(key=key, reverse=True)
        print(f"\n{TERM_HEADING[term]}  ({len(group)})")
        print("-" * 108)
        for p in group:
            flag = {"ok": " ", "redirected_to_index": "!", "dead": "X"}.get(p.link_status, "?")
            posted = p.posted_at.strftime("%Y-%m-%d") if p.posted_at else "    -     "
            print(
                f" {flag} t{int(p.tier)} {p.score:>3}  {p.company[:20]:<20} "
                f"{p.title[:44]:<44} {p.location_norm[:12]:<12} "
                f"{posted}  {posted_age(p):<12} {p.status.value}"
            )
            if args.urls:
                print(f"        {p.apply_url}")
    print(
        f"\n{len(rows)} posting(s), sorted by {args.sort}.  "
        f"! = link resolves to an index, X = dead link"
    )
    return 0


def _sheets_or_explain():
    """(client, cfg) or (None, cfg) with the reason already printed."""
    cfg = load_config()
    if not cfg.sheets_mirror_enabled:
        missing = [
            name for name, value in
            (("GOOGLE_SHEET_ID", cfg.sheet_id), ("GOOGLE_SA_KEY", cfg.sheet_key))
            if not value
        ]
        print(f"sheets mirror is off: {', '.join(missing)} unset", file=sys.stderr)
        return None, cfg
    from jobpipe.sheets import SheetsClient

    return SheetsClient(cfg.sheet_id, cfg.sheet_key), cfg


def _cmd_sheets_doctor(args: argparse.Namespace) -> int:
    """Read-only. Answers the two questions that actually go wrong: is the key
    readable, and has the sheet been shared with the service account."""
    from jobpipe.sheets import SheetsError
    from jobpipe.sheets.mirror import LIVE, LIVE_HEADERS, check_headers

    client, cfg = _sheets_or_explain()
    if client is None:
        return 1
    try:
        print(f"service account : {client.service_account_email}")
    except SheetsError as exc:
        print(f"key             : UNREADABLE - {exc}", file=sys.stderr)
        return 1
    try:
        tabs = client.tabs()
    except SheetsError as exc:
        print(f"spreadsheet     : UNREACHABLE - {exc}", file=sys.stderr)
        print(
            "\nIf this says PERMISSION_DENIED, share the sheet with the service "
            "account address above (Editor).",
            file=sys.stderr,
        )
        return 1
    print(f"spreadsheet     : ok, tabs {sorted(tabs)}")
    try:
        check_headers(client.read(f"'{LIVE}'!A1:H1"))
        print(f"columns A-H     : ok, {LIVE_HEADERS}")
    except SheetsError as exc:
        print(f"columns A-H     : {exc}", file=sys.stderr)
        return 1
    statuses = client.read(f"'{LIVE}'!I2:I")
    print(f"your column I   : {sum(1 for r in statuses if r and r[0].strip())} set")
    return 0


def _cmd_sheets_setup(args: argparse.Namespace) -> int:
    from jobpipe.sheets.setup import setup, status_legend

    client, cfg = _sheets_or_explain()
    if client is None:
        return 1
    result = setup(client)
    print(json.dumps(result, indent=2))
    print(f"\nStatus column (I) understands: {status_legend()}")
    print("Anything else there is left alone. Columns I onward are yours.")
    return 0


def _cmd_sheets_sync(args: argparse.Namespace) -> int:
    from jobpipe.sheets import apply_statuses, read_statuses, sync_live, sync_stats

    client, cfg = _sheets_or_explain()
    if client is None:
        return 1
    with SqliteStore(cfg.db_path) as store:
        live = [p for p in store.recent(limit=10**6) if p.status is not Status.EXPIRED]
        statuses, source = read_statuses(client, cfg.sheet_status_cache)
        changed = apply_statuses(store, statuses)
        counts = sync_live(client, live)
        sync_stats(client, live, statuses)
    print(json.dumps({**counts, "read": len(statuses), "read_from": source,
                      "statuses_applied": changed}, indent=2))
    return 0


def _cmd_sheets_import_backlog(args: argparse.Namespace) -> int:
    from jobpipe.config import BACKLOG_CSV_PATH
    from jobpipe.sheets.backlog import TERM_RANK, order, read_csv

    path = args.csv or BACKLOG_CSV_PATH
    rows = order(read_csv(path))
    off_cycle = [r for r in rows if TERM_RANK.get((r.get("term") or "").strip(), 9) <= 2]
    print(f"{len(rows)} rows, {len(off_cycle)} off-cycle, which sort first:")
    for row in off_cycle[:10]:
        print(f"  {row['term']:<13} {row['score']:>3}  {row['company'][:28]:<28} {row['title'][:44]}")
    if not args.write:
        print("\ndry run. re-run with --write to load the Backlog tab.")
        return 0

    from jobpipe.sheets.backlog import import_backlog

    client, cfg = _sheets_or_explain()
    if client is None:
        return 1
    print(json.dumps(import_backlog(client, path), indent=2))
    return 0


def _cmd_applied(args: argparse.Namespace) -> int:
    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        ok = store.set_status(args.posting_id, Status.APPLIED)
    print("marked applied" if ok else f"no posting with id {args.posting_id}", file=sys.stderr)
    return 0 if ok else 1


def _cmd_init_topic(args: argparse.Namespace) -> int:
    """Generate an unguessable ntfy topic and write it to .env.

    ntfy topics are public: anyone who knows the name reads every message.
    The topic is therefore a bearer secret, printed here only redacted.
    """
    from jobpipe.notify import generate_topic, redact

    topic = generate_topic()
    env = REPO_ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    rotating = any(ln.startswith("NTFY_TOPIC=") for ln in lines)
    lines = [ln for ln in lines if not ln.startswith(("NTFY_TOPIC=", "NTFY_ACK_TOPIC="))]
    lines.append(f"NTFY_TOPIC={topic}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if rotating:
        print("ROTATED. The previous topic is now dead to this pipeline.")
        print("Unsubscribe from it on your phone - anyone holding it can still")
        print("read whatever was already published there.")
        print()
    print(f"wrote NTFY_TOPIC to {env} (gitignored)")
    print(f"topic (redacted): {redact(topic)}")
    print()
    print("Subscribe on your phone:")
    print("  1. install ntfy (App Store / Play Store / F-Droid)")
    print("  2. Subscribe to topic -> paste the value of NTFY_TOPIC from .env")
    print()
    print("Treat that string like a password: anyone who has it reads your alerts.")
    return 0


def _cmd_notify_test(args: argparse.Namespace) -> int:
    """Fire one synthetic push through the real client and payload builder."""
    from datetime import timedelta

    from jobpipe.models import Posting, Term, Tier, utcnow
    from jobpipe.notify import NtfyClient, is_quiet_hours, redact

    cfg = load_config()
    if not cfg.ntfy_topic:
        print("NTFY_TOPIC is unset. Run: jobpipe init-topic", file=sys.stderr)
        return 2

    now = utcnow()
    # Prefer a real stored posting: a synthetic fixture can carry a URL that
    # does not exist, which is exactly how the first test push ended up
    # pointing at a careers index instead of a req.
    with SqliteStore(cfg.db_path) as store:
        real = store.recent(limit=1)
    if real:
        synthetic = real[0]
        synthetic.score_rationale = f"TEST PUSH - {synthetic.score_rationale}"
        print("using a real stored posting")
    else:
        synthetic = Posting(
            id="notify-test", dedupe_key="test|test|test|test",
            company="Cloudflare", title="Software Engineer Intern (Fall 2026)",
            term=Term.FALL_2026, location="Austin, TX", remote=False,
            # A real req URL, with a job id. Verified live.
            apply_url="https://boards.greenhouse.io/cloudflare/jobs/8052785?gh_jid=8052785",
            source="notify-test", first_seen_at=now, last_seen_at=now,
            posted_at=now - timedelta(days=2), tier=Tier.INTERRUPTING, score=88,
            score_rationale="synthetic test push - confirms end-to-end delivery",
            location_norm="austin",
        )
        print("no stored postings yet; using a synthetic one with a real req URL")

    client = NtfyClient(cfg)
    print(f"server  {cfg.ntfy_server}")
    print(f"topic   {redact(cfg.ntfy_topic)}")
    quiet = is_quiet_hours(now)
    print(f"quiet hours right now: {quiet}"
          + ("  (a real tier-1 would be downgraded to silent)" if quiet else ""))
    try:
        client.send_posting(synthetic, priority=5)
    except Exception as exc:
        print(f"send FAILED: {exc}", file=sys.stderr)
        return 1
    print("\nsent. check your phone.")
    return 0


def _cmd_cutover(args: argparse.Namespace) -> int:
    """Collapse the current working set to baseline and start forward-only.

    Order matters and is enforced here: export first, then baseline, then set
    the date. Reversing it would lose the review artifact.
    """
    import csv

    from jobpipe.config import CUTOVER_DATE_PATH, write_cutover_date
    from jobpipe.models import utcnow

    cfg = load_config()
    when = utcnow()
    with SqliteStore(cfg.db_path) as store:
        rows = store.recent(limit=10**6)
        if not rows:
            print("no postings to cut over", file=sys.stderr)
            return 1

        # 1. export for one-time manual review (gitignored, throwaway)
        out = REPO_ROOT / "data" / "backlog-review.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        term_order = {
            "fall-2026": 0, "winter-2027": 1, "spring-2027": 2,
            "new-grad": 3, "summer-2027": 4, "unknown": 5,
        }
        rows.sort(key=lambda p: (term_order.get(p.term.value, 9), -p.score, p.company.lower()))
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "term", "score", "tier", "company", "title", "location",
                "remote", "posted_at", "source", "apply_url", "disqualifiers",
            ])
            for p in rows:
                w.writerow([
                    p.term.value, p.score, int(p.tier), p.company, p.title,
                    p.location_norm, int(p.remote),
                    p.posted_at.date().isoformat() if p.posted_at else "",
                    p.source, p.apply_url,
                    "|".join(d.value for d in p.disqualifiers),
                ])
        print(f"1. exported {len(rows)} postings -> {out}")
        by_term: dict[str, int] = {}
        for p in rows:
            by_term[p.term.value] = by_term.get(p.term.value, 0) + 1
        for term, n in sorted(by_term.items(), key=lambda kv: term_order.get(kv[0], 9)):
            print(f"     {term:<14} {n}")

        # 2. collapse to baseline
        ids = [p.id for p in rows]
        store.seed_baseline(ids, seeded_at=when)
        store.conn.execute("DELETE FROM postings")
        store.conn.execute("DELETE FROM sightings")
        store.vacuum()
        print(f"2. collapsed {len(ids)} ids to baseline; postings table emptied")

        # 3. record the cutover instant
        write_cutover_date(when)
        print(f"3. cutover date set -> {CUTOVER_DATE_PATH.name} = {when.isoformat()}")

    print("\nFrom now on only postings first seen after that instant are stored.")
    print(f"Review {out.name} before it is deleted - it is gitignored and not recoverable.")
    return 0


def _cmd_verify_companies(args: argparse.Namespace) -> int:
    """Probe every ATS board and report which tokens are live.

    Seeded tokens are conventional-slug guesses. Companies migrate ATS vendors
    and rename their boards, so this is the tool that turns the config file
    from a guess into a checked list.
    """
    from jobpipe.sources import ATSSource, HttpClient

    cfg = load_config()
    http = HttpClient(store=None, timeout=20.0, max_retries=2)
    source = ATSSource(http, cfg.companies, delay=args.delay)
    source.fetch()

    live, dead, empty, other = [], [], [], []
    for company in cfg.companies:
        result = source.results.get(company.name, "no-result")
        if result.startswith("ok:"):
            live.append((company, int(result.split(":")[1])))
        elif result in ("404", "403", "410"):
            dead.append((company, result))
        elif result == "empty":
            empty.append((company, result))
        else:
            other.append((company, result))

    for label, group in (("LIVE", live), ("EMPTY BOARD", empty), ("DEAD", dead), ("OTHER", other)):
        if not group:
            continue
        print(f"\n{label} ({len(group)})")
        for company, detail in sorted(group, key=lambda x: x[0].name.lower()):
            print(f"  {company.name:<24} {company.ats:<11} {company.token:<22} {detail}")

    total = len(cfg.companies)
    print(f"\n{len(live)}/{total} live, {len(empty)} empty, {len(dead)} dead, {len(other)} other")

    if args.write:
        import json as _json

        path = DEFAULT_COMPANIES
        data = _json.loads(path.read_text(encoding="utf-8"))
        live_names = {c.name for c, _ in live}
        empty_names = {c.name for c, _ in empty}
        dead_names = {c.name for c, _ in dead}
        kept = []
        for entry in data["companies"]:
            name = entry["name"]
            if name in dead_names:
                # Dropped rather than kept-and-disabled: a dead token costs a
                # request every run forever and can never return data.
                continue
            entry["verified"] = name in live_names
            if name in empty_names:
                entry["note"] = "board reachable but currently lists no jobs"
            kept.append(entry)
        data["companies"] = kept
        path.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path} - {len(dead_names)} dead entries removed")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from jobpipe.runner import run

    cfg = load_config(dry_run=args.dry_run)
    if args.replay:
        print("--replay is not wired yet; it lands with triage in Phase 3.", file=sys.stderr)
        return 2

    report = run(cfg, only=args.source)

    label = " (dry run)" if cfg.dry_run else ""
    print(f"run {report.run_id}{label}  {report.duration_s:.1f}s")
    print()
    header = f"{'source':<20} {'ok':<4} {'fetched':>8} {'new':>6} {'filtered':>9} {'ms':>7}"
    print(header)
    print("-" * len(header))
    for s in report.sources:
        flag = "304" if s.not_modified else ("ok" if s.ok else "FAIL")
        print(
            f"{s.name:<20} {flag:<4} {s.fetched:>8} {s.new:>6} "
            f"{s.filtered_out:>9} {s.latency_ms:>7}"
        )
    print("-" * len(header))
    print(
        f"{'total':<20} {'':<4} {report.total_fetched:>8} {report.total_new:>6} "
        f"{sum(s.filtered_out for s in report.sources):>9}"
    )
    print()
    print(f"deduped out     {report.deduped_out}")
    print(f"backlog         {report.backlog_unapplied}")

    for s in report.sources:
        for w in s.warnings:
            print(f"  warn  [{s.name}] {w}", file=sys.stderr)
        for e in s.errors:
            print(f"  ERROR [{s.name}] {e}", file=sys.stderr)
    for w in report.warnings:
        print(f"  warn  {w}", file=sys.stderr)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobpipe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch, dedupe, triage, notify (Phase 1+)")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full pipeline without writing to the store or pushing",
    )
    run.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="re-run triage against stored raw payloads without refetching",
    )
    run.add_argument("--source", action="append", help="limit to named source(s)")
    run.set_defaults(func=_cmd_run)

    stats = sub.add_parser("stats", help="database and configuration summary")
    stats.set_defaults(func=_cmd_stats)

    norm = sub.add_parser("normalize", help="show how a posting would be deduped")
    norm.add_argument("--company", required=True)
    norm.add_argument("--title", required=True)
    norm.add_argument("--location", default=None)
    norm.add_argument("--description", default=None)
    norm.add_argument("--term", default=None, help="source-level fallback term when the text says nothing")
    norm.set_defaults(func=_cmd_normalize)

    recent = sub.add_parser("recent", help="list stored postings")
    recent.add_argument("--limit", type=int, default=25)
    recent.add_argument("--tier", type=int, choices=[1, 2, 3])
    recent.add_argument("--status", choices=[s.value for s in Status])
    recent.add_argument("--json", action="store_true")
    recent.set_defaults(func=_cmd_recent)

    verify = sub.add_parser("verify-companies", help="probe every ATS board token")
    verify.add_argument("--write", action="store_true", help="update companies.json in place")
    verify.add_argument("--delay", type=float, default=0.15, help="seconds between requests")
    verify.set_defaults(func=_cmd_verify_companies)

    sub.add_parser("init-topic", help="generate an ntfy topic into .env").set_defaults(
        func=_cmd_init_topic
    )
    sub.add_parser(
        "notify-test", help="fire one synthetic push through the real path"
    ).set_defaults(func=_cmd_notify_test)
    sub.add_parser(
        "cutover", help="export backlog, collapse to baseline, start forward-only"
    ).set_defaults(func=_cmd_cutover)

    dig = sub.add_parser("digest", help="send the daily digest")
    dig.add_argument("--hours", type=int, default=24)
    dig.add_argument("--limit", type=int, default=12)
    dig.add_argument("--stdout", action="store_true", help="print instead of pushing")
    dig.set_defaults(func=_cmd_digest)

    mig = sub.add_parser("migrate-levels", help="re-key after splitting numeric levels")
    mig.add_argument("--write", action="store_true", help="apply (default is a dry run)")
    mig.set_defaults(func=_cmd_migrate_levels)

    exc = sub.add_parser(
        "audit-exclusions", help="sample what the eligibility gate rejected"
    )
    exc.add_argument("--sample", type=int, default=20)
    exc.add_argument("--reason", help="filter to one filter_reason")
    exc.add_argument("--urls", action="store_true")
    exc.set_defaults(func=_cmd_audit_exclusions)

    sup = sub.add_parser(
        "audit-suppressions", help="show what the cutover baseline swallowed"
    )
    sup.add_argument("--days", type=int, default=30)
    sup.add_argument("--sample", type=int, default=40)
    sup.add_argument("--top", type=int, default=20)
    sup.set_defaults(func=_cmd_audit_suppressions)

    lst = sub.add_parser("list", help="show live postings grouped by term")
    lst.add_argument("--term", choices=[
        "fall-2026", "winter-2027", "spring-2027", "summer-2027", "new-grad", "unknown",
    ])
    lst.add_argument("--tier", type=int, choices=[1, 2, 3])
    lst.add_argument(
        "--sort", choices=list(SORTS), default=SORT_DATE,
        help="date: newest first, score as tiebreak (default). score: the reverse.",
    )
    lst.add_argument("--urls", action="store_true", help="print apply URLs")
    lst.add_argument("--all", action="store_true", help="include expired and skipped")
    lst.set_defaults(func=_cmd_list)

    applied = sub.add_parser("applied", help="mark a posting as applied")
    applied.add_argument("posting_id")
    applied.set_defaults(func=_cmd_applied)

    sheets = sub.add_parser("sheets", help="Google Sheets mirror")
    sheets_sub = sheets.add_subparsers(dest="sheets_cmd", required=True)
    sheets_sub.add_parser(
        "doctor", help="check credentials and column ownership without writing"
    ).set_defaults(func=_cmd_sheets_doctor)
    sheets_sub.add_parser(
        "setup", help="create tabs, headers and conditional formatting (idempotent)"
    ).set_defaults(func=_cmd_sheets_setup)
    sheets_sub.add_parser(
        "sync", help="push columns A-H and refresh Stats"
    ).set_defaults(func=_cmd_sheets_sync)
    imp = sheets_sub.add_parser(
        "import-backlog",
        help="one-time load of data/backlog-review.csv, off-cycle first (local only)",
    )
    imp.add_argument("--csv", type=Path, default=None)
    imp.add_argument(
        "--write", action="store_true", help="apply (default prints what would happen)"
    )
    imp.set_defaults(func=_cmd_sheets_import_backlog)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
