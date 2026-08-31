#!/usr/bin/env python3
"""Install or remove the per-user macOS Analyst Console service."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.bmv.analyst-console"
DESTINATION = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
STATE_DIR = ROOT / "data" / "analyst_console"
PAGES_ORIGIN = "https://chiavellini.github.io"
PAGES_URL = f"{PAGES_ORIGIN}/bmv-filings-platform/"


def payload() -> dict:
    python = ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit("Install the root .venv before installing the Analyst Console service.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "src.analyst_console.server",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PDFS_PROJECT_ROOT": str(ROOT),
            "ANALYST_CONSOLE_PAGES_ORIGIN": PAGES_ORIGIN,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(STATE_DIR / "server.log"),
        "StandardErrorPath": str(STATE_DIR / "server.err"),
        "ProcessType": "Interactive",
    }


def domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> None:
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    temporary = DESTINATION.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload(), handle, sort_keys=False)
    temporary.replace(DESTINATION)
    subprocess.run(["launchctl", "bootout", domain(), str(DESTINATION)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "bootstrap", domain(), str(DESTINATION)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain()}/{LABEL}"], check=True)
    print(f"Installed {LABEL}. Add {PAGES_URL} to the Dock from Safari.")


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", domain(), str(DESTINATION)], check=False)
    DESTINATION.unlink(missing_ok=True)
    print(f"Removed {LABEL}. Job history was left in {STATE_DIR}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "uninstall"))
    args = parser.parse_args(argv)
    install() if args.command == "install" else uninstall()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
