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

from jobpipe.config import load_config
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
        term_hint=args.term,
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


def _cmd_applied(args: argparse.Namespace) -> int:
    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        ok = store.set_status(args.posting_id, Status.APPLIED)
    print("marked applied" if ok else f"no posting with id {args.posting_id}", file=sys.stderr)
    return 0 if ok else 1


def _cmd_run(args: argparse.Namespace) -> int:
    print(
        "`run` is not wired yet - sources land in Phase 1.\n"
        "Phase 0 ships the store, normalization and dedupe; see FEEDBACK.md.",
        file=sys.stderr,
    )
    return 2


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
    norm.add_argument("--term", default=None, help="override the inferred term")
    norm.set_defaults(func=_cmd_normalize)

    recent = sub.add_parser("recent", help="list stored postings")
    recent.add_argument("--limit", type=int, default=25)
    recent.add_argument("--tier", type=int, choices=[1, 2, 3])
    recent.add_argument("--status", choices=[s.value for s in Status])
    recent.add_argument("--json", action="store_true")
    recent.set_defaults(func=_cmd_recent)

    applied = sub.add_parser("applied", help="mark a posting as applied")
    applied.add_argument("posting_id")
    applied.set_defaults(func=_cmd_applied)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
