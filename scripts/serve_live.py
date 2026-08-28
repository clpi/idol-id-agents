#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from fleet_control.live_api import LiveApiError, serve


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the read-only local Idol Live observatory")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18991)
    args = parser.parse_args()
    try:
        serve(args.state_dir.expanduser(), host=args.host, port=args.port)
    except (OSError, ValueError, LiveApiError) as exc:
        print(f"idol-live-observatory: REFUSED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
