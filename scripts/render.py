#!/usr/bin/env python3
"""Render role-targeted LaTeX sections from tagged content.

    python3 scripts/render.py --role ios [--explain] [--budget N]

Reads content/*.toml and roles/<role>.toml, scores every bullet against
the role's tag weights, keeps the strongest within each section's limits,
and writes sections/*.tex. Those files are build output, not source --
edit content/ and roles/ instead.

Scoring: a bullet's score is its `weight` scaled by the geometric mean of
the role `boost` for each of its tags. Tags the role does not mention
default to 1.0, so an untagged bullet keeps its base weight rather than
vanishing. An entry (job/project) blends its own score with the mean of
its kept bullets additively, at 60/40.

Both choices exist to stop tag-count from dominating substance: a plain
product is exponential in the number of tags, and multiplying an entry by
its bullets applies the same role weighting twice.

Ordering: projects and leadership are ranked by score; experience is not.
Jobs stay in authored (reverse-chronological) order, because moving a
current employer below an older one is what a CV reader treats as a red
flag. Relevance shows up in which bullets survive inside each job.

`--budget` trims the lowest-scoring bullets until at most N remain across
the whole document. The Makefile uses it to fit one page automatically.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
ROLES = ROOT / "roles"
SECTIONS = ROOT / "sections"

GENERATED_HEADER = (
    "% GENERATED FILE -- do not edit.\n"
    "% Produced by scripts/render.py for role: {role}\n"
    "% Edit content/*.toml or roles/*.toml instead.\n"
)


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"error: missing {path.relative_to(ROOT)}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def score_of(item: dict[str, Any], boost: dict[str, float]) -> float:
    """Base weight scaled by the *geometric mean* of its tag multipliers.

    A plain product is exponential in tag count -- four tags boosted 2.5x
    would multiply a bullet by ~39, so merely tagging something thoroughly
    outranks work that is substantively stronger. The geometric mean keeps
    the direction of each boost while bounding the result to roughly the
    range of the individual multipliers, which is what the numbers in
    roles/*.toml read as if they mean.
    """
    tags = item.get("tags", [])
    weight = float(item.get("weight", 5))
    if not tags:
        return weight
    product = 1.0
    for tag in tags:
        product *= float(boost.get(tag, 1.0))
    return weight * (product ** (1.0 / len(tags)))


def pick_bullets(
    entry: dict[str, Any], boost: dict[str, float], cap: int
) -> list[dict[str, Any]]:
    """Highest-scoring bullets, capped, restored to their authored order.

    Selection is by score, but presentation keeps the order the author
    wrote them in -- reordering bullets inside a job reads as arbitrary.
    """
    bullets = list(enumerate(entry.get("bullets", [])))
    ranked = sorted(bullets, key=lambda pair: -score_of(pair[1], boost))
    kept = sorted(ranked[:cap], key=lambda pair: pair[0])
    out = []
    for index, bullet in kept:
        item = dict(bullet)
        item["_score"] = score_of(bullet, boost)
        out.append(item)
    return out


def rank_entries(
    entries: list[dict[str, Any]], boost: dict[str, float], bullet_cap: int
) -> list[dict[str, Any]]:
    ranked = []
    for entry in entries:
        kept = pick_bullets(entry, boost, bullet_cap)
        if not kept and entry.get("bullets"):
            continue
        mean = sum(b["_score"] for b in kept) / len(kept) if kept else 0.0
        item = dict(entry)
        item["_bullets"] = kept
        # Blend additively, not multiplicatively: the entry and its bullets
        # already carry the same tag boosts, so multiplying them squares the
        # role weighting and lets a narrowly-relevant trifle outrank
        # substantially stronger work.
        base = score_of(entry, boost)
        item["_score"] = 0.6 * base + 0.4 * mean if kept else base
        ranked.append(item)
    return ranked


#: Sections the budget will never take a bullet from.
PROTECTED_SECTIONS = frozenset({"education"})


def apply_budget(sections: dict[str, list[dict[str, Any]]], budget: int | None) -> int:
    """Drop the globally lowest-scoring bullets until `budget` remain.

    `budget` counts every bullet in the document, so it matches the total
    the renderer reports. Two protections apply: education is never
    trimmed, and no job is taken below two bullets -- a job showing a
    single line reads as filler, so the budget takes from projects and
    extras first.
    """
    if budget is None:
        return 0
    pool: list[tuple[float, str, int, list]] = []
    for name, entries in sections.items():
        for entry in entries:
            siblings = entry["_bullets"]
            for position, bullet in enumerate(siblings):
                pool.append((bullet["_score"], name, id(bullet), siblings))

    dropped = 0
    while len(pool) > budget:
        pool.sort(key=lambda row: row[0])
        for index, (_, name, ident, siblings) in enumerate(pool):
            if name in PROTECTED_SECTIONS:
                continue
            if name == "experience" and len(siblings) <= 2:
                continue
            # Remove by identity: two bullets can compare equal as dicts,
            # and list.remove() would then delete the wrong one.
            for position, candidate in enumerate(siblings):
                if id(candidate) == ident:
                    del siblings[position]
                    break
            pool.pop(index)
            dropped += 1
            break
        else:
            break  # everything left is protected
    return dropped


# --------------------------------------------------------------------- #
# LaTeX emission
# --------------------------------------------------------------------- #

BARE_PERCENT = re.compile(r"(?<!\\)%")


def tex(value: Any) -> str:
    """Emit authored text safely.

    Content is authored as LaTeX -- \\textbf{...} and friends are intended
    -- so this deliberately does not escape backslashes or braces. It only
    fixes the one character that is silently destructive: an unescaped `%`
    starts a LaTeX comment, so "cut load by 40% and shipped it" swallows
    the rest of the line including the closing brace. Percentages are
    common on a CV and nobody expects to escape them by hand.
    """
    return BARE_PERCENT.sub(r"\\%", str(value))


def esc_url(url: str) -> str:
    return url.replace("%", r"\%").replace("#", r"\#")


def render_heading(profile: dict[str, Any], role: str) -> str:
    links = " $\\cdot$\n  ".join(
        f"\\underline{{\\href{{{esc_url(l['url'])}}}{{{l['label']}}}}}"
        for l in profile.get("links", [])
    )
    return (
        GENERATED_HEADER.format(role=role)
        + "\\begin{center}\n"
        f"  {{\\Huge \\scshape {tex(profile['name'])}}} \\\\[4pt]\n"
        "  \\footnotesize\n"
        f"  {tex(profile['location'])} $\\cdot$\n"
        f"  \\underline{{\\href{{mailto:{profile['email']}}}{{{profile['email']}}}}} $\\cdot$\n"
        f"  {tex(profile['phone'])} \\\\[2pt]\n"
        f"  {links}\n"
        "\\end{center}\n"
        "\\vspace{-11pt}\n"
    )


def render_subheading_section(title: str, entries: list[dict[str, Any]], role: str,
                              tail_space: str = "-14pt") -> str:
    out = [GENERATED_HEADER.format(role=role), f"\\section{{{title}}}",
           "\\resumeSubHeadingListStart", ""]
    for entry in entries:
        out.append("  \\resumeSubheading")
        out.append(f"    {{{tex(entry['org'])}}}{{{tex(entry['location'])}}}")
        out.append(f"    {{{tex(entry['role'])}}}{{{tex(entry['dates'])}}}")
        if entry["_bullets"]:
            out.append("  \\resumeItemListStart")
            for bullet in entry["_bullets"]:
                out.append(f"    \\resumeItem{{{tex(bullet['text'])}}}")
            out.append("  \\resumeItemListEnd")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    out.append(f"\\vspace{{{tail_space}}}")
    return "\n".join(out) + "\n"


def render_projects(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role), "\\section{Projects}",
           "\\vspace{-4pt}", "\\resumeSubHeadingListStart", ""]
    for index, entry in enumerate(entries):
        links = " $|$ ".join(
            f"\\underline{{\\href{{{esc_url(l['url'])}}}{{{l['label']}}}}}"
            for l in entry.get("links", [])
        )
        stack = f" $|$ \\textit{{{tex(entry['stack'])}}}" if entry.get("stack") else ""
        out.append("\\resumeProjectHeading")
        out.append(f"  {{{tex(entry['name'])}{stack}}}")
        out.append(f"  {{{links}}}")
        if entry["_bullets"]:
            out.append("\\resumeItemListStart")
            for bullet in entry["_bullets"]:
                out.append(f"  \\resumeItem{{{tex(bullet['text'])}}}")
            out.append("\\resumeItemListEnd")
        if index != len(entries) - 1:
            out.append("\\vspace{-12pt}")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    out.append("\\vspace{-14pt}")
    return "\n".join(out) + "\n"


def render_skills(categories: list[dict[str, Any]], role: str) -> str:
    lines = []
    for index, cat in enumerate(categories):
        sep = " \\\\[1mm]" if index != len(categories) - 1 else ""
        lines.append(f"    \\textbf{{{tex(cat['label'])}}}{{: {tex(cat['items'])}}}{sep}")
    return (
        GENERATED_HEADER.format(role=role)
        + "\\section{Technical Skills}\n"
        "\\begin{itemize}[leftmargin=0.15in, label={}]\n"
        "  \\small{\\item{\n"
        + "\n".join(lines)
        + "\n  }}\n\\end{itemize}\n\\vspace{-14pt}\n"
    )


def render_leadership(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role), "\\section{Leadership \\& Community}",
           "\\vspace{-4pt}", "\\resumeSubHeadingListStart", ""]
    for entry in entries:
        out.append("  \\resumeProjectHeading")
        out.append(f"    {{{tex(entry['name'])}}}")
        out.append(f"    {{{tex(entry['dates'])}}}")
        if entry["_bullets"]:
            out.append("  \\resumeItemListStart")
            for bullet in entry["_bullets"]:
                out.append(f"    \\resumeItem{{{tex(bullet['text'])}}}")
            out.append("  \\resumeItemListEnd")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--explain", action="store_true",
                        help="print the score behind every kept and dropped bullet")
    parser.add_argument("--budget", type=int,
                        help="keep at most N bullets document-wide")
    args = parser.parse_args()

    role_path = ROLES / f"{args.role}.toml"
    if not role_path.is_file():
        available = ", ".join(sorted(p.stem for p in ROLES.glob("*.toml")))
        raise SystemExit(f"error: unknown role '{args.role}'. Available: {available}")

    role = load(role_path)
    boost: dict[str, float] = role.get("boost", {})
    limits: dict[str, int] = role.get("limits", {})

    profile = load(CONTENT / "profile.toml")
    education = load(CONTENT / "education.toml").get("entry", [])
    experience = load(CONTENT / "experience.toml").get("entry", [])
    projects = load(CONTENT / "projects.toml").get("entry", [])
    skills = load(CONTENT / "skills.toml").get("category", [])
    leadership = load(CONTENT / "leadership.toml").get("entry", [])

    edu = rank_entries(education, boost, limits.get("experience_bullets_max", 3))
    # Experience stays in authored (reverse-chronological) order. Ranking
    # jobs by relevance would push a current role below an older one, which
    # breaks the convention recruiters read for and looks like concealment.
    # Relevance is expressed by which bullets survive inside each job.
    exp = rank_entries(experience, boost, limits.get("experience_bullets_max", 5))
    prj = rank_entries(projects, boost, limits.get("project_bullets_max", 1))
    prj.sort(key=lambda e: -e["_score"])
    prj = prj[: limits.get("projects_max", 4)]
    ldr = rank_entries(leadership, boost, 1)
    ldr.sort(key=lambda e: -e["_score"])
    ldr = ldr[: limits.get("leadership_max", 1)]

    skl = sorted(skills, key=lambda c: -score_of(c, boost))[: limits.get("skills_max", 5)]

    dropped = apply_budget(
        {"education": edu, "experience": exp, "projects": prj, "leadership": ldr},
        args.budget,
    )

    SECTIONS.mkdir(exist_ok=True)
    written = {
        "heading.tex": render_heading(profile, args.role),
        "education.tex": render_subheading_section("Education", edu, args.role),
        "experience.tex": render_subheading_section("Experience", exp, args.role),
        "skills.tex": render_skills(skl, args.role),
        "projects.tex": render_projects(prj, args.role),
        "leadership.tex": render_leadership(ldr, args.role),
    }
    for name, text in written.items():
        (SECTIONS / name).write_text(text, encoding="utf-8")

    order = role.get("sections", {}).get(
        "order", ["education", "experience", "skills", "projects", "leadership"]
    )
    # The heading is not orderable: a CV without a name and contact block
    # is not a CV. Emit it first regardless of what the role lists, and
    # ignore it if a role names it explicitly.
    order = ["heading"] + [name for name in order if name != "heading"]
    body = "\n\n".join(f"\\input{{sections/{name}}}" for name in order)
    (SECTIONS / "order.tex").write_text(
        GENERATED_HEADER.format(role=args.role) + body + "\n", encoding="utf-8"
    )

    kept = sum(len(e["_bullets"]) for e in exp + prj + ldr + edu)
    print(f"role={args.role} ({role.get('name')})  bullets={kept}"
          + (f"  dropped_by_budget={dropped}" if dropped else ""))
    print("  experience: " + ", ".join(f"{e['org']}({len(e['_bullets'])})" for e in exp))
    print("  projects  : " + ", ".join(e["name"].split(" ---")[0] for e in prj))
    print("  skills    : " + ", ".join(c["label"] for c in skl))

    if args.explain:
        print("\n--- scores ---")
        for label, entries in (("EXPERIENCE", exp), ("PROJECTS", prj), ("LEADERSHIP", ldr)):
            print(f"\n{label}")
            for entry in entries:
                title = entry.get("org") or entry.get("name")
                print(f"  [{entry['_score']:7.1f}] {title}")
                for bullet in entry["_bullets"]:
                    tags = ",".join(bullet.get("tags", []))
                    ev = len(bullet.get("evidence", []))
                    flag = "" if ev else "   <- NO EVIDENCE"
                    print(f"      {bullet['_score']:7.1f}  [{tags}]{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
