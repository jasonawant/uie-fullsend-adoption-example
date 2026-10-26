#!/usr/bin/env python3
"""Post idempotent Jira evidence for a PR-sourced review or fix run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jira_uie_gate import GateError, Jira, load_config, post_comment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--stage", required=True, choices=("review", "fix"))
    parser.add_argument("--job-status", required=True, choices=("success", "failure", "cancelled"))
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    try:
        load_config(args.config)
        jira = Jira.from_env()
        body = f"**UIE {args.stage} {args.job_status}.**\n\nPull request: {args.pr_url}\n\nWorkflow: {args.run_url}"
        run_id = args.run_url.rsplit("/", 1)[-1]
        post_comment(jira, args.issue, body, f"uie-{args.stage}-{args.issue}-{run_id}")
        return 0
    except GateError as exc:
        print(f"UIE PR feedback failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
