#!/usr/bin/env python3
"""Discover non-secret Jira field IDs required by the UIE delivery gate.

By default this prints candidate values only; --write updates the supplied UIE
configuration after verifying that each required field name maps to exactly one
custom field.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jira_uie_gate import GateError, Jira


FIELD_NAMES = {
    "current_sprint": "Sprint",
    "acceptance_criteria": "Acceptance Criteria",
    "story_points": "Story Points",
}

def candidates(fields: list[dict[str, Any]], name: str) -> list[dict[str, str]]:
    return [
        {"id": str(field["id"]), "name": str(field["name"]), "schema": str(field.get("schema", {}))}
        for field in fields
        if field.get("name") == name and str(field.get("id", "")).startswith("customfield_")
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(".fullsend/uie-delivery.json"))
    parser.add_argument("--write", action="store_true", help="write unambiguous discovered IDs to --config")
    args = parser.parse_args()
    try:
        jira = Jira.from_env()
        fields = jira.request("GET", "/field")
        if not isinstance(fields, list):
            raise GateError("Jira /field did not return a field list")
        found = {key: candidates(fields, name) for key, name in FIELD_NAMES.items()}
        print(json.dumps({"fields": found}, indent=2, sort_keys=True))
        ambiguous = [key for key, values in found.items() if len(values) != 1]
        if ambiguous:
            raise GateError(f"field discovery is not unique for: {', '.join(ambiguous)}")
        if args.write:
            config = json.loads(args.config.read_text())
            for key, values in found.items():
                config["jira"]["fields"][key] = values[0]["id"]
            args.config.write_text(json.dumps(config, indent=2) + "\n")
            print(f"Updated {args.config}", file=sys.stderr)
        return 0
    except (GateError, OSError, json.JSONDecodeError) as exc:
        print(f"UIE Jira discovery failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
