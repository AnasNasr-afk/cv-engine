#!/usr/bin/env python3
"""Author-first commit extraction for large shared repositories.

The git-career-resume-loop scanner walks every commit in a repo and reads
its diffstat before filtering by author. That is fine for personal repos,
but on a shared company repo with tens of thousands of commits it is both
enormously wasteful and incompatible with a blobless partial clone -- it
would hydrate every blob in the repository to find a handful of commits.

This helper inverts the order: ask git for the author's commits first, then
read only those diffs. Output is shaped like the scanner's per-repository
report so it can be merged into the same evidence pass.

Usage:
    python3 scripts/scan_large_repo.py REPO --config career-loop.json \
        [--output scan-large.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

COAUTHOR_RE = re.compile(r"^Co-authored-by:\s*(.+?)\s*<([^>]+)>\s*$", re.IGNORECASE | re.MULTILINE)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout


def author_commits(repo: Path, patterns: list[str]) -> list[str]:
    """Commits on any ref whose author or co-author matches a pattern.

    Uses git's own --author filter (a regex) per pattern, which avoids
    materialising the whole history. Results are unioned and de-duplicated
    while preserving chronological order.
    """
    seen: dict[str, None] = {}
    for pattern in patterns:
        out = git(
            repo, "log", "--all", "--reverse", "--regexp-ignore-case",
            f"--author={pattern}", "--format=%H", check=False,
        )
        for sha in out.splitlines():
            if sha:
                seen.setdefault(sha, None)
    # Co-authored trailers are not covered by --author, so sweep those too.
    for pattern in patterns:
        out = git(
            repo, "log", "--all", "--reverse", "--regexp-ignore-case",
            f"--grep=Co-authored-by:.*{pattern}", "--format=%H", check=False,
        )
        for sha in out.splitlines():
            if sha:
                seen.setdefault(sha, None)
    return list(seen)


def commit_record(repo: Path, sha: str) -> dict[str, Any] | None:
    fmt = "%H%x00%aI%x00%an%x00%ae%x00%s%x00%P"
    meta = git(repo, "show", "-s", f"--format={fmt}", "--no-show-signature", sha, check=False)
    parts = meta.rstrip("\n").split("\x00")
    if len(parts) != 6:
        return None

    files: list[dict[str, Any]] = []
    stats = subprocess.run(
        ["git", "-C", str(repo), "show", "--numstat", "--format=", "--no-renames", sha, "--"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if stats.returncode != 0:
        # Typically a blobless clone that could not lazily fetch. Record the
        # commit without file stats rather than dropping the evidence.
        files = []
        incomplete = stats.stderr.strip()[:200] or "diffstat unavailable"
    else:
        incomplete = ""
        for line in stats.stdout.splitlines():
            columns = line.split("\t", 2)
            if len(columns) != 3:
                continue
            added, deleted, path = columns
            files.append({
                "path": path,
                "added": None if added == "-" else int(added),
                "deleted": None if deleted == "-" else int(deleted),
                "binary": added == "-" or deleted == "-",
            })

    body = git(repo, "show", "-s", "--format=%B", "--no-show-signature", sha, check=False)
    record: dict[str, Any] = {
        "sha": parts[0],
        "authored_at": parts[1],
        "author_name": parts[2],
        "author_email": parts[3],
        "subject": parts[4],
        "parents": parts[5].split() if parts[5] else [],
        "co_authors": [
            {"name": m.group(1).strip(), "email": m.group(2).strip()}
            for m in COAUTHOR_RE.finditer(body)
        ],
        "files": files,
        "attribution": "author",
    }
    if incomplete:
        record["incomplete"] = incomplete
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    config = json.loads(args.config.expanduser().resolve().read_text(encoding="utf-8"))
    patterns = config.get("author_patterns") or []
    if not patterns:
        raise SystemExit("error: config has no author_patterns")

    toplevel = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if toplevel.returncode != 0:
        raise SystemExit(f"error: not a git worktree: {repo}")
    repo = Path(toplevel.stdout.strip()).resolve()

    shas = author_commits(repo, patterns)
    records = [r for r in (commit_record(repo, s) for s in shas) if r]
    incomplete = sum(1 for r in records if "incomplete" in r)

    payload = {
        "version": 1,
        "method": "author-first",
        "path": str(repo),
        "scanned_commits": len(shas),
        "attributed_commits": len(records),
        "incomplete_diffstats": incomplete,
        "commits": records,
    }
    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        out = args.output.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
        print(f"{repo.name}: {len(records)} commits"
              + (f" ({incomplete} without diffstat)" if incomplete else ""))
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
