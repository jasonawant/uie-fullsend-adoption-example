#!/usr/bin/env python3
"""Finalize a UIE Jira stage after a locally dispatched Fullsend harness run."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from jira_uie_gate import CLAIM_LABELS, GateError, Jira, load_config, post_comment


VALID_TRIAGE_ACTIONS = {
    "sufficient", "insufficient", "duplicate", "prerequisites", "in-progress",
    "not-planned", "completed", "split", "question",
}
PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+")


def read_result(output_dir: Path) -> dict[str, Any]:
    for result_file in sorted(output_dir.rglob("agent-result.json")):
        try:
            value = json.loads(result_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def find_pr_url(output_dir: Path, result: dict[str, Any]) -> str | None:
    candidates = [json.dumps(result)]
    for path in output_dir.rglob("*.json"):
        try:
            candidates.append(path.read_text())
        except OSError:
            continue
    for candidate in candidates:
        match = PR_URL.search(candidate)
        if match:
            return match.group(0)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--stage", required=True, choices=tuple(CLAIM_LABELS))
    parser.add_argument("--job-status", required=True, choices=("success", "failure", "cancelled"))
    parser.add_argument("--skipped", default="")
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--correlation-output", type=Path)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        for name in ("JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_TOKEN"):
            if not os.environ.get(name):
                raise GateError(f"{name} is required")
        jira = Jira(os.environ["JIRA_BASE_URL"], os.environ["JIRA_USER_EMAIL"], os.environ["JIRA_TOKEN"])
        claim = jira.property(args.issue, cfg["jira"]["claim_property"])
        if not isinstance(claim, dict) or claim.get("stage") != args.stage:
            raise GateError(f"no matching active {args.stage} claim exists")
        result = read_result(args.output_dir)
        status = args.job_status
        action = result.get("action") if args.stage == "triage" else None
        if args.skipped == "true":
            status = "skipped"
        elif status == "success" and args.stage == "triage" and action not in VALID_TRIAGE_ACTIONS:
            status = "failure"
        if args.stage == "triage" and action in VALID_TRIAGE_ACTIONS:
            jira.set_property(args.issue, cfg["jira"]["triage_property"], {
                "action": action,
                "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "run_url": args.run_url,
            })
        pr_url = find_pr_url(args.output_dir, result) if args.stage == "code" else None
        if args.correlation_output and pr_url:
            args.correlation_output.write_text(json.dumps({"issue": args.issue, "pr_url": pr_url}) + "\n")
        lines = [f"**UIE {args.stage} {status}.**", f"Workflow: {args.run_url}"]
        if action:
            lines.insert(1, f"Triage action: `{action}`.")
        if pr_url:
            lines.insert(1, f"Pull request: {pr_url}")
        jira.labels(args.issue, remove=[CLAIM_LABELS[args.stage]])
        jira.delete_property(args.issue, cfg["jira"]["claim_property"])
        claim_id = claim.get("id", "unknown")
        post_comment(jira, args.issue, "\n\n".join(lines), f"uie-terminal-{claim_id}")
        return 0
    except GateError as exc:
        print(f"UIE completion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
