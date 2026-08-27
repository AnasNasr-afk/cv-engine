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
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

MAX_UNITS_IN_PROMPT = 8
MAX_FILES_PER_UNIT = 6
MAX_LINES_PER_FILE = 60
CALL_TIMEOUT = 240

SHA_REF_DRAFT = re.compile(r"^([A-Za-z0-9._-]+)@([0-9a-f]{7,40})$")

SKIP_DIFF = re.compile(r"\.(lock|png|jpg|jpeg|pdf|otf|ttf|woff2?|svg|ico|strings)$|"
                       r"(^|/)(dist|build|generated|\.next)/", re.I)

NEW_THREAD_PROMPT = """You are maintaining a one-page engineering CV.

A cluster of new work has no matching line on the CV yet. Decide whether it
belongs on the CV at all, and if so, write the line and say where it goes.

THE WORK
{work}

PLACES IT COULD GO (use the name exactly as written, or "none")
{places}

RULES
- Most work does not belong on a CV. Practice exercises, config tweaks,
  dependency bumps, scaffolding and README edits are not achievements. If
  in doubt, answer worthy=false. A CV that grows every month is a worse CV.
- Place it under the employer or project whose repository this is. If none
  of the listed places fit and the work is a substantial project in its own
  right, use "new_project" and give it a name and stack.
- Never invent metrics, ownership, scale or production use.
- 20-40 words. LaTeX markup allowed: \\textbf{{...}}. Literal percent as \\%.
- `tags` come from this vocabulary where they apply: ios, swift, flutter,
  dart, web, react, typescript, backend, architecture, performance,
  security, ui, ai, mobile, leadership, testing, devops.

Reply with ONLY a JSON object, no prose, no fence:
{{"worthy": true | false,
  "why": "one sentence",
  "place": "exact name from the list, or new_project, or none",
  "project_name": "only when place is new_project",
  "stack": "only when place is new_project",
  "id": "kebab-case-thread-id",
  "text": "the CV line",
  "tags": ["..."],
  "weight": 1-10}}
"""

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
    """Diffs for the largest units, with the rest listed by subject.

    A busy month can attach twenty commits to one thread; including every
    diff would build a prompt of several thousand lines to rewrite a single
    sentence. The biggest changes carry the substance, and the remainder
    still appear by subject so nothing is silently dropped.
    """
    ranked = sorted(items, key=lambda i: -i["unit"]["added"])
    shown, rest = ranked[:MAX_UNITS_IN_PROMPT], ranked[MAX_UNITS_IN_PROMPT:]
    blocks = []
    for item in shown:
        unit = item["unit"]
        repo = repos.get(item["repo"])
        head = (f"* {unit['subject']}\n"
                f"  {unit['date']}  {unit['file_count']} files  "
                f"+{unit['added']}/-{unit['deleted']}  repo: {item['repo']}\n"
                f"  paths: {', '.join(unit['paths'][:8])}")
        body = diff_excerpt(repo, unit["shas"][0]) if repo else "  (repo not found locally)"
        blocks.append(head + "\n" + body)
    if rest:
        listed = "\n".join(f"  - {i['unit']['subject']} (+{i['unit']['added']})" for i in rest)
        blocks.append(f"ALSO ON THIS THREAD, DIFFS OMITTED FOR LENGTH:\n{listed}")
    return "\n\n".join(blocks)


#: Credentials arrive through CI secrets, where a value copied from a
#: wrapped terminal keeps its line break. Claude Code then rejects it as an
#: invalid Authorization header, which reads like a bad token rather than a
#: bad paste. Strip them once, at the point of use.
for _var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
    _value = os.environ.get(_var)
    if _value and _value.strip() != _value.replace("\n", "").replace("\r", ""):
        os.environ[_var] = "".join(_value.split())


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
        # The CLI reports most failures on stdout, so a stderr-only message
        # prints as "claude failed:" with nothing after it -- which is how a
        # missing ANTHROPIC_API_KEY looked for a whole CI run.
        detail = (err.strip() or out.strip() or f"exit code {code}")[:300]
        print(f"  claude failed: {detail}", file=sys.stderr)
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            print("  hint: no ANTHROPIC_API_KEY in the environment", file=sys.stderr)
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
        escaped = toml_str(text)
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


