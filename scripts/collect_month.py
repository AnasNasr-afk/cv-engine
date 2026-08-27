#!/usr/bin/env python3
"""Collect a month's genuinely-new work across every tracked repository.

    python3 scripts/collect_month.py --month 2026-08
    python3 scripts/collect_month.py --since 2026-07-01 --until 2026-09-01

Produces the digest the monthly pull request is built from. Three things
make the output usable rather than a commit dump:

1. **Author-first.** Repositories are queried for the author's commits
   directly, never walked in full. A shared repo with 14,000 commits costs
   the same as a personal one, and a blobless clone works because only the
   author's own commits are ever materialised.

2. **Deduped.** The same work rebased across release branches appears as
   many commits with identical subject and diffstat. They collapse into one
   work unit carrying every sha, so a month's output reflects what was
   done, not how many branches it landed on.

3. **Subtracted.** Commits already referenced as evidence in content/ are
   dropped. This is what stops the loop re-proposing the same work every
   month, and it is why evidence ids are worth carrying.

Work units are also marked low-signal when the change is a merge, a
lockfile bump, or generated output -- flagged rather than discarded, since
the judgement of what matters belongs downstream.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXCLUDES = ["**/.git/**", "**/node_modules/**", "**/vendor/**", "**/.cache/**"]

#: Paths whose changes rarely say anything about engineering judgement.
GENERATED = [
    "*.lock", "*-lock.json", "*.lockb", "Podfile.lock", "pubspec.lock",
    "*.pbxproj", "*.xcworkspacedata", "*.resolved",
    "**/generated/**", "**/*.g.dart", "**/*.freezed.dart", "**/*.pb.go",
    "**/dist/**", "**/build/**", "**/.next/**",
]

SHA_REF = re.compile(r"^([A-Za-z0-9._-]+)@([0-9a-f]{7,40})$")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout if result.returncode == 0 else ""


def excluded(path: Path, patterns: list[str]) -> bool:
    value = path.as_posix()
    return any(fnmatch.fnmatch(value, p) or fnmatch.fnmatch(value + "/", p) for p in patterns)


def resolve(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def discover(config: dict[str, Any], base: Path) -> list[Path]:
    """Every git worktree named explicitly or found under a configured root."""
    repos: set[Path] = set()
    patterns = DEFAULT_EXCLUDES + list(config.get("exclude_patterns", []))
    max_depth = int(config.get("max_discovery_depth", 3))

    for raw in config.get("repositories", []):
        repo = resolve(raw, base)
        top = git(repo, "rev-parse", "--show-toplevel").strip()
        if top:
            repos.add(Path(top).resolve())

    for raw in config.get("repository_roots", []):
        root = resolve(raw, base)
        if not root.is_dir():
            continue
        root_depth = len(root.parts)
        for current, dirs, files in os.walk(root):
            here = Path(current)
            if ".git" in dirs or ".git" in files:
                if not excluded(here, patterns):
                    top = git(here, "rev-parse", "--show-toplevel").strip()
                    if top:
                        repos.add(Path(top).resolve())
                dirs[:] = []
                continue
            depth = len(here.parts) - root_depth
            dirs[:] = [d for d in dirs
                       if depth < max_depth and not excluded(here / d, patterns)]
    return sorted(repos)


def claimed_shas(content_dir: Path) -> set[tuple[str, str]]:
    """(repo, sha-prefix) pairs already cited as evidence somewhere in content/."""
    claimed: set[tuple[str, str]] = set()
    for path in sorted(content_dir.glob("*.toml")):
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "evidence" and isinstance(value, list):
                        for ref in value:
                            m = SHA_REF.match(str(ref))
                            if m:
                                claimed.add((m.group(1), m.group(2)))
                    else:
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
    return claimed


def is_claimed(repo_name: str, sha: str, claimed: set[tuple[str, str]]) -> bool:
    for name, prefix in claimed:
        if name == repo_name and (sha.startswith(prefix) or prefix.startswith(sha)):
            return True
    return False


def generated_path(path: str) -> bool:
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch("/" + path, p) for p in GENERATED)


def commits_in_window(repo: Path, patterns: list[str], since: str, until: str) -> list[str]:
    seen: dict[str, None] = {}
    for pattern in patterns:
        out = git(repo, "log", "--all", "--reverse", "--regexp-ignore-case",
                  f"--author={pattern}", f"--since={since}", f"--until={until}",
                  "--format=%H")
        for sha in out.splitlines():
            if sha:
                seen.setdefault(sha, None)
    return list(seen)


def describe(repo: Path, sha: str) -> dict[str, Any] | None:
    meta = git(repo, "show", "-s", "--format=%H%x00%aI%x00%s%x00%P",
               "--no-show-signature", sha).rstrip("\n")
    parts = meta.split("\x00")
    if len(parts) != 4:
        return None

    files: list[dict[str, Any]] = []
    for line in git(repo, "show", "--numstat", "--format=", "--no-renames", sha, "--").splitlines():
        cols = line.split("\t", 2)
        if len(cols) != 3:
            continue
        added, deleted, path = cols
        files.append({
            "path": path,
            "added": None if added == "-" else int(added),
            "deleted": None if deleted == "-" else int(deleted),
        })

    parents = parts[3].split() if parts[3] else []
    added = sum(f["added"] or 0 for f in files)
    deleted = sum(f["deleted"] or 0 for f in files)

    # Any merge is low signal, with or without a diffstat. Merging your own
    # branch is not a second piece of work, and `git show --numstat` on a
    # merge reports the incoming changes -- which would otherwise double
    # every feature: once for the commit, once for the merge that landed it.
    if len(parents) > 1:
        signal, why = "low", "merge commit"
    elif files and all(generated_path(f["path"]) for f in files):
        signal, why = "low", "only generated or lockfile paths"
    elif not files:
        signal, why = "low", "no file changes"
    else:
        signal, why = "normal", ""

    return {
        "sha": parts[0], "date": parts[1][:10], "subject": parts[2],
        "parents": parents, "files": files,
        "added": added, "deleted": deleted, "file_count": len(files),
        "signal": signal, "signal_reason": why,
    }


def dedupe(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same work landed on several branches into one unit.

    A rebase or cherry-pick preserves subject and diffstat but changes the
    sha, so those three together identify the work rather than the commit.
    """
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for commit in commits:
        groups[(commit["subject"], commit["added"], commit["deleted"], commit["file_count"])].append(commit)

    units = []
    for members in groups.values():
        members.sort(key=lambda c: c["date"])
        first = members[0]
        units.append({
            "subject": first["subject"],
            "date": first["date"],
            "shas": [m["sha"] for m in members],
            "duplicate_of_count": len(members),
            "added": first["added"],
            "deleted": first["deleted"],
            "file_count": first["file_count"],
            "paths": [f["path"] for f in first["files"][:20]],
            "signal": first["signal"],
            "signal_reason": first["signal_reason"],
        })
    units.sort(key=lambda u: (u["date"], -u["added"]))
    return units


