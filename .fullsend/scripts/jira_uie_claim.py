#!/usr/bin/env python3
"""Operator-only claim recovery and triage-record management for UIE stages."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from jira_uie_gate import CLAIM_LABELS, GateError, Jira, load_config, post_comment

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("record-triage", "release-claim"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--triage-action", choices=("sufficient", "insufficient", "duplicate", "prerequisites", "in-progress"))
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        jira = Jira.from_env()
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        if args.action == "record-triage":
            if not args.triage_action:
                raise GateError("--triage-action is required when recording triage")
            jira.set_property(args.issue, cfg["jira"]["triage_property"], {
                "action": args.triage_action, "recorded_at": stamp, "run_url": args.run_url,
            })
            post_comment(
                jira, args.issue,
                f"**UIE triage recorded:** `{args.triage_action}`. {args.reason}\n\nWorkflow: {args.run_url}",
                f"uie-triage-result-{args.issue}-{args.run_url.rsplit('/', 1)[-1]}",
            )
            return 0
        claim = jira.property(args.issue, cfg["jira"]["claim_property"])
        stage = claim.get("stage") if isinstance(claim, dict) else None
        if stage not in CLAIM_LABELS:
            raise GateError("no releasable UIE triage or code claim exists")
        jira.labels(args.issue, remove=[CLAIM_LABELS[stage]])
        jira.delete_property(args.issue, cfg["jira"]["claim_property"])
        post_comment(
            jira, args.issue,
            f"**UIE {stage} claim released.** {args.reason}\n\nWorkflow: {args.run_url}",
            f"uie-claim-release-{args.issue}-{stage}-{args.run_url.rsplit('/', 1)[-1]}",
        )
        return 0
    except GateError as exc:
        print(f"UIE claim operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
