#!/usr/bin/env python3
"""Mark any running sync jobs as stopped after an external stop signal."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "local_tools" / "obsidian_sync" / "creators.yaml"


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mark running content sync jobs as stopped.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--reason", default="已停止")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_yaml(project_path(args.config))
    state_path = project_path(str(config.get("state_db", "local_tools/obsidian_sync/state.sqlite")))
    if not state_path.exists():
        print(f"STATE_DB_MISSING {state_path}")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(str(state_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'running' ORDER BY started_at DESC"
        ).fetchall()
        run_ids = [str(row["run_id"]) for row in rows]
        if not run_ids:
            print("NO_RUNNING_RUNS")
            return 0
        placeholders = ",".join("?" for _ in run_ids)
        conn.execute(
            f"""
            UPDATE runs
            SET status = 'stopped',
                current_stage = ?,
                ended_at = ?,
                updated_at = ?
            WHERE run_id IN ({placeholders})
            """,
            [args.reason, now, now, *run_ids],
        )
        conn.execute(
            f"""
            UPDATE run_items
            SET status = 'pending',
                stage = ?,
                error = ?,
                updated_at = ?
            WHERE run_id IN ({placeholders})
              AND status = 'running'
            """,
            [args.reason, args.reason, now, *run_ids],
        )
        conn.commit()
    print(f"MARKED_STOPPED {','.join(run_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
