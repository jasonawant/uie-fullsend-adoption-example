#!/usr/bin/env python3
"""Gate Jira poll dispatches for the UIE Delivery Standard.

The Fullsend Jira poller deliberately emits generic execution references. This
script is the repository-owned policy layer between that output and the
reusable dispatcher. It is intentionally fail-closed: unknown Jira field
mapping, identity, or claim state means no agent run is started.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc
ELIGIBLE_TYPES = {"Story", "Task", "Bug"}
CLAIM_LABELS = {"triage": "triage-in-progress", "code": "code-in-progress"}
TRIGGER_LABELS = {"triage": "ready-for-triage", "code": "ready-to-code"}


class GateError(Exception):
    """A configuration or remote-operation failure that must block dispatch."""


class Jira:
    def __init__(self, base_url: str, email: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/json",
        }

    @classmethod
    def from_env(cls) -> Jira:
        required = ("JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_TOKEN")
        missing = next((name for name in required if not os.environ.get(name)), None)
        if missing:
            raise GateError(f"{missing} is required")
        return cls(os.environ["JIRA_BASE_URL"], os.environ["JIRA_USER_EMAIL"], os.environ["JIRA_TOKEN"])

    def request(self, method: str, path: str, body: Any | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base_url}/rest/api/3{path}", data=data, headers=self.headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise GateError(f"Jira {method} {path} returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GateError(f"Jira {method} {path} failed: {exc.reason}") from exc
        return json.loads(raw) if raw else None

    def issue(self, key: str, field_ids: list[str]) -> dict[str, Any]:
        fields = ",".join(["summary", "description", "issuetype", "status", "assignee", "priority", "labels"] + field_ids)
        return self.request("GET", f"/issue/{urllib.parse.quote(key)}?fields={urllib.parse.quote(fields)}")

    def property(self, key: str, name: str) -> Any | None:
        try:
            value = self.request("GET", f"/issue/{urllib.parse.quote(key)}/properties/{urllib.parse.quote(name)}")
        except GateError as exc:
            if "returned 404" in str(exc):
                return None
            raise
        return value.get("value")

    def set_property(self, key: str, name: str, value: Any) -> None:
        self.request("PUT", f"/issue/{urllib.parse.quote(key)}/properties/{urllib.parse.quote(name)}", value)

    def delete_property(self, key: str, name: str) -> None:
        try:
            self.request("DELETE", f"/issue/{urllib.parse.quote(key)}/properties/{urllib.parse.quote(name)}")
        except GateError as exc:
            if "returned 404" not in str(exc):
                raise

    def labels(self, key: str, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        add = add or []
        remove = remove or []
        update: dict[str, list[dict[str, str]]] = {"labels": []}
        update["labels"].extend({"add": label} for label in add)
        update["labels"].extend({"remove": label} for label in remove)
        if update["labels"]:
            self.request("PUT", f"/issue/{urllib.parse.quote(key)}", {"update": update})


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    jira = data.get("jira", {})
    required = ("project", "status", "route_label", "claim_property", "triage_property")
    missing = [key for key in required if not jira.get(key)]
    fields = jira.get("fields", {})
    missing += [f"fields.{key}" for key in ("current_sprint", "acceptance_criteria", "story_points") if not fields.get(key)]
    if missing:
        raise GateError(f"UIE configuration is incomplete: {', '.join(missing)}")
    return data


def nonempty(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (dict, list, set, tuple)):
        return bool(value)
    return True


def has_active_sprint(value: Any) -> bool:
    """Return whether Jira's Sprint field contains an active sprint object."""
    if not isinstance(value, list):
        return False
    return any(isinstance(sprint, dict) and sprint.get("state") == "active" for sprint in value)


