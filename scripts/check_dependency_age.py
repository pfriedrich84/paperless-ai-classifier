#!/usr/bin/env python3
"""Verify that all installed Python packages are at least N days old.

Supply-chain protection: newly published packages are more likely to be
compromised.  This script queries PyPI for the upload date of each installed
version and fails if any package is younger than the configured threshold.

Packages younger than the threshold are automatically allowed if they fix
a known CVE (verified via the OSV.dev API).  Manual exceptions can still
be listed in an allowlist file.

Usage:
    python scripts/check_dependency_age.py [--min-days 3] [--allowlist .dependency-age-allowlist]

Exit codes:
    0  all packages pass
    1  one or more packages are too new (without CVE justification)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime

# Packages we don't publish to PyPI (local editable installs, etc.)
SKIP_PACKAGES = frozenset(
    {
        "paperless-ai-classifier",
        "pip",
        "setuptools",
        "wheel",
        "pkg-resources",
    }
)


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
        return datetime.fromisoformat(ts).replace(tzinfo=UTC)
    except Exception:
        return None


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse version string to comparable tuple, ignoring non-numeric suffixes."""
    parts = []
    for segment in v.split("."):
        digits = ""
        for ch in segment:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def check_cve_fix(name: str, version: str) -> list[str]:
    """Query OSV.dev whether *version* of *name* fixes any known vulnerability.

    Returns a list of CVE/advisory IDs that this version fixes.
    An empty list means no known CVE fix — the package stays blocked.
    """
    url = "https://api.osv.dev/v1/query"
    payload = json.dumps({"package": {"name": name, "ecosystem": "PyPI"}}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception:
        return []

    our_version = _parse_version(version)
    fixed_cves: list[str] = []

    for vuln in result.get("vulns", []):
        vuln_id = vuln.get("id", "")
        for affected in vuln.get("affected", []):
            pkg = affected.get("package", {})
            if pkg.get("ecosystem", "") != "PyPI":
                continue
            if pkg.get("name", "").lower() != name.lower():
                continue
            for rng in affected.get("ranges", []):
                if rng.get("type") != "ECOSYSTEM":
                    continue
                for event in rng.get("events", []):
                    fixed_ver = event.get("fixed")
                    if not fixed_ver:
                        continue
                    # Our version is a fix if it's >= the listed fix version
                    if our_version >= _parse_version(fixed_ver):
                        fixed_cves.append(vuln_id)
    return list(dict.fromkeys(fixed_cves))  # deduplicate, preserve order


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-days",
        type=int,
        default=3,
        help="Minimum age in days (default: 3)",
    )
    parser.add_argument(
        "--allowlist",
        default=".dependency-age-allowlist",
        help="Path to allowlist file for security-patch exceptions",
    )
    parser.add_argument(
        "--no-auto-cve",
        action="store_true",
        help="Disable automatic CVE-fix detection via OSV.dev",
    )
    args = parser.parse_args()

    allowlist = load_allowlist(args.allowlist)
    if allowlist:
        print(f"Loaded {len(allowlist)} allowlisted exception(s)")

    now = datetime.now(UTC)
    packages = get_installed_packages()
    violations: list[tuple[str, str, datetime, int]] = []
    auto_allowed: list[tuple[str, str, int, list[str]]] = []
    checked = 0

    for name, version in packages:
        if name.lower() in {s.lower() for s in SKIP_PACKAGES}:
            continue
        if (name.lower(), version) in allowlist:
            continue

        release_date = get_release_date(name, version)
        if release_date is None:
            continue

        checked += 1
        age_days = (now - release_date).days
        if age_days >= args.min_days:
            continue

        # Package is too new — check if it fixes a known CVE
        if not args.no_auto_cve:
            cves = check_cve_fix(name, version)
            if cves:
                auto_allowed.append((name, version, age_days, cves))
                continue

        violations.append((name, version, release_date, age_days))

    print(f"Checked {checked} packages (min age: {args.min_days} days)")

    if auto_allowed:
        print(f"\nAuto-allowed {len(auto_allowed)} package(s) (CVE security fixes):")
        for name, version, age, cves in auto_allowed:
            cve_list = ", ".join(cves[:3])
            if len(cves) > 3:
                cve_list += f" (+{len(cves) - 3} more)"
            print(f"  {name}=={version} ({age}d old) fixes {cve_list}")

    if violations:
        print(f"\n{'=' * 70}")
        print(f"FAILED: {len(violations)} package(s) younger than {args.min_days} days")
        print(f"{'=' * 70}\n")
        print(f"{'Package':<30} {'Version':<15} {'Released':<12} {'Age':<6}")
        print("-" * 70)
        for name, version, release_date, age in sorted(violations, key=lambda x: x[3]):
            print(f"{name:<30} {version:<15} {release_date:%Y-%m-%d}   {age}")
        print(f"\n{'=' * 70}")
        print("Supply-chain protection: New packages are quarantined for")
        print(f"{args.min_days} days to allow the community to detect compromised releases.")
        print()
        print("Packages that fix a known CVE are auto-allowed (via OSV.dev).")
        print("If auto-detection missed a valid security fix, add manually:")
        print("  .dependency-age-allowlist:")
        print("  package==version  # CVE-XXXX-XXXXX (released YYYY-MM-DD, remove after YYYY-MM-DD)")
        print(f"{'=' * 70}")
        return 1

    print(f"OK — all {checked} packages are at least {args.min_days} days old.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
