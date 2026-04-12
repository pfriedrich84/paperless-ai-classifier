#!/usr/bin/env python3
"""Verify that all installed Python packages are at least N days old.

Supply-chain protection: newly published packages are more likely to be
compromised.  This script queries PyPI for the upload date of each installed
version and fails if any package is younger than the configured threshold.

Security patches from established packages can be exempted via an allowlist
file (one ``package==version`` per line, with a comment explaining the CVE).

Usage:
    python scripts/check_dependency_age.py [--min-days 14] [--allowlist .dependency-age-allowlist]

Exit codes:
    0  all packages pass
    1  one or more packages are too new
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

# Packages we don't publish to PyPI (local editable installs, etc.)
SKIP_PACKAGES = frozenset({
    "paperless-ai-classifier",
    "pip",
    "setuptools",
    "wheel",
    "pkg-resources",
})


def load_allowlist(path: str) -> set[tuple[str, str]]:
    """Load package==version pairs that are exempted from the age check."""
    allowed: set[tuple[str, str]] = set()
    if not os.path.isfile(path):
        return allowed
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "==" in line:
                name, version = line.split("==", 1)
                allowed.add((name.strip().lower(), version.strip()))
    return allowed


def get_installed_packages() -> list[tuple[str, str]]:
    """Return [(name, version), ...] from pip freeze."""
    out = subprocess.check_output(
        [sys.executable, "-m", "pip", "freeze", "--local"],
        text=True,
    )
    packages = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # editable installs: -e git+...#egg=name
        if "==" in line:
            name, version = line.split("==", 1)
            packages.append((name.strip(), version.strip()))
    return packages


def get_release_date(name: str, version: str) -> datetime | None:
    """Query PyPI JSON API for the upload timestamp of *name*==*version*."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        urls = data.get("urls", [])
        if not urls:
            return None
        upload_time = urls[0].get("upload_time_iso_8601") or urls[0].get("upload_time")
        if not upload_time:
            return None
        # Handle both formats: with and without timezone
        ts = upload_time.replace("Z", "+00:00")
        if "+" not in ts and ts.count("T") == 1:
            ts += "+00:00"
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-days",
        type=int,
        default=14,
        help="Minimum age in days (default: 14)",
    )
    parser.add_argument(
        "--allowlist",
        default=".dependency-age-allowlist",
        help="Path to allowlist file for security-patch exceptions (default: .dependency-age-allowlist)",
    )
    args = parser.parse_args()

    allowlist = load_allowlist(args.allowlist)
    if allowlist:
        print(f"Loaded {len(allowlist)} allowlisted exception(s)")

    now = datetime.now(timezone.utc)
    packages = get_installed_packages()
    violations: list[tuple[str, str, datetime, int]] = []
    checked = 0

    for name, version in packages:
        if name.lower() in {s.lower() for s in SKIP_PACKAGES}:
            continue
        if (name.lower(), version) in allowlist:
            continue

        release_date = get_release_date(name, version)
        if release_date is None:
            # Can't verify — skip (e.g. private packages)
            continue

        checked += 1
        age_days = (now - release_date).days
        if age_days < args.min_days:
            violations.append((name, version, release_date, age_days))

    print(f"Checked {checked} packages (min age: {args.min_days} days)")

    if violations:
        print(f"\nFAILED: {len(violations)} package(s) are too new:\n")
        print(f"{'Package':<30} {'Version':<15} {'Released':<12} {'Age (days)':<10}")
        print("-" * 70)
        for name, version, release_date, age in sorted(violations, key=lambda x: x[3]):
            print(f"{name:<30} {version:<15} {release_date:%Y-%m-%d}   {age}")
        print(
            f"\nAll dependencies must be at least {args.min_days} days old "
            "to mitigate supply-chain attacks."
        )
        print("Pin affected packages to older versions in pyproject.toml or constraints.txt.")
        return 1

    print("OK — all packages meet the minimum age requirement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