def stage_and_event(ref: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    stage = ref.get("agent")
    if stage not in CLAIM_LABELS:
        return None, {}
    try:
        event = json.loads(ref["event_payload"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GateError(f"dispatch has invalid event_payload: {exc}") from exc
    if event.get("source", {}).get("system") != "jira" or event.get("entity", {}).get("kind") != "work_item":
        return None, event
    return stage, event


def trigger_is_valid(stage: str, event: dict[str, Any]) -> bool:
    transition = event.get("transition", {})
    if transition.get("kind") == "label_changed":
        label = transition.get("label", {})
        return label.get("action") == "added" and label.get("name") == TRIGGER_LABELS[stage]
    if transition.get("kind") == "comment_added":
        comment = transition.get("comment", {})
        command = "/fs-triage" if stage == "triage" else "/fs-code"
        first_line = str(comment.get("body", "")).splitlines()[0].strip() if comment.get("body") else ""
        return comment.get("command") == command and first_line == command
    return False


def gate_failures(stage: str, event: dict[str, Any], issue: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    jira_cfg = cfg["jira"]
    fields = issue.get("fields", {})
    configured = jira_cfg["fields"]
    failures: list[str] = []
    if event.get("actor", {}).get("kind") == "bot":
        failures.append("trigger actor is the Jira poller bot")
    if not trigger_is_valid(stage, event):
        failures.append(f"event is not a new {TRIGGER_LABELS[stage]} label or exact /fs-{stage} command")
    routes = [label for label in fields.get("labels", []) if str(label).startswith("repo:")]
    if routes != [jira_cfg["route_label"]]:
        failures.append(f"repository ownership must be exactly {jira_cfg['route_label']}")
    if fields.get("issuetype", {}).get("name") not in ELIGIBLE_TYPES:
        failures.append("work type must be Story, Task, or Bug")
    if fields.get("status", {}).get("name") != jira_cfg["status"]:
        failures.append(f"Jira status must be {jira_cfg['status']}")
    for name, value in (
        ("Assignee", fields.get("assignee")),
        ("Summary", fields.get("summary")),
        ("Description", fields.get("description")),
        ("Priority", fields.get("priority")),
        ("Acceptance Criteria", fields.get(configured["acceptance_criteria"])),
    ):
        if not nonempty(value):
            failures.append(f"{name} must be set")
    if not has_active_sprint(fields.get(configured["current_sprint"])):
        failures.append("Current Sprint must contain an active sprint")
    try:
        if float(fields.get(configured["story_points"])) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        failures.append("Story Points must be a positive numeric value")
    return failures


def marker(key: str, stage: str, event: dict[str, Any], kind: str) -> str:
    raw = json.dumps(event.get("transition", {}), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"uie-{kind}-{key}-{stage}-{digest}"


def post_comment(jira: Jira, key: str, text: str, comment_marker: str) -> None:
    command = [
        "fullsend", "issues", "post-comment", "--tracker", "jira", "--project", key.split("-", 1)[0],
        "--number", key.rsplit("-", 1)[1], "--jira-url", jira.base_url,
        "--jira-email", os.environ["JIRA_USER_EMAIL"], "--token", os.environ["JIRA_TOKEN"],
        "--marker", comment_marker, "--result", "-",
    ]
    try:
        subprocess.run(command, input=text, text=True, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateError(f"Failed to post Jira comment: {exc}") from exc


def active_claim(claim: Any, now: dt.datetime, ttl_minutes: int) -> bool:
    if not isinstance(claim, dict) or not claim.get("created_at"):
        return False
    try:
        created = dt.datetime.fromisoformat(str(claim["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - created <= dt.timedelta(minutes=ttl_minutes)


def claim(jira: Jira, key: str, stage: str, event: dict[str, Any], cfg: dict[str, Any], run_url: str) -> tuple[bool, str]:
    jira_cfg = cfg["jira"]
    now = dt.datetime.now(UTC)
    existing = jira.property(key, jira_cfg["claim_property"])
    if active_claim(existing, now, int(jira_cfg.get("claim_ttl_minutes", 90))):
        return False, f"active {existing.get('stage', 'unknown')} run {existing.get('run_url', '')}".strip()
    claim_value = {
        "id": str(uuid.uuid4()), "stage": stage, "created_at": now.isoformat(), "run_url": run_url,
        "event_marker": marker(key, stage, event, "trigger"),
    }
    jira.set_property(key, jira_cfg["claim_property"], claim_value)
    # Jira entity properties lack compare-and-set. Read-after-write detects a
    # competing claimant; workflow-level serialization narrows that window.
    confirmed = jira.property(key, jira_cfg["claim_property"])
    if not isinstance(confirmed, dict) or confirmed.get("id") != claim_value["id"]:
        return False, "claim was superseded by another dispatcher"
    jira.labels(key, add=[CLAIM_LABELS[stage]], remove=[TRIGGER_LABELS[stage]])
    return True, ""


def process(refs: list[dict[str, Any]], jira: Jira, cfg: dict[str, Any], run_url: str) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    field_ids = list(cfg["jira"]["fields"].values())
    for ref in refs:
        stage, event = stage_and_event(ref)
        if stage is None:
            continue
        key = event.get("entity", {}).get("key", "<unknown>")
        if not key:
            print("Skipping Jira dispatch without entity.key", file=sys.stderr)
            continue
        try:
            issue = jira.issue(key, field_ids)
            failures = gate_failures(stage, event, issue, cfg)
            if stage == "code" and not failures:
                triage = jira.property(key, cfg["jira"]["triage_property"])
                if not isinstance(triage, dict) or triage.get("action") != "sufficient":
                    failures.append("latest recorded UIE triage action must be sufficient")
            if failures:
                post_comment(jira, key, "**UIE Delivery Gate rejected this request.**\n\n" + "\n".join(f"- {f}" for f in failures), marker(key, stage, event, "rejection"))
                if event.get("transition", {}).get("kind") == "label_changed":
                    jira.labels(key, remove=[TRIGGER_LABELS[stage]])
                continue
            acquired, reason = claim(jira, key, stage, event, cfg, run_url)
            if not acquired:
                post_comment(jira, key, f"**UIE {stage} is already active.** {reason}", marker(key, stage, event, "active"))
                if event.get("transition", {}).get("kind") == "label_changed":
                    jira.labels(key, remove=[TRIGGER_LABELS[stage]])
                continue
            post_comment(jira, key, f"**UIE {stage} started.** Workflow: {run_url}", marker(key, stage, event, "started"))
            accepted.append(ref)
        except GateError as exc:
            raise GateError(f"UIE gate could not process {key} {stage}: {exc}") from exc
    return accepted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        refs = json.loads(args.input.read_text())
        if not isinstance(refs, list):
            raise GateError("poll output must be a JSON array")
        accepted = process(refs, Jira.from_env(), cfg, args.run_url)
        args.output.write_text(json.dumps(accepted) + "\n")
        return 0
    except (GateError, json.JSONDecodeError) as exc:
        print(f"UIE gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
