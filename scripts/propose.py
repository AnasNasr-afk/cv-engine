#!/usr/bin/env python3
"""Match a month's new work onto existing CV threads.

    python3 scripts/propose.py --digest digest-2026-08.json

A thread is a bullet with a stable `id`. New work either belongs to a
thread already on the CV -- in which case that one line should be rewritten
to be stronger -- or it starts a new one. This decides which, and produces
the brief the drafting step writes wording from.

Matching runs on three signals, strongest first:

  repo      work in the same repository as a thread's existing evidence
  paths     overlapping directories with the commits that thread cites
  words     shared distinctive terms between the unit subject and the line

Paths carry the most weight because they are the least gameable: two pieces
of work touching `Managers/SavedSearchReminder/` are the same thread
whatever the commit messages say. Word overlap alone is never enough to
attach -- it only breaks ties between path-plausible candidates.

Unattached units are clustered by shared directory into candidate new
threads, so a month of related work arrives as one proposal rather than
nine. Nothing here writes wording; that judgement belongs to the drafting
step, which needs the diffs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SHA_REF = re.compile(r"^([A-Za-z0-9._-]+)@([0-9a-f]{7,40})$")

#: Terms too common in commit subjects to indicate a shared thread.
STOPWORDS = {
    "add", "added", "adds", "fix", "fixed", "fixes", "update", "updated",
    "remove", "removed", "refactor", "chore", "feat", "merge", "branch",
    "the", "and", "for", "with", "into", "from", "that", "this", "new",
    "use", "using", "make", "made", "set", "support", "improve", "better",
    "pull", "request", "staging", "main", "wip", "test", "tests",
}

ATTACH_THRESHOLD = 2.0   # below this, the unit starts its own thread
PATH_WEIGHT      = 3.0
REPO_WEIGHT      = 1.0
WORD_WEIGHT      = 0.5


def load_threads(content: Path) -> list[dict[str, Any]]:
    """Every bullet that carries an id, with the section it lives in."""
    threads = []
    for name in ("education.toml", "experience.toml", "projects.toml", "leadership.toml"):
        path = content / name
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for entry in data.get("entry", []):
            owner = entry.get("org") or entry.get("name") or "?"
            for bullet in entry.get("bullets", []):
                if bullet.get("id"):
                    threads.append({
                        "id": bullet["id"],
                        "section": name.replace(".toml", ""),
                        "owner": owner,
                        "text": bullet.get("text", ""),
                        "tags": bullet.get("tags", []),
                        "evidence": bullet.get("evidence", []),
                    })
    return threads


def words(value: str) -> set[str]:
    raw = re.sub(r"\\[a-zA-Z]+|[{}$\\]", " ", value).lower()
    return {w for w in re.findall(r"[a-z][a-z0-9-]{2,}", raw) if w not in STOPWORDS}


def dirs_of(paths: list[str], depth: int = 3) -> set[str]:
    out = set()
    for path in paths:
        parts = Path(path).parts[:-1][:depth]
        for i in range(1, len(parts) + 1):
            out.add("/".join(parts[:i]))
    return out


def evidence_paths(repos: dict[str, Path], evidence: list[str]) -> tuple[set[str], set[str]]:
    """Directories and repo names behind a thread's cited commits."""
    paths: set[str] = set()
    names: set[str] = set()
    for ref in evidence:
        m = SHA_REF.match(str(ref))
        if not m:
            # PR or summary reference: the repo name is still usable
            if "/" in str(ref):
                names.add(str(ref).split("/")[-1].split("#")[0])
            elif "@" in str(ref):
                names.add(str(ref).split("@")[0])
            continue
        name, sha = m.group(1), m.group(2)
        names.add(name)
        repo = repos.get(name)
        if not repo:
            continue
        out = subprocess.run(
            ["git", "-C", str(repo), "show", "--name-only", "--format=", sha],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ).stdout
        paths |= dirs_of([line for line in out.splitlines() if line])
    return paths, names


