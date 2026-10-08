#!/usr/bin/env python3
"""Guard: prevents a new cron run while a previous agent run is still active.

Exits 0 (proceed) only when the agent is idle. The caller must skip task
execution on any non-zero exit code so a second run can never overlap the
previous one.
"""

import argparse
import re
import subprocess
import sys
import time

CIM_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name like 'hermes%.exe'\" | "
    "ForEach-Object { $_.CommandLine }"
)


def _cim_command_lines():
    """Yield command lines of running hermes*.exe processes (PowerShell/CIM)."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", CIM_QUERY],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line:
            yield line


def find_busy_processes(profile_name, process_lines=None):
    """Yield PIDs/command lines of hermes runs started with `-p <profile>`."""
    pattern = re.compile(r"-p\s+" + re.escape(profile_name), re.IGNORECASE)
    if process_lines is None:
        process_lines = _cim_command_lines
    for cmd in process_lines():
        if pattern.search(cmd):
            yield cmd


def main(argv=None, process_lines=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="ai-worker")
    parser.add_argument("--max-wait", type=float, default=300)
    parser.add_argument("--check-interval", type=float, default=10)
    args = parser.parse_args(argv)
    if process_lines is None:
        process_lines = _cim_command_lines

    elapsed = 0.0
    print("Checking if agent profile '%s' is active..." % args.profile)

    while any(find_busy_processes(args.profile, process_lines)):
        if elapsed >= args.max_wait:
            print("Agent has been busy for %.0fs. Skipping this interval." % args.max_wait)
            return 1
        print("Agent is busy. Waiting %.0fs... (%.0fs elapsed)"
              % (args.check_interval, elapsed))
        time.sleep(args.check_interval)
        elapsed += args.check_interval

    print("Agent is idle. Proceeding with task execution.")
    return 0


if __name__ == "__main__":
    sys.exit(main())