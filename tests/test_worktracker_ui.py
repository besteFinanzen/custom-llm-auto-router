"""Behavioural checks for the self-contained Worktracker browser application."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_worktracker_browser_authentication():
    """Run dependency-free DOM/fetch fakes against the JavaScript served to users."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("the optional browser JavaScript checks require Node.js")
    suite = Path(__file__).parent / "js" / "worktracker-auth.test.cjs"
    result = subprocess.run(
        [node, "--test", str(suite)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
