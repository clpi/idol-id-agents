#!/usr/bin/env python3
"""Small evidence policy shared by the Claw browser probe.

Only nonessential third-party telemetry explicitly blocked by the application's
own CSP is exempt from boot admission. First-party failures and arbitrary
third-party failures remain evidence of a broken surface.
"""

from __future__ import annotations

from typing import Mapping, Any

CLOUDFLARE_INSIGHTS_HOST = "static.cloudflareinsights.com"


def expected_request_failure(item: Mapping[str, Any]) -> bool:
    host = str(item.get("host") or "").lower()
    failure = str(item.get("failure") or "").lower()
    return host == CLOUDFLARE_INSIGHTS_HOST and failure == "csp"
