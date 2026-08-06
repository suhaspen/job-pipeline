"""JSON-lines structured logging.

One file per UTC day in `logs/`, plus a human-readable line on stderr. Every
record carries the `run_id` so a run can be reconstructed after the fact from
the CI artifact alone.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobpipe.config import LOG_DIR


class RunLogger:
    def __init__(self, run_id: str, log_dir: Path | None = None, *, echo: bool = True):
        self.run_id = run_id
        self.echo = echo
        self.log_dir = log_dir or LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.path = self.log_dir / f"{day}.jsonl"

    def _write(self, level: str, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "level": level,
            "event": event,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        if self.echo:
            detail = " ".join(f"{k}={v}" for k, v in fields.items() if k != "traceback")
            print(f"[{level:<5}] {event} {detail}".rstrip(), file=sys.stderr)

    def debug(self, event: str, **f: Any) -> None:
        self._write("debug", event, **f)

    def info(self, event: str, **f: Any) -> None:
        self._write("info", event, **f)

    def warn(self, event: str, **f: Any) -> None:
        self._write("warn", event, **f)

    def error(self, event: str, **f: Any) -> None:
        self._write("error", event, **f)
