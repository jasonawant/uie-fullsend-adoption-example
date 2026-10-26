import datetime as dt
import importlib.util
import json
import os
import subprocess
import tempfile
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
            "customfield_1": [{"id": 1, "name": "Sprint 1", "state": "active"}],
            "customfield_2": "Given a valid request, it is checked",
            "customfield_3": 3,
        }
    }


class FakeJira:
    def __init__(self, triage=None):
        self.base_url = "https://jira.example.test"
        self.triage = triage
        self.claim = None

    def issue(self, key, field_ids):
        return issue()

    def property(self, key, name):
        if name == CONFIG["jira"]["triage_property"]:
            return self.triage
        return self.claim

    def set_property(self, key, name, value):
        if name == CONFIG["jira"]["claim_property"]:
            self.claim = value

    def labels(self, key, add=None, remove=None):
        pass


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

    def test_rejects_future_only_sprint(self):
        candidate = issue()
        candidate["fields"]["customfield_1"] = [{"id": 2, "name": "Sprint 2", "state": "future"}]
        failures = gate.gate_failures("triage", event(), candidate, CONFIG)
        self.assertIn("Current Sprint must contain an active sprint", failures)

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

    def test_trigger_must_be_an_exact_first_line_command(self):
        valid = event()
        valid["transition"] = {
            "kind": "comment_added",
            "comment": {"command": "/fs-triage", "body": "/fs-triage"},
        }
        self.assertTrue(gate.trigger_is_valid("triage", valid))
        valid["transition"]["comment"]["body"] = "/fs-triage investigate this"
        self.assertFalse(gate.trigger_is_valid("triage", valid))

    def test_code_request_requires_sufficient_triage(self):
        ref = {"agent": "code", "event_payload": json.dumps(event("code"))}
        original_post_comment = gate.post_comment
        gate.post_comment = lambda *args: None
        try:
            self.assertEqual(gate.process([ref], FakeJira(), CONFIG, "https://example.test/run"), [])
            accepted = gate.process(
                [ref], FakeJira({"action": "sufficient"}), CONFIG, "https://example.test/run"
            )
        finally:
            gate.post_comment = original_post_comment
        self.assertEqual(len(accepted), 1)

    def test_load_config_rejects_missing_required_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uie-delivery.json"
            invalid = json.loads(json.dumps(CONFIG))
            invalid["jira"]["fields"]["story_points"] = ""
            path.write_text(json.dumps(invalid))
            with self.assertRaisesRegex(gate.GateError, "fields.story_points"):
                gate.load_config(path)

    def test_nonempty_rejects_empty_collections(self):
        self.assertFalse(gate.nonempty([]))
        self.assertFalse(gate.nonempty({}))
        self.assertTrue(gate.nonempty(["value"]))

    def test_post_comment_wraps_command_failure(self):
        original_run = gate.subprocess.run
        gate.subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "fullsend")
        )
        try:
            with self.assertRaisesRegex(gate.GateError, "Failed to post Jira comment"):
                gate.post_comment(FakeJira(), "FRFLY-123", "body", "marker")
        finally:
            gate.subprocess.run = original_run

    def test_jira_from_env_requires_each_credential(self):
        original = dict(os.environ)
        try:
            for name in ("JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_TOKEN"):
                os.environ.pop(name, None)
            with self.assertRaisesRegex(gate.GateError, "JIRA_BASE_URL is required"):
                gate.Jira.from_env()
        finally:
            os.environ.clear()
            os.environ.update(original)

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
