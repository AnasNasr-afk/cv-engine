#!/usr/bin/env python3
"""Check that the monthly loop behaves the way it is supposed to.

    python3 scripts/verify_loop.py --month 2026-08

Runs the collection and matching steps against a real month and asserts the
four properties the loop depends on. Uses a scratch copy of content/, so it
never touches the CV, and never calls Claude, so it is free and repeatable.

  1. COLLECTS      the month yields work units at all
  2. DEDUPES       commits rebased across branches collapse into one unit
  3. SUBTRACTS     once work is cited as evidence, a re-run proposes nothing
                   -- the property that stops the loop repeating itself
  4. RENDERS       every role still builds to a single page

Property 3 is the one worth caring about. Without it the loop re-proposes
the same commits every month until you stop reading the pull requests.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=ROOT, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout


def collect(month: str, content: Path, out: Path) -> dict:
    code, log = run([sys.executable, "scripts/collect_month.py", "--month", month,
                     "--content", str(content), "--output", str(out)])
    if code != 0:
        raise SystemExit(f"collect_month failed:\n{log}")
    return json.loads(out.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--month", required=True)
    args = parser.parse_args()

    checks: list[tuple[str, bool, str]] = []
    tmp = Path(tempfile.mkdtemp(prefix="cv-verify-"))
    try:
        content = tmp / "content"
        shutil.copytree(ROOT / "content", content)

        first = collect(args.month, content, tmp / "d1.json")
        new_units = first["units_new"]
        checks.append(("COLLECTS", new_units > 0,
                       f"{new_units} new work units across "
                       f"{first['repos_with_work']} repos"))

        # Assert the output contains no duplicates, rather than that this
        # particular month happened to have some. A quiet month with no
        # rebases is normal; two units sharing a subject and diffstat is not.
        collapsed = first["duplicates_collapsed"]
        seen: set[tuple] = set()
        dupes = 0
        for entry in first["repositories"]:
            for unit in entry["units"]:
                key = (entry["repo"], unit["subject"], unit["added"],
                       unit["deleted"], unit["file_count"])
                if key in seen:
                    dupes += 1
                seen.add(key)
        checks.append(("DEDUPES", dupes == 0,
                       f"{dupes} duplicate units remain in the output"
                       f" ({collapsed} collapsed this month)"))

        # Claim every unit this month found, exactly as an accepted PR would.
        claimed: dict[str, list[str]] = {}
        for entry in first["repositories"]:
            for unit in entry["units"]:
                claimed.setdefault(entry["repo"], []).extend(unit["shas"])
        refs = [f"{repo}@{sha[:7]}" for repo, shas in claimed.items() for sha in shas]

        target = content / "experience.toml"
        text = target.read_text()
        block = "\n".join(f'    "{r}",' for r in refs)
        text = text.replace('  evidence = []',
                            f'  evidence = [\n{block}\n  ]', 1)
        target.write_text(text)

        second = collect(args.month, content, tmp / "d2.json")
        checks.append(("SUBTRACTS", second["units_new"] == 0,
                       f"re-run proposed {second['units_new']} units "
                       f"(claimed {len(refs)} shas)"))

        code, log = run(["make", "all-roles"])
        pages = log.count("(1 page)")
        built = log.count("built ")
        checks.append(("RENDERS", code == 0 and pages == built and built > 0,
                       f"{pages}/{built} roles at one page"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nmonthly loop — {args.month}\n")
    ok = True
    for name, passed, detail in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name:<10} {detail}")
        ok &= passed
    print()
    if not ok:
        print("The loop is NOT behaving as planned.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
