#!/usr/bin/env python3
"""Turn matched work into proposed thread wording.

    python3 scripts/draft.py --proposals proposals-2026-08.json
    python3 scripts/draft.py --proposals ... --apply

Reads the real diffs behind each attached work unit and asks Claude to
rewrite that thread's line so it reflects what the code now shows. Writes
DRAFT.md for review and, with --apply, edits content/*.toml in place.

Two deliberate limits:

* **Only rewrites are applied.** New-thread suggestions go to DRAFT.md for
  a human to place, because deciding that a piece of work belongs under a
  particular job -- or is not CV material at all -- is a judgement the diff
  cannot settle.

* **It degrades instead of failing.** If the `claude` CLI is missing or a
  call fails, the brief is still written with commits, paths and diff
  excerpts. You lose proposed wording, not the month's collection.

Claims are constrained hard: no invented metrics, no ownership or scale the
diff does not show, and anything the model cannot support has to be listed
under `unverifiable` rather than written into the line.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

MAX_FILES_PER_UNIT = 6
MAX_LINES_PER_FILE = 60
CALL_TIMEOUT = 240

SHA_REF_DRAFT = re.compile(r"^([A-Za-z0-9._-]+)@([0-9a-f]{7,40})$")

SKIP_DIFF = re.compile(r"\.(lock|png|jpg|jpeg|pdf|otf|ttf|woff2?|svg|ico|strings)$|"
                       r"(^|/)(dist|build|generated|\.next)/", re.I)

PROMPT = """You are maintaining a one-page engineering CV. Rewrite ONE line of it.

The line describes an ongoing thread of work. New commits have landed on that
thread. Rewrite the line so it reflects what the code actually shows now.

CURRENT LINE
{current}

TAGS: {tags}

EARLIER WORK ALREADY ON THIS THREAD (context — do not re-describe as new)
{prior}

NEW WORK ON THIS THREAD
{units}

RULES
- The current line was written and approved by the CV owner, who knows
  things the diff cannot show. Preserve its existing claims. Remove one only
  if the new work actively contradicts it, and say so in `why`. Absence of
  corroboration is not contradiction.
- Never invent metrics, percentages, user counts, revenue, team size, or
  scale. If the diff does not show it, it does not go in the line.
- Do not claim ownership, leadership, or production impact unless the diff
  demonstrates it. Writing code in a repo is not owning a system.
- Name real mechanisms from the diff (types, patterns, services) where they
  make the line concrete. Prefer specifics over adjectives.
- The line must fit on one CV line: roughly 20-40 words. It replaces the
  current line entirely; it is not appended to it.
- LaTeX markup is allowed and expected: \\textbf{{...}} for emphasis. Write a
  literal percent as \\%.
- If the new work does not make the line stronger, return action "keep".
- Anything you considered saying but could not support from the diff goes in
  `unverifiable`, not in the line.

Reply with ONLY a JSON object, no prose, no code fence:
{{"action": "rewrite" | "keep",
  "text": "the rewritten line, or the unchanged line if keeping",
  "why": "one sentence on what changed and why",
  "confidence": "high" | "medium" | "low",
  "unverifiable": ["claims you could not support from the diff"]}}
