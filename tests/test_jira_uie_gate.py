import datetime as dt
import importlib.util
import json
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".fullsend/scripts/jira_uie_gate.py"
SPEC = importlib.util.spec_from_file_location("jira_uie_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gate)


CONFIG = {
    "jira": {
        "project": "FRFLY",
        "status": "In Progress",
        "route_label": "repo:github/jasonawant/uie-fullsend-adoption-example",
        "claim_property": "fullsend-uie-delivery-claim",
        "triage_property": "fullsend-uie-delivery-triage",
        "claim_ttl_minutes": 90,
        "fields": {
            "current_sprint": "customfield_1",
            "acceptance_criteria": "customfield_2",
            "story_points": "customfield_3",
        },
    }
}


def event(stage="triage"):
    label = "ready-for-triage" if stage == "triage" else "ready-to-code"
    return {
        "source": {"system": "jira"},
        "entity": {"kind": "work_item", "key": "FRFLY-123"},
        "actor": {"id": "human-account", "kind": "human"},
        "transition": {"kind": "label_changed", "label": {"action": "added", "name": label}},
    }


def issue():
    return {
        "fields": {
            "labels": ["repo:github/jasonawant/uie-fullsend-adoption-example"],
            "issuetype": {"name": "Story"},
            "status": {"name": "In Progress"},
            "assignee": {"accountId": "human-account"},
            "summary": "Implement the policy gate",
            "description": {"content": [{"type": "paragraph"}]},
            "priority": {"name": "High"},
            "customfield_1": {"name": "Sprint 1"},
            "customfield_2": "Given a valid request, it is checked",
            "customfield_3": 3,
        }
    }


class JiraUieGateTests(unittest.TestCase):
    def test_accepts_complete_triage_request(self):
        self.assertEqual(gate.gate_failures("triage", event(), issue(), CONFIG), [])

    def test_rejects_missing_required_values_and_extra_routes(self):
        candidate = issue()
        candidate["fields"]["labels"].append("repo:gitlab/jasonawant/uie-fullsend-adoption-example")
        candidate["fields"]["customfield_3"] = 0
        failures = gate.gate_failures("triage", event(), candidate, CONFIG)
        self.assertIn("repository ownership must be exactly repo:github/jasonawant/uie-fullsend-adoption-example", failures)
        self.assertIn("Story Points must be a positive numeric value", failures)

    def test_rejects_bot_and_non_exact_command(self):
        candidate = event()
        candidate["actor"]["kind"] = "bot"
        candidate["transition"] = {
            "kind": "comment_added",
            "comment": {"command": "/fs-triage", "body": "/fs-triage extra instructions"},
        }
        failures = gate.gate_failures("triage", candidate, issue(), CONFIG)
        self.assertIn("trigger actor is the Jira poller bot", failures)
        self.assertIn("event is not a new ready-for-triage label or exact /fs-triage command", failures)

    def test_claim_expiry(self):
        now = dt.datetime.now(dt.timezone.utc)
        current = {"created_at": now.isoformat()}
        stale = {"created_at": (now - dt.timedelta(minutes=91)).isoformat()}
        self.assertTrue(gate.active_claim(current, now, 90))
        self.assertFalse(gate.active_claim(stale, now, 90))

    def test_poll_ref_is_jira_work_item(self):
        ref = {"agent": "triage", "event_payload": json.dumps(event())}
        self.assertEqual(gate.stage_and_event(ref)[0], "triage")


if __name__ == "__main__":
    unittest.main()
