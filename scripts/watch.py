#!/usr/bin/env python3
"""Rebuild the CV whenever content, roles, styles or scripts change.

    python3 scripts/watch.py --role ios

`latexmk -pvc` only watches files LaTeX itself reads, so it never notices
an edit to content/*.toml -- the source you actually edit. This polls the
real inputs, re-renders, and rebuilds. Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXBIN = Path("/Library/TeX/texbin")
LATEXMK = str(TEXBIN / "latexmk") if (TEXBIN / "latexmk").exists() else "latexmk"

WATCHED = ["content/*.toml", "roles/*.toml", "styles/*.sty", "scripts/render.py", "main.tex"]


def env() -> dict[str, str]:
    e = dict(os.environ)
    if TEXBIN.is_dir():
        e["PATH"] = f"{TEXBIN}{os.pathsep}{e.get('PATH', '')}"
    return e


def fingerprint() -> dict[str, float]:
    stamps: dict[str, float] = {}
    for pattern in WATCHED:
        for path in ROOT.glob(pattern):
            try:
                stamps[str(path)] = path.stat().st_mtime
            except OSError:
                pass
    return stamps


def build(role: str) -> None:
    rendered = subprocess.run(
        [sys.executable, "scripts/render.py", "--role", role],
        cwd=ROOT, text=True, capture_output=True,
    )
    if rendered.returncode != 0:
        print(rendered.stderr.strip() or "render failed")
        return
    print(rendered.stdout.strip().splitlines()[0])
    compiled = subprocess.run(
        [LATEXMK, "-pdf", "-interaction=nonstopmode", "-file-line-error",
         "-synctex=1", "-outdir=build", "main.tex"],
        cwd=ROOT, text=True, capture_output=True, env=env(),
    )
    if compiled.returncode != 0:
        print("  compile FAILED -- see build/main.log")
        for line in compiled.stdout.splitlines():
            if line.startswith("./") and ":" in line:
                print("   ", line)
        return
    dist = ROOT / f"build/Anas_Nasr_Mostafa_CV_{role}.pdf"
    shutil.copyfile(ROOT / "build/main.pdf", dist)
    print(f"  -> {dist.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    print(f"watching content/, roles/, styles/, main.tex for role '{args.role}' -- Ctrl+C to stop")
    build(args.role)
    previous = fingerprint()
    try:
        while True:
            time.sleep(args.interval)
            current = fingerprint()
            if current != previous:
                changed = [Path(p).name for p in current
                           if previous.get(p) != current[p]]
                print(f"\nchanged: {', '.join(sorted(set(changed))) or 'files removed'}")
                build(args.role)
                previous = current
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