"""


def run(cmd: list[str], cwd: Path | None = None, stdin: str | None = None,
        timeout: int | None = None) -> tuple[int, str, str]:
    result = subprocess.run(cmd, cwd=cwd, input=stdin, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def diff_excerpt(repo: Path, sha: str) -> str:
    """A bounded, source-only view of one commit."""
    code, out, _ = run(["git", "-C", str(repo), "show", "--name-only", "--format=", sha])
    if code != 0:
        return "  (diff unavailable)"
    paths = [p for p in out.splitlines() if p and not SKIP_DIFF.search(p)]
    if not paths:
        return "  (no source files changed)"

    chunks = []
    for path in paths[:MAX_FILES_PER_UNIT]:
        code, body, _ = run(["git", "-C", str(repo), "show", "--format=",
                             "--unified=2", sha, "--", path])
        if code != 0 or not body.strip():
            continue
        lines = [l for l in body.splitlines()
                 if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        if not lines:
            continue
        shown = lines[:MAX_LINES_PER_FILE]
        more = len(lines) - len(shown)
        chunks.append(f"  --- {path}\n" + "\n".join("  " + l for l in shown)
                      + (f"\n  ... {more} more changed lines" if more > 0 else ""))
    hidden = len(paths) - MAX_FILES_PER_UNIT
    if hidden > 0:
        chunks.append(f"  ... {hidden} more files changed")
    return "\n".join(chunks) if chunks else "  (no textual changes)"


def prior_context(repos: dict[str, Path], evidence: list[str]) -> str:
    """Subjects and touched areas of the commits a thread already cites.

    Without this the model only ever sees the current month's increment, so
    it cannot correct an inaccuracy in the existing line or tell how large
    the thread already is -- it just re-describes the newest slice.
    """
    lines = []
    for ref in evidence[:8]:
        m = SHA_REF_DRAFT.match(str(ref))
        if not m:
            continue
        repo = repos.get(m.group(1))
        if not repo:
            continue
        code, out, _ = run(["git", "-C", str(repo), "show", "-s", "--format=%s", m.group(2)])
        subject = out.strip() if code == 0 else "(unavailable)"
        code, names, _ = run(["git", "-C", str(repo), "show", "--name-only",
                              "--format=", m.group(2)])
        paths = [p for p in names.splitlines() if p][:5] if code == 0 else []
        lines.append(f"* {subject}\n  {', '.join(paths)}")
    return "\n".join(lines) if lines else "  (none recorded)"


def describe_units(repos: dict[str, Path], items: list[dict[str, Any]]) -> str:
    blocks = []
    for item in items:
        unit = item["unit"]
        repo = repos.get(item["repo"])
        head = (f"* {unit['subject']}\n"
                f"  {unit['date']}  {unit['file_count']} files  "
                f"+{unit['added']}/-{unit['deleted']}  repo: {item['repo']}\n"
                f"  paths: {', '.join(unit['paths'][:8])}")
        body = diff_excerpt(repo, unit["shas"][0]) if repo else "  (repo not found locally)"
        blocks.append(head + "\n" + body)
    return "\n\n".join(blocks)


def ask_claude(prompt: str) -> dict[str, Any] | None:
    if not shutil.which("claude"):
        print("  claude CLI not found — brief only", file=sys.stderr)
        return None
    try:
        code, out, err = run(["claude", "-p"], stdin=prompt, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("  claude timed out", file=sys.stderr)
        return None
    if code != 0:
        print(f"  claude failed: {err.strip()[:200]}", file=sys.stderr)
        return None
    text = out.strip()
    # Tolerate a fenced block even though the prompt asks for bare JSON.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.S)
        if brace:
            text = brace.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  unparseable output: {out.strip()[:160]}", file=sys.stderr)
        return None


def apply_rewrite(thread_id: str, text: str, add_evidence: list[str]) -> bool:
    """Replace one thread's text line and extend its evidence, in place.

    Targeted string surgery rather than a TOML round-trip, because writing
    the file back through a parser would discard every comment in content/.
    """
    for path in sorted(CONTENT.glob("*.toml")):
        source = path.read_text()
        anchor = re.search(rf'^([ \t]*)id(\s*)=\s*"{re.escape(thread_id)}"\s*$', source, re.M)
        if not anchor:
            continue
        indent = anchor.group(1)
        head, rest = source[:anchor.end()], source[anchor.end():]

        text_line = re.search(r'^[ \t]*text(\s*)=\s*"(?:[^"\\]|\\.)*"[ \t]*$', rest, re.M)
        if not text_line:
            return False
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        rest = (rest[:text_line.start()]
                + f'{indent}text{text_line.group(1)}= "{escaped}"'
                + rest[text_line.end():])

        if add_evidence:
            ev = re.search(r'^[ \t]*evidence(\s*)=\s*(\[[^\]]*\])', rest, re.M | re.S)
            if ev:
                try:
                    current = json.loads(re.sub(r",(\s*\])", r"\1", ev.group(2)))
                except json.JSONDecodeError:
                    current = []
                merged = list(dict.fromkeys([*current, *add_evidence]))
                body = "[\n" + "".join(f'{indent}  "{e}",\n' for e in merged) + f"{indent}]"
                rest = (rest[:ev.start()]
                        + f"{indent}evidence{ev.group(1)}= {body}"
                        + rest[ev.end():])

        path.write_text(head + rest)
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "DRAFT.md")
    parser.add_argument("--apply", action="store_true",
                        help="write proposed rewrites into content/*.toml")
    parser.add_argument("--no-claude", action="store_true",
                        help="produce the brief only, without proposed wording")
    parser.add_argument("--from-drafted", type=Path,
                        help="apply a previous run's drafted.json verbatim, without redrafting")
    args = parser.parse_args()

    # Applying a saved draft must not regenerate it: drafting is not
    # deterministic, so re-running would apply wording nobody reviewed.
    if args.from_drafted:
        saved = json.loads(args.from_drafted.expanduser().resolve().read_text(encoding="utf-8"))
        applied = 0
        for item in saved["rewrites"]:
            if apply_rewrite(item["thread"], item["text"], item["evidence"]):
                applied += 1
                print(f"  applied {item['thread']}", file=sys.stderr)
            else:
                print(f"  FAILED to apply {item['thread']}", file=sys.stderr)
        print(f"applied {applied}/{len(saved['rewrites'])} saved rewrites", file=sys.stderr)
        return 0 if applied == len(saved["rewrites"]) else 1

    data = json.loads(args.proposals.expanduser().resolve().read_text(encoding="utf-8"))
    window = data["window"]["label"]

    repos: dict[str, Path] = {}
    def locate(name: str) -> None:
        if name in repos:
            return
        for base in (Path.home() / "career-evidence", Path.home() / "Projects"):
            if (base / name / ".git").is_dir():
                repos[name] = base / name
                return

    for item in data["attach_to_existing"]:
        locate(item["repo"])
        for ref in item.get("thread_evidence", []):
            m = SHA_REF_DRAFT.match(str(ref))
            if m:
                locate(m.group(1))

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in data["attach_to_existing"]:
        grouped[item["thread"]].append(item)

    lines = [f"# Proposed CV changes — {window}", "",
             f"{len(grouped)} thread(s) to rewrite, "
             f"{len(data['candidate_new_threads'])} candidate new thread(s).", ""]
    results = []

    for thread_id, items in grouped.items():
        print(f"drafting {thread_id} ({len(items)} unit(s))", file=sys.stderr)
        current = items[0]["current_text"]
        units_text = describe_units(repos, items)
        shas = [f"{i['repo']}@{i['unit']['shas'][0][:7]}" for i in items]

        result = None
        if not args.no_claude:
            result = ask_claude(PROMPT.format(
                current=current,
                tags=", ".join(items[0].get("tags", [])) or "(none recorded)",
                prior=prior_context(repos, items[0].get("thread_evidence", [])),
                units=units_text,
            ))

        lines += [f"## `{thread_id}`", "",
                  f"**{items[0]['owner']}** · {items[0]['section']}", "",
                  "**Now**", "", f"> {current}", ""]

        if result and result.get("action") == "rewrite" and result.get("text"):
            lines += ["**Proposed**", "", f"> {result['text']}", "",
                      f"*{result.get('why', '')}*  ",
                      f"confidence: **{result.get('confidence', '?')}**", ""]
            if result.get("unverifiable"):
                lines += ["Considered but not supported by the diff, so left out:", ""]
                lines += [f"- {u}" for u in result["unverifiable"]] + [""]
            results.append({"thread": thread_id, "text": result["text"], "evidence": shas})
        elif result and result.get("action") == "keep":
            lines += [f"**No change proposed** — {result.get('why', '')}", ""]
        else:
            lines += ["**No wording drafted** (drafting unavailable). "
                      "New work on this thread:", ""]
            lines += [f"- `{s}` {i['unit']['subject']}" for s, i in zip(shas, items)] + [""]

        lines += ["<details><summary>New commits</summary>", "",
                  "```", units_text[:4000], "```", "</details>", "", "---", ""]

    if data["candidate_new_threads"]:
        lines += ["## Candidate new threads", "",
                  "Not applied automatically — deciding whether work belongs on the CV, "
                  "and under which role, needs a human.", ""]
        for c in data["candidate_new_threads"]:
            lines += [f"### {c['repo']} · `{c['area']}`",
                      f"{c['unit_count']} units, +{c['added']} lines", ""]
            lines += [f"- {s}" for s in c["subjects"][:6]] + [""]

    args.output.write_text("\n".join(lines), encoding="utf-8")
    drafted = args.output.with_suffix(".json")
    drafted.write_text(json.dumps({"version": 1, "window": window,
                                   "rewrites": results}, indent=2) + "\n",
                       encoding="utf-8")
    print(f"\nwrote {args.output.name} and {drafted.name}", file=sys.stderr)

    if args.apply and results:
        applied = 0
        for item in results:
            if apply_rewrite(item["thread"], item["text"], item["evidence"]):
                applied += 1
                print(f"  applied {item['thread']}", file=sys.stderr)
            else:
                print(f"  FAILED to apply {item['thread']}", file=sys.stderr)
        print(f"applied {applied}/{len(results)} rewrites to content/", file=sys.stderr)
    elif args.apply:
        print("nothing to apply", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
