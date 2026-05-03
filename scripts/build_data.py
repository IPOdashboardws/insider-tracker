"""
build_data.py — Run both fetchers, merge into docs/data.json

Usage: python build_data.py
Env vars: DART_API_KEY, SEC_USER_AGENT
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"
OUT = ROOT / "docs" / "data.json"


def run(script: str) -> list:
    """Run a fetcher script and parse its stdout JSON."""
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / script)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print(f"[build] {script} exited {result.returncode}", file=sys.stderr)
            return []
        return json.loads(result.stdout or "[]")
    except subprocess.TimeoutExpired:
        print(f"[build] {script} timed out", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"[build] {script} produced invalid JSON: {e}", file=sys.stderr)
        return []


def main() -> None:
    print("[build] Fetching KR data...", file=sys.stderr)
    kr = run("fetch_kr.py")
    print(f"[build] KR: {len(kr)} entries", file=sys.stderr)

    print("[build] Fetching US data...", file=sys.stderr)
    us = run("fetch_us.py")
    print(f"[build] US: {len(us)} entries", file=sys.stderr)

    print("[build] Fetching JP data...", file=sys.stderr)
    jp = run("fetch_jp.py")
    print(f"[build] JP: {len(jp)} entries", file=sys.stderr)

    # Preserve previous data if all fetchers failed (avoid wiping the dashboard)
    if not kr and not us and not jp and OUT.exists():
        print("[build] All fetchers empty; preserving existing data.json", file=sys.stderr)
        return

    payload = {
        "last_updated": datetime.now(KST).isoformat(),
        "kr": kr,
        "us": us,
        "jp": jp,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[build] Wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
