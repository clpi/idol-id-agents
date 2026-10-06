from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
import slack_intake as intake  # noqa: E402
from fleet_control.model import BillingClass, BillingProof, Route  # noqa: E402

NOW = 50.0


def route(route_id: str, billing: BillingClass = BillingClass.LOCAL) -> Route:
    base = Route(
        id=route_id,
        provider="local",
        model="model-one",
        provider_family="local",
        runtime="plain",
        command=("python3", "fake.py"),
        parser="plain-json",
        billing=billing,
        proof=BillingProof(kind="local-process", subject_hash="pending", observed_at=1,
                           expires_at=100, evidence_hash="e", trusted=True),
        roles=frozenset({"evidence"}),
    )
    return replace(base, proof=replace(base.proof, subject_hash=base.subject_hash))


class FakeSlack:
    def __init__(self, messages, replies=None):
        self.messages = messages
        self.replies = replies or {}
        self.posts = []

    def __call__(self, method, token, params):
        if method == "conversations.history":
            return {"ok": True, "messages": self.messages}
        if method == "conversations.replies":
            return {"ok": True, "messages": self.replies.get(params["ts"], [])}
        if method == "chat.postMessage":
            self.posts.append((token, dict(params)))
            return {"ok": True}
        raise AssertionError(method)


class SlackIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"MINIMAX_BOT": "xoxb-m", "FACTORY_BOT": "xoxb-f"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {
            "schema": intake.SCHEMA,
            "mode": "apply",
            "channel": "C0BVD016C9M",
            "history_token_env": "MINIMAX_BOT",
            "ignore_users": ["UDEVIN"],
            "agents": [
                {"name": "MiniMax", "user_id": "UMINI", "token_env": "MINIMAX_BOT", "route": "minimax"},
                {"name": "FactoryDroid", "user_id": "UFACT", "token_env": "FACTORY_BOT", "route": "factory"},
            ],
        }
        self.routes = {"minimax": route("minimax"), "factory": route("factory")}
        self.prompts = []

    def runner(self, selected, prompt, workdir):
        self.prompts.append((selected.id, prompt))
        return f"verdict from {selected.id}"

    def run_cycle(self, slack, config=None, routes=None):
        with intake.Ledger(Path(self.tmp.name)) as ledger:
            return intake.cycle(config or self.config, routes or self.routes, ledger,
                                transport=slack, runner=self.runner, now=NOW)

    def test_untagged_human_messages_rotate_across_apps_and_reply_once(self) -> None:
        slack = FakeSlack([
            {"ts": "1.000001", "user": "UHUMAN", "text": "why is ci red"},
            {"ts": "2.000001", "user": "UHUMAN", "text": "evaluate the parser"},
        ])
        decisions = self.run_cycle(slack)
        self.assertEqual([d["status"] for d in decisions], ["replied", "replied"])
        self.assertEqual([(token, params["thread_ts"]) for token, params in slack.posts],
                         [("xoxb-m", "1.000001"), ("xoxb-f", "2.000001")])
        self.assertIn("why is ci red", self.prompts[0][1])
        self.assertEqual(self.run_cycle(slack), [])
        self.assertEqual(len(slack.posts), 2)

    def test_devin_reply_does_not_suppress_fleet_reply_but_fleet_reply_does(self) -> None:
        slack = FakeSlack(
            [{"ts": "1.0", "user": "UHUMAN", "text": "fix", "reply_count": 1},
             {"ts": "2.0", "user": "UHUMAN", "text": "fix", "reply_count": 1}],
            {"1.0": [{"ts": "1.0", "user": "UHUMAN"}, {"ts": "1.1", "user": "UDEVIN"}],
             "2.0": [{"ts": "2.0", "user": "UHUMAN"}, {"ts": "2.1", "user": "UFACT"}]},
        )
        decisions = self.run_cycle(slack)
        self.assertEqual([d["status"] for d in decisions], ["replied", "already-answered"])
        self.assertEqual(len(slack.posts), 1)

    def test_bot_thread_and_agent_messages_are_skipped(self) -> None:
        slack = FakeSlack([
            {"ts": "1.0", "bot_id": "B1", "text": "Taking on"},
            {"ts": "2.0", "user": "UMINI", "text": "status"},
            {"ts": "3.1", "thread_ts": "3.0", "user": "UHUMAN", "text": "reply"},
            {"ts": "4.0", "user": "UHUMAN", "subtype": "channel_join", "text": "joined"},
            {"ts": "5.0", "user": "UDEVIN", "text": "devin result"},
        ])
        self.assertEqual(self.run_cycle(slack), [])
        self.assertEqual(slack.posts, [])

    def test_message_addressed_to_one_app_goes_only_to_that_app(self) -> None:
        slack = FakeSlack([
            {"ts": "1.0", "user": "UHUMAN", "text": "<@UFACT> review this"},
            {"ts": "2.0", "user": "UHUMAN", "text": "<@UOTHER> only you"},
        ])
        decisions = self.run_cycle(slack)
        self.assertEqual([(d["agent"], d["status"]) for d in decisions],
                         [("FactoryDroid", "replied"), (None, "addressed-elsewhere")])
        self.assertEqual([token for token, _ in slack.posts], ["xoxb-f"])

    def test_paygo_or_unproven_routes_are_never_dispatched(self) -> None:
        routes = {"minimax": route("minimax", BillingClass.PAYGO),
                  "factory": replace(route("factory"), proof=replace(route("factory").proof, trusted=False))}
        slack = FakeSlack([{"ts": "1.0", "user": "UHUMAN", "text": "do work"}])
        decisions = self.run_cycle(slack, routes=routes)
        self.assertTrue(decisions[0]["status"].startswith("no-admitted-route:"))
        self.assertIn("billing-paygo-forbidden", decisions[0]["status"])
        self.assertEqual(self.prompts, [])
        self.assertEqual(slack.posts, [])

    def test_disallowed_route_falls_through_to_next_admitted_app(self) -> None:
        routes = {"minimax": route("minimax", BillingClass.PAYGO), "factory": route("factory")}
        slack = FakeSlack([{"ts": "1.0", "user": "UHUMAN", "text": "do work"}])
        decisions = self.run_cycle(slack, routes=routes)
        self.assertEqual((decisions[0]["agent"], decisions[0]["status"]), ("FactoryDroid", "replied"))

    def test_observe_plan_neither_runs_nor_posts_nor_records(self) -> None:
        slack = FakeSlack([{"ts": "1.0", "user": "UHUMAN", "text": "do work"}])
        config = dict(self.config, mode="observe-plan")
        self.assertEqual(self.run_cycle(slack, config=config)[0]["status"], "selected")
        self.assertEqual((self.prompts, slack.posts), ([], []))
        self.assertEqual(self.run_cycle(slack, config=config)[0]["status"], "selected")

    def test_runtime_refusal_is_recorded_without_posting_or_retrying(self) -> None:
        slack = FakeSlack([{"ts": "1.0", "user": "UHUMAN", "text": "do work"}])

        def refuse(selected, prompt, workdir):
            raise intake.IntakeRefusal("route exited 1")

        with intake.Ledger(Path(self.tmp.name)) as ledger:
            decisions = intake.cycle(self.config, self.routes, ledger, transport=slack, runner=refuse, now=NOW)
        self.assertEqual(decisions[0]["status"], "refused:route exited 1")
        self.assertEqual(slack.posts, [])
        self.assertEqual(self.run_cycle(slack), [])

    def test_route_envelope_requires_reply_text_and_zero_local_cost(self) -> None:
        workdir = Path(self.tmp.name) / "work"

        def scripted(name, envelope):
            script = Path(self.tmp.name) / f"{name}.py"
            script.write_text(f"import sys\nprint(open(sys.argv[1]).read() and {envelope!r})\n")
            return replace(route(name), command=("python3", str(script), "{prompt}"))

        self.assertEqual(intake.run_route(scripted("good", '{"reply": "done"}'), "p", workdir), "done")
        with self.assertRaises(intake.IntakeRefusal):
            intake.run_route(scripted("empty", '{"status": "ok"}'), "p", workdir)
        with self.assertRaises(intake.IntakeRefusal):
            intake.run_route(scripted("costly", '{"reply": "x", "costUsd": 1}'), "p", workdir)

    def test_uncalibrated_fleet_config_disables_every_route(self) -> None:
        fleet = Path(self.tmp.name) / "fleet.json"
        fleet.write_text((ROOT / "config/fleet.example.json").read_text())
        routes = intake.load_fleet_routes(fleet)
        self.assertTrue(routes)
        self.assertTrue(all(not item.enabled for item in routes.values()))

    def test_example_config_loads_in_observe_plan(self) -> None:
        config = intake.load_config(ROOT / "config/slack-intake.example.json")
        self.assertEqual(config["mode"], "observe-plan")


if __name__ == "__main__":
    unittest.main()
