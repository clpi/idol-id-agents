import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "claw_evidence_policy", ROOT / "scripts" / "claw_evidence_policy.py"
)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


class EvidencePolicyTests(unittest.TestCase):
    def test_cloudflare_insights_blocked_by_csp_is_expected(self) -> None:
        self.assertTrue(
            policy.expected_request_failure(
                {"host": "static.cloudflareinsights.com", "failure": "csp"}
            )
        )

    def test_first_party_csp_failure_is_not_exempt(self) -> None:
        self.assertFalse(
            policy.expected_request_failure(
                {"host": "claw.idol.id", "failure": "csp"}
            )
        )

    def test_cloudflare_network_failure_is_not_exempt(self) -> None:
        self.assertFalse(
            policy.expected_request_failure(
                {
                    "host": "static.cloudflareinsights.com",
                    "failure": "net::ERR_CONNECTION_RESET",
                }
            )
        )

    def test_arbitrary_third_party_failure_is_not_exempt(self) -> None:
        self.assertFalse(
            policy.expected_request_failure(
                {"host": "cdn.example.invalid", "failure": "csp"}
            )
        )


if __name__ == "__main__":
    unittest.main()
