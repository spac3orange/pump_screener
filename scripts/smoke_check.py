#!/usr/bin/env python3
"""Basic smoke checks against a running screener instance."""

from __future__ import annotations

import sys
import urllib.request


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    for path in ("/healthz", "/metrics"):
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            if resp.status != 200:
                print(f"FAIL {path} status={resp.status}")
                return 1
            print(f"OK {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
