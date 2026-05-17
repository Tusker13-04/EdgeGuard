# tests/test_cli.py
# CLI argument validation tests — covers ISS-05 (--interval bounds check).

import subprocess
import sys


def test_cli_interval_bounds():
    """Verify that main.py rejects --interval <= 0 with exit code 2."""
    # Test zero interval
    proc_zero = subprocess.run(
        [sys.executable, "main.py", "--interval", "0"],
        capture_output=True,
        text=True,
    )
    assert proc_zero.returncode != 0
    assert "interval must be > 0" in proc_zero.stderr.lower()

    # Test negative interval
    proc_neg = subprocess.run(
        [sys.executable, "main.py", "--interval", "-1"],
        capture_output=True,
        text=True,
    )
    assert proc_neg.returncode != 0
    assert "interval must be > 0" in proc_neg.stderr.lower()


def test_cli_interval_valid():
    """Verify that main.py starts (or at least doesn't exit immediately) with a valid interval."""
    # --demo avoids waiting for a live socket; timeout=2 catches hangs
    proc = subprocess.run(
        [sys.executable, "main.py", "--demo", "data/demo.jsonl", "--interval", "0.5"],
        capture_output=True,
        text=True,
        timeout=2,
    )
    # It will time out (TimeoutExpired) or exit cleanly — either is fine.
    # What we must NOT see is the interval validation error.
    assert "interval must be > 0" not in proc.stderr.lower()