def score(unit: dict[str, Any], repo_name: str, thread: dict[str, Any],
          th_paths: set[str], th_repos: set[str]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    total = 0.0

    same_repo = repo_name in th_repos
    if same_repo:
        total += REPO_WEIGHT
        reasons.append(f"same repo ({repo_name})")

    # Path overlap only means something inside one repository. Directory
    # names like src/, lib/ and src/app exist in nearly every project, so
    # across repos they bridge unrelated work -- a Next.js hackathon build
    # matched a Vite video feed purely on both having src/app.
    shared = dirs_of(unit["paths"]) & th_paths if same_repo else set()
    if shared:
        deepest = max(shared, key=lambda d: d.count("/"))
        total += PATH_WEIGHT * min(len(shared), 3) / 3
        reasons.append(f"shares {deepest}")

    common = words(unit["subject"]) & words(thread["text"])
    if common:
        total += WORD_WEIGHT * min(len(common), 3)
        reasons.append("terms: " + ", ".join(sorted(common)[:3]))

    return total, reasons


def cluster(units: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Group unattached units by their deepest shared directory."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for repo_name, unit in units:
        dirs = dirs_of(unit["paths"], depth=2)
        key = max(dirs, key=lambda d: (d.count("/"), len(d))) if dirs else "(root)"
        buckets[(repo_name, key)].append(unit)

    clusters = []
    for (repo_name, key), members in buckets.items():
        members.sort(key=lambda u: -u["added"])
        clusters.append({
            "repo": repo_name,
            "area": key,
            "unit_count": len(members),
            "added": sum(u["added"] for u in members),
            "shas": [s for u in members for s in u["shas"]],
            "subjects": [u["subject"] for u in members],
        })
    clusters.sort(key=lambda c: -c["added"])
    return clusters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--digest", required=True, type=Path)
    parser.add_argument("--content", type=Path, default=ROOT / "content")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-low", action="store_true",
                        help="consider units marked low-signal too")
    args = parser.parse_args()

    digest = json.loads(args.digest.expanduser().resolve().read_text(encoding="utf-8"))
    threads = load_threads(args.content.resolve())

    repos: dict[str, Path] = {}
    for entry in digest["repositories"]:
        repos[entry["repo"]] = Path(entry["path"])
    for thread in threads:
        for ref in thread["evidence"]:
            m = SHA_REF.match(str(ref))
            if m and m.group(1) not in repos:
                for base in (Path.home() / "career-evidence", Path.home() / "Projects"):
                    if (base / m.group(1) / ".git").is_dir():
                        repos[m.group(1)] = base / m.group(1)
                        break

    context = {t["id"]: evidence_paths(repos, t["evidence"]) for t in threads}

    attach: list[dict[str, Any]] = []
    orphans: list[tuple[str, dict[str, Any]]] = []
    skipped = 0

    for entry in digest["repositories"]:
        for unit in entry["units"]:
            if unit["signal"] == "low" and not args.include_low:
                skipped += 1
                continue
            best, best_score, best_reasons = None, 0.0, []
            for thread in threads:
                th_paths, th_repos = context[thread["id"]]
                value, reasons = score(unit, entry["repo"], thread, th_paths, th_repos)
                if value > best_score:
                    best, best_score, best_reasons = thread, value, reasons
            if best and best_score >= ATTACH_THRESHOLD:
                attach.append({
                    "thread": best["id"], "section": best["section"], "owner": best["owner"],
                    "current_text": best["text"], "repo": entry["repo"],
                    "unit": unit, "score": round(best_score, 2), "why": best_reasons,
                })
            else:
                orphans.append((entry["repo"], unit))

    clusters = cluster(orphans)
    payload = {
        "version": 1,
        "window": digest["window"],
        "threads_known": len(threads),
        "units_considered": len(attach) + len(orphans),
        "low_signal_skipped": skipped,
        "attach_to_existing": attach,
        "candidate_new_threads": clusters,
    }

    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        out = args.output.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)

    print(f"\n{digest['window']['label']}: {len(attach)} units attach to existing threads, "
          f"{len(clusters)} candidate new threads ({skipped} low-signal skipped)")

    by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in attach:
        by_thread[item["thread"]].append(item)

    if by_thread:
        print("\nREWRITE these threads:")
        for tid, items in sorted(by_thread.items(), key=lambda kv: -len(kv[1])):
            print(f"\n  {tid}  ({len(items)} new unit{'s' if len(items) > 1 else ''})")
            print(f"    now: {re.sub(r'[\\\\][a-zA-Z]+|[{}]', '', items[0]['current_text'])[:78]}")
            for item in items:
                print(f"    +    {item['unit']['subject'][:62]}")
                print(f"         score {item['score']} — {'; '.join(item['why'])}")

    if clusters:
        print("\nNEW threads proposed:")
        for c in clusters:
            print(f"\n  {c['repo']} :: {c['area']}   ({c['unit_count']} units, +{c['added']})")
            for s in c["subjects"][:4]:
                print(f"    - {s[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
