#!/usr/bin/env python3
"""Clone or update every tracked repository into a working directory.

    python3 scripts/fetch_repos.py --into _evidence

Reads `github_repos` from career-loop.json. Used by the monthly workflow so
a runner has the same evidence a local checkout does.

Clones are blobless (`--filter=blob:none`). The collection step is
author-first, so only the author's own commits ever need their file
contents fetched -- a 900 MB iOS repository arrives as roughly 100 MB, and
nothing pays to materialise fourteen thousand commits nobody will read.

Credentials come from GH_TOKEN in the environment and are written into a
git credential helper for the process, never into a remote URL, so a token
cannot end up committed in .git/config or printed in a log line.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode, result.stdout


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        # Supply the token via askpass rather than embedding it in the URL,
        # so it never reaches .git/config, the reflog, or CI output.
        helper = ROOT / ".git-askpass.sh"
        helper.write_text(f'#!/bin/sh\ncase "$1" in\n'
                          f'  Username*) echo "x-access-token" ;;\n'
                          f'  Password*) echo "{token}" ;;\n'
                          f'esac\n')
        helper.chmod(0o700)
        env["GIT_ASKPASS"] = str(helper)
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "career-loop.json")
    parser.add_argument("--into", type=Path, default=ROOT / "_evidence")
    parser.add_argument("--full", action="store_true",
                        help="clone with full blobs instead of blobless")
    args = parser.parse_args()

    config = json.loads(args.config.expanduser().resolve().read_text(encoding="utf-8"))
    repos: list[str] = config.get("github_repos", [])
    if not repos:
        raise SystemExit("error: career-loop.json has no github_repos")

    target = args.into.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    env = git_env()

    ok = failed = 0
    for slug in repos:
        name = slug.split("/")[-1]
        dest = target / name
        url = f"https://github.com/{slug}.git"
        if (dest / ".git").is_dir():
            code, out = run(["git", "-C", str(dest), "fetch", "--all", "--prune", "--quiet"], env=env)
            action = "updated"
        else:
            cmd = ["git", "clone", "--quiet"]
            if not args.full:
                cmd.append("--filter=blob:none")
            cmd += [url, str(dest)]
            code, out = run(cmd, env=env)
            action = "cloned"
        if code == 0:
            ok += 1
            print(f"  {action}: {slug}")
        else:
            failed += 1
            # Never echo `out` verbatim: a failed clone can include the URL
            # with credentials substituted by some git versions.
            print(f"  FAILED:  {slug}  (exit {code})", file=sys.stderr)

    helper = ROOT / ".git-askpass.sh"
    if helper.exists():
        helper.unlink()

    print(f"\n{ok} ready, {failed} failed, into {target.name}/")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
