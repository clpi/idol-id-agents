#!/usr/bin/env python3
"""Join an existing Apodex session. Refuse if this host has no session.

Mailbox check on 2026-09-25: no Apodex message to chrisp1877@gmail.com.
Marketing mail went to other addresses. GitHub OAuth on clpi was user:email
only, which cannot list sessions. This script does not create an account,
does not guess a session id, and does not send a request without both
APODEX_API_KEY and APODEX_SESSION_ID.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    key = os.environ.get("APODEX_API_KEY", "").strip()
    session = os.environ.get("APODEX_SESSION_ID", "").strip()
    if not key or not session:
        print(
            "no apodex session: APODEX_API_KEY and APODEX_SESSION_ID are unset. "
            "None was found for chrisp1877@gmail.com.",
            file=sys.stderr,
        )
        return 2
    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not prompt.strip():
        print("no prompt on stdin", file=sys.stderr)
        return 2
    body = json.dumps(
        {
            "model": "apodex-1.1",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
        }
    ).encode()
    request = urllib.request.Request(
        "https://api.apodex.ai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Session-Id": session,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        sys.stdout.write(response.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
