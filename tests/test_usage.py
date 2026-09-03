from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fleet_control.model import BillingClass, BillingProof, Route
from fleet_control.usage import UsageRefusal, observe_usage, refresh_routes


class UsageTests(unittest.TestCase):
    now = 1_000_000.0

    def route(self, script: Path, *, required: bool = True) -> Route:
        route = Route(
            id="included-test",
            provider="provider",
            model="model",
            provider_family="family",
            runtime="plain",
            command=("true",),
            parser="plain-json",
            billing=BillingClass.INCLUDED,
            proof=BillingProof(
                kind="subscription-plan",
                subject_hash="pending",
                observed_at=1,
                expires_at=2_000_000,
                evidence_hash="proof",
                trusted=True,
            ),
            roles=frozenset({"mechanic"}),
            enabled=True,
            usage_command=("python3", str(script), "{route_subject}"),
            usage_required=required,
            usage_max_age_seconds=900,
        )
        return replace(route, proof=replace(route.proof, subject_hash=route.subject_hash))

    def script(self, root: Path, overrides: dict | None = None) -> Path:
        path = root / "usage.py"
        payload = {
            "schema": "idol.fleet.usage.v1",
            "route_subject": "ARG",
            "provider": "provider",
            "model": "model",
            "billing": "included",
            "observed_at": self.now,
            "windows": [{"label": "session", "remaining_fraction": 0.75, "resets_at": self.now + 3600}],
            "extra_usage_enabled": False,
            "paygo_enabled": False,
            "purchased_credits_selected": False,
            "topup_selected": False,
            "reset_redeemed": False,
        }
        payload.update(overrides or {})
        path.write_text(
            "import json, sys\n"
            f"p={payload!r}\n"
            "p['route_subject']=sys.argv[1] if p['route_subject']=='ARG' else p['route_subject']\n"
            "print(json.dumps(p))\n"
        )
        return path

    def test_valid_usage_observation_updates_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            route = self.route(self.script(root))
            observation = observe_usage(route, now=self.now)
            self.assertEqual(observation.windows[0].remaining_fraction, 0.75)
            routes, facts = refresh_routes((route,), now=self.now)
            self.assertTrue(routes[0].enabled)
            self.assertEqual(routes[0].allowance, observation.windows)
            self.assertTrue(facts[0]["ok"])

    def test_paygo_flag_refuses_included_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            route = self.route(self.script(root, {"paygo_enabled": True}))
            with self.assertRaises(UsageRefusal):
                observe_usage(route, now=self.now)

    def test_topup_or_reset_flags_refuse(self) -> None:
        for field in ("extra_usage_enabled", "purchased_credits_selected", "topup_selected", "reset_redeemed"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                route = self.route(self.script(root, {field: True}))
                with self.assertRaises(UsageRefusal):
                    observe_usage(route, now=self.now)

    def test_wrong_route_subject_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            route = self.route(self.script(root, {"route_subject": "wrong"}))
            with self.assertRaises(UsageRefusal):
                observe_usage(route, now=self.now)

    def test_stale_usage_refuses_and_disables_required_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            route = self.route(self.script(root, {"observed_at": self.now - 901}))
            routes, facts = refresh_routes((route,), now=self.now)
            self.assertFalse(routes[0].enabled)
            self.assertFalse(facts[0]["ok"])

    def test_optional_usage_failure_does_not_fabricate_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            route = self.route(self.script(root, {"windows": []}), required=False)
            routes, facts = refresh_routes((route,), now=self.now)
            self.assertTrue(routes[0].enabled)
            self.assertFalse(routes[0].allowance)
            self.assertFalse(facts[0]["ok"])


if __name__ == "__main__":
    unittest.main()
