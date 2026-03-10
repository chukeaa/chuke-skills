#!/usr/bin/env python3
"""NotebookLM CLI wrapper (Python).

This wrapper forwards all arguments to notebooklm-py CLI.
Preferred binary order:
1) NOTEBOOKLM_BIN env var
2) ~/.local/bin/notebooklm
3) notebooklm from PATH
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def usage() -> None:
    print("Usage: notebooklm.py <command> [args...]", file=sys.stderr)
    print("Examples:", file=sys.stderr)
    print("  notebooklm.py status", file=sys.stderr)
    print("  notebooklm.py login", file=sys.stderr)
    print("  notebooklm.py list", file=sys.stderr)
    print("  notebooklm.py use <notebook_id>", file=sys.stderr)
    print("  notebooklm.py ask \"Summarize the key takeaways\" --notebook <id>", file=sys.stderr)
    raise SystemExit(2)


def resolve_bin() -> str:
    env_bin = os.getenv("NOTEBOOKLM_BIN")
    if env_bin:
        return env_bin

    user_bin = Path.home() / ".local" / "bin" / "notebooklm"
    if user_bin.exists():
        return str(user_bin)

    path_bin = shutil.which("notebooklm")
    return path_bin or "notebooklm"


def missing_binary(bin_path: str) -> None:
    print(f"NotebookLM CLI not found: {bin_path}", file=sys.stderr)
    print("Install notebooklm-py and retry.", file=sys.stderr)
    print("Suggested install:", file=sys.stderr)
    print("  python3 -m pip install --user -U notebooklm-py --break-system-packages", file=sys.stderr)
    print("Then verify:", file=sys.stderr)
    print("  ~/.local/bin/notebooklm --version", file=sys.stderr)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        usage()

    bin_path = resolve_bin()
    try:
        result = subprocess.run([bin_path, *args], check=False)
        return int(result.returncode)
    except FileNotFoundError:
        missing_binary(bin_path)
        return 127
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to run NotebookLM CLI: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