def toml_str(value: str) -> str:
    """Escape a value for a TOML basic string.

    TOML reads \\t, \\n and friends as control characters, so writing LaTeX
    like \\textbf verbatim silently turns the command into a TAB followed by
    "extbf" -- valid TOML, valid LaTeX-free text, and a corrupted CV line.
    Backslashes must be doubled and quotes escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def plain(label: str) -> str:
    """Strip LaTeX so a name compares equal however it was read.

    entry_places() reads names through tomllib, where "\\\\textbf" has already
    become a single backslash; insert_bullet() regexes the raw file, where it
    is still two characters. Removing command names alone left a stray
    backslash on one side and the two never matched.
    """
    return " ".join(re.sub(r"\\+[a-zA-Z]+|[\\{}]", "", label).split())


def entry_places(content: Path) -> list[str]:
    """Employers and projects a new bullet could be attached to."""
    import tomllib
    places = []
    for name in ("experience.toml", "projects.toml", "leadership.toml"):
        path = content / name
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for entry in data.get("entry", []):
            label = entry.get("org") or entry.get("name") or ""
            clean = plain(label)
            dates = entry.get("dates", "")
            places.append(f"{clean}  [{name.replace('.toml','')}{', ' + dates if dates else ''}]")
    return places


def insert_bullet(content: Path, place: str, bullet: dict[str, Any]) -> bool:
    """Add a bullet to an existing entry, keeping the file's comments."""
    target = plain(re.sub(r"\s*\[.*\]$", "", place))
    for name in ("experience.toml", "projects.toml", "leadership.toml"):
        path = content / name
        if not path.is_file():
            continue
        source = path.read_text()
        for m in re.finditer(r'^(?:org|name)\s*=\s*"(.+)"\s*$', source, re.M):
            clean = plain(m.group(1))
            if clean != target:
                continue
            # Insert just before the next [[entry]], or at end of file.
            nxt = re.search(r"^\[\[entry\]\]", source[m.end():], re.M)
            at = m.end() + nxt.start() if nxt else len(source)
            ev = "".join(f'    "{e}",\n' for e in bullet["evidence"])
            block = (f'\n  [[entry.bullets]]\n'
                     f'  id       = "{bullet["id"]}"\n'
                     f'  text     = "{toml_str(bullet["text"])}"\n'
                     f'  tags     = {json.dumps(bullet["tags"])}\n'
                     f'  weight   = {bullet["weight"]}\n'
                     f'  evidence = [\n{ev}  ]\n')
            path.write_text(source[:at].rstrip("\n") + "\n" + block + "\n" + source[at:])
            return True
    return False


