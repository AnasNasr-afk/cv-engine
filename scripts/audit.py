#!/usr/bin/env python3
"""Check every CV claim against the evidence backing it.

    python3 scripts/audit.py [--verify]

Without --verify this reports which bullets carry no evidence reference at
all. With --verify it additionally resolves each `repo@sha` reference
against the local clones and reports any that do not exist -- catching
typos, rebased-away commits, and references to repositories that are no
longer on disk.

Reference forms understood:
    repo@abc1234              a commit in a local clone
    owner/repo#123            a pull request (existence not checked here)
    repo@N-commits            a deliberate summary reference, counted but
                              not resolvable to a single sha
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
SEARCH_ROOTS = [Path.home() / "career-evidence", Path.home() / "Projects"]

SHA_REF = re.compile(r"^([A-Za-z0-9._-]+)@([0-9a-f]{7,40})$")
SUMMARY_REF = re.compile(r"^([A-Za-z0-9._-]+)@\d+-(commits|prs)$")
PR_REF = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#[\d-]+")


def load(name: str) -> dict[str, Any]:
    with (CONTENT / name).open("rb") as handle:
        return tomllib.load(handle)


def find_repo(name: str) -> Path | None:
    for root in SEARCH_ROOTS:
        candidate = root / name
        if (candidate / ".git").is_dir():
            return candidate
    return None


def sha_exists(repo: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def walk() -> list[tuple[str, str, str, list[str]]]:
    """(section, entry title, bullet text, evidence) for every bullet."""
    rows = []
    for filename, key, title_field in [
        ("education.toml", "entry", "org"),
        ("experience.toml", "entry", "org"),
        ("projects.toml", "entry", "name"),
        ("leadership.toml", "entry", "name"),
    ]:
        section = filename.replace(".toml", "")
        for entry in load(filename).get(key, []):
            title = entry.get(title_field, "?")
            for bullet in entry.get("bullets", []):
                rows.append((section, title, bullet.get("text", ""), bullet.get("evidence", [])))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="resolve repo@sha references against local clones")
    args = parser.parse_args()

    rows = walk()
    unevidenced = [r for r in rows if not r[3]]
    evidenced = len(rows) - len(unevidenced)

    print(f"claims: {len(rows)}   evidenced: {evidenced}   unevidenced: {len(unevidenced)}")

    if unevidenced:
        print("\nNO EVIDENCE -- defensible only from memory, not from a commit:")
        current = None
        for section, title, text, _ in unevidenced:
            if title != current:
                clean = re.sub(r"\\\w+|[{}]", "", title).strip()
                print(f"\n  {clean}  ({section})")
                current = title
            snippet = re.sub(r"\\\w+\{([^}]*)\}", r"\1", text)[:96]
            print(f"    - {snippet}...")

    if args.verify:
        print("\n--- verifying references ---")
        bad, ok, skipped = [], 0, 0
        for section, title, _, evidence in rows:
            for ref in evidence:
                m = SHA_REF.match(ref)
                if m:
                    repo = find_repo(m.group(1))
                    if repo is None:
                        bad.append((ref, "repository not found locally"))
                    elif not sha_exists(repo, m.group(2)):
                        bad.append((ref, "commit not found in repository"))
                    else:
                        ok += 1
                elif SUMMARY_REF.match(ref) or PR_REF.match(ref):
                    skipped += 1
                else:
                    bad.append((ref, "unrecognised reference format"))
        print(f"resolved: {ok}   summary/PR refs (not resolved): {skipped}   broken: {len(bad)}")
        for ref, why in bad:
            print(f"  BROKEN  {ref}  -- {why}")
        if bad:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
