#!/usr/bin/env python3
"""Trim the CV until it fits on one page.

    python3 scripts/fit.py --role ios [--pages 1]

Renders and compiles repeatedly, lowering the document-wide bullet budget
one step at a time until the PDF reaches the target page count. Reports
exactly which bullets were sacrificed, because a CV that silently drops
your best line is worse than one that runs long.

Bullets are removed lowest-score-first, and render.py refuses to take a
job below two bullets, so trimming eats into projects and extras before it
guts your experience section.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXBIN = Path("/Library/TeX/texbin")
LATEXMK = str(TEXBIN / "latexmk") if (TEXBIN / "latexmk").exists() else "latexmk"


def env() -> dict[str, str]:
    import os
    e = dict(os.environ)
    if TEXBIN.is_dir():
        e["PATH"] = f"{TEXBIN}{os.pathsep}{e.get('PATH', '')}"
    return e


def render(role: str, budget: int | None) -> str:
    cmd = [sys.executable, "scripts/render.py", "--role", role]
    if budget is not None:
        cmd += ["--budget", str(budget)]
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "render failed")
    return result.stdout


def compile_pdf() -> int:
    result = subprocess.run(
        [LATEXMK, "-pdf", "-interaction=nonstopmode", "-file-line-error",
         "-outdir=build", "main.tex"],
        cwd=ROOT, text=True, capture_output=True, env=env(),
    )
    log = (ROOT / "build/main.log")
    if not log.is_file():
        raise SystemExit(result.stdout[-1500:] or "compile produced no log")
    raw = re.sub(r"\n", "", log.read_text(errors="replace"))
    m = re.search(r"Output written on .*?\((\d+) pages?", raw)
    if not m:
        raise SystemExit("could not determine page count; check build/main.log")
    return int(m.group(1))


def count_bullets(render_output: str) -> int:
    m = re.search(r"bullets=(\d+)", render_output)
    return int(m.group(1)) if m else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--pages", type=int, default=1)
    args = parser.parse_args()

    out = render(args.role, None)
    total = count_bullets(out)
    pages = compile_pdf()
    print(f"{args.role}: {total} bullets -> {pages} page(s)")

    if pages <= args.pages:
        print(f"already fits in {args.pages} page(s); nothing trimmed")
        return 0

    budget = total
    while budget > 1:
        budget -= 1
        out = render(args.role, budget)
        pages = compile_pdf()
        kept = count_bullets(out)
        print(f"  budget={budget:2d}  bullets={kept:2d}  pages={pages}")
        if pages <= args.pages:
            print(f"\nfits at {kept} bullets ({total - kept} trimmed)")
            print("run `make explain ROLE=%s` to see what was dropped and why" % args.role)
            return 0

    print("could not reach the target page count by trimming bullets alone;")
    print("consider lowering limits in roles/%s.toml or shortening long bullets" % args.role)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