def create_project(content: Path, bullet: dict[str, Any], name: str, stack: str) -> bool:
    path = content / "projects.toml"
    ev = "".join(f'    "{e}",\n' for e in bullet["evidence"])
    block = (f'\n[[entry]]\n'
             f'name   = "{toml_str(name)}"\n'
             f'stack  = "{toml_str(stack)}"\n'
             f'tags   = {json.dumps(bullet["tags"])}\n'
             f'weight = {bullet["weight"]}\n'
             f'links  = []\n\n'
             f'  [[entry.bullets]]\n'
             f'  id       = "{bullet["id"]}"\n'
             f'  text     = "{toml_str(bullet["text"])}"\n'
             f'  tags     = {json.dumps(bullet["tags"])}\n'
             f'  weight   = {bullet["weight"]}\n'
             f'  evidence = [\n{ev}  ]\n')
    path.write_text(path.read_text().rstrip("\n") + "\n" + block)
    return True


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
        for item in saved.get("new_threads", []):
            if item["place"] == "new_project" and item.get("project_name"):
                create_project(CONTENT, item, item["project_name"], item.get("stack", ""))
            elif item["place"] and item["place"] != "none":
                insert_bullet(CONTENT, item["place"], item)
        print(f"applied {applied}/{len(saved['rewrites'])} saved rewrites, "
              f"{len(saved.get('new_threads', []))} new thread(s)", file=sys.stderr)
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

    new_threads = []
    if data["candidate_new_threads"]:
        places = entry_places(CONTENT)
        lines += ["## New threads", ""]
        for c in data["candidate_new_threads"]:
            work = (f"repo: {c['repo']}   area: {c['area']}\n"
                    f"{c['unit_count']} commits, +{c['added']} lines\n"
                    + "\n".join(f"  - {s}" for s in c["subjects"][:10]))
            repo = repos.get(c["repo"])
            if repo:
                work += "\n\n" + diff_excerpt(repo, c["shas"][0])

            verdict = None if args.no_claude else ask_claude(
                NEW_THREAD_PROMPT.format(work=work, places="\n".join(f"  - {p}" for p in places)))

            head = f"### {c['repo']} · `{c['area']}` — {c['unit_count']} units, +{c['added']}"
            if not verdict:
                lines += [head, "", "_No verdict drafted._", ""]
                lines += [f"- {s}" for s in c["subjects"][:6]] + [""]
                continue
            if not verdict.get("worthy"):
                lines += [head, "", f"**Not CV material** — {verdict.get('why','')}", ""]
                continue
            evidence = [f"{c['repo']}@{s[:7]}" for s in c["shas"][:6]]
            item = {"id": verdict.get("id") or f"{c['repo'].lower()}-work",
                    "text": verdict.get("text", ""),
                    "tags": verdict.get("tags") or [],
                    "weight": int(verdict.get("weight") or 6),
                    "evidence": evidence,
                    "place": verdict.get("place", "none"),
                    "project_name": verdict.get("project_name"),
                    "stack": verdict.get("stack", "")}
            new_threads.append(item)
            lines += [head, "", f"**New line** → _{item['place']}_", "",
                      f"> {item['text']}", "", f"*{verdict.get('why','')}*", ""]

    args.output.write_text("\n".join(lines), encoding="utf-8")
    drafted = args.output.with_suffix(".json")
    drafted.write_text(json.dumps({"version": 1, "window": window,
                                   "rewrites": results,
                                   "new_threads": new_threads}, indent=2) + "\n",
                       encoding="utf-8")
    print(f"\nwrote {args.output.name} and {drafted.name}", file=sys.stderr)

    # New threads must be applied even when nothing was rewritten. A month
    # of entirely new work produces zero rewrites, and gating on `results`
    # alone reported "nothing to apply" while discarding every new bullet.
    if args.apply and (results or new_threads):
        applied = 0
        for item in results:
            if apply_rewrite(item["thread"], item["text"], item["evidence"]):
                applied += 1
                print(f"  applied {item['thread']}", file=sys.stderr)
            else:
                print(f"  FAILED to apply {item['thread']}", file=sys.stderr)
        for item in new_threads:
            if item["place"] == "new_project" and item.get("project_name"):
                ok = create_project(CONTENT, item, item["project_name"], item.get("stack", ""))
            elif item["place"] and item["place"] != "none":
                ok = insert_bullet(CONTENT, item["place"], item)
            else:
                ok = False
            print(f"  {'added' if ok else 'SKIPPED'} new thread {item['id']}", file=sys.stderr)
        print(f"applied {applied}/{len(results)} rewrites, "
              f"{len(new_threads)} new thread(s)", file=sys.stderr)
    elif args.apply:
        print("nothing to apply", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