def month_window(month: str) -> tuple[str, str]:
    year, mon = (int(x) for x in month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d} 23:59:59"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "career-loop.json")
    parser.add_argument("--content", type=Path, default=ROOT / "content")
    parser.add_argument("--month", help="YYYY-MM")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-claimed", action="store_true",
                        help="keep work already cited as evidence (for backfill inspection)")
    args = parser.parse_args()

    if args.month:
        since, until = month_window(args.month)
        label = args.month
    elif args.since and args.until:
        since, until, label = args.since, args.until, f"{args.since}..{args.until}"
    else:
        raise SystemExit("error: pass --month YYYY-MM, or both --since and --until")

    config_path = args.config.expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    patterns = config.get("author_patterns") or []
    if not patterns:
        raise SystemExit("error: config has no author_patterns")

    claimed = set() if args.include_claimed else claimed_shas(args.content.resolve())
    repos = discover(config, config_path.parent)

    report: list[dict[str, Any]] = []
    total_units = total_new = total_dupes = 0

    low_repos = set(config.get("low_signal_repos", []))

    for repo in repos:
        shas = commits_in_window(repo, patterns, since, until)
        if not shas:
            continue
        described = [d for d in (describe(repo, s) for s in shas) if d]
        if repo.name in low_repos:
            # Practice, solutions and tutorial repos generate real commits
            # that say nothing about engineering judgement.
            for d in described:
                d["signal"] = "low"
                d["signal_reason"] = d["signal_reason"] or "repository marked low-signal"
        units = dedupe(described)
        total_dupes += sum(u["duplicate_of_count"] - 1 for u in units)

        fresh = [u for u in units
                 if not any(is_claimed(repo.name, s, claimed) for s in u["shas"])]
        total_units += len(units)
        total_new += len(fresh)
        if fresh:
            report.append({
                "repo": repo.name, "path": str(repo),
                "units_total": len(units), "units_new": len(fresh),
                "units": fresh,
            })

    payload = {
        "version": 1,
        "window": {"label": label, "since": since, "until": until},
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repos_with_work": len(report),
        "units_total": total_units,
        "units_new": total_new,
        "duplicates_collapsed": total_dupes,
        "already_claimed": total_units - total_new,
        "repositories": report,
    }

    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        out = args.output.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    print(f"\n{label}: {total_new} new work units across {len(report)} repos"
          f"  ({total_dupes} duplicates collapsed,"
          f" {total_units - total_new} already on the CV)", file=sys.stderr)
    for entry in report:
        print(f"\n  {entry['repo']}  ({entry['units_new']} new)", file=sys.stderr)
        for unit in entry["units"]:
            mark = " ·low" if unit["signal"] == "low" else "     "
            dup = f" ×{unit['duplicate_of_count']}" if unit["duplicate_of_count"] > 1 else ""
            print(f"   {mark} {unit['date']}  +{unit['added']:<5} "
                  f"{unit['subject'][:64]}{dup}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
